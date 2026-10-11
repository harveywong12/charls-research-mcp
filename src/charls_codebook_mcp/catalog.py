"""Bounded read-only research tools over the locally published catalogue."""
import json
import hashlib
from pathlib import Path
import re
import sqlite3
import threading

from .query import Codebook, QueryError, envelope, encoded_size, escape_like, literal_fts
from .catalog_documents import DocumentEvidence
from .presentation import check_fields, finish_page, RETRIEVAL_REVISION
from .indicator_evidence import IndicatorEvidence


def token_contains(text, term):
    # Underscore separates identifier components: TG matches bl_tg, never mortgage.
    return int(bool(text and re.search(r'(?<![A-Za-z0-9])' + re.escape(term) +
                                     r'(?![A-Za-z0-9])', str(text), re.I)))


def strict_term(term, mode):
    return mode == 'exact' or (mode == 'auto' and (term.upper() in {'TG','TC','GHB','BP','PLT','BMI','HDL','LDL','CRP','BUN','MCV','HCT','WBC','ADL','IADL','CESD'} or bool(re.fullmatch(r'[A-Z]{2,4}', term))))


def text_matches(text, term, mode):
    return bool(token_contains(text, term)) if strict_term(term, mode) else term.casefold() in (text or '').casefold()

CAUTION = '候选匹配不证明指标定义相同；跨年合并前须核对问法、取值、回忆期、适用人群和构建规则。'
NO_MATCH = '未命中只说明当前索引与查询无匹配，不证明该年份未测量；请用英文、变量名或不同关键词复查。'
MAIN_YEARS = [2011, 2013, 2015, 2018, 2020]
# Generic terms must not be matched against labels, value labels or documented
# source lists in the expanded retrieval tier; they are matched against variable
# names only. A per-alias "weak" list in the alias file replaces this default.
DEFAULT_WEAK = {'kidney', 'disease', 'condition', 'weight', 'height', 'age', 'health',
                'care', 'help', 'support', 'visit', 'functioning', 'limitation'}
# The catalogue stores an internal wave number derived from the folder year. Official
# CHARLS wave labels may differ (download paths use "wave4" for more than one year), so
# every response that exposes `wave` states the basis and warns against citing it blind.
WAVE_BASIS = ('catalogue internal mapping (2011→1, 2013→2, 2015→3, 2018→4, 2020→5); '
              'official CHARLS wave labels may differ — confirm against the official codebook '
              'before citing a wave number')


class ResearchCatalog(IndicatorEvidence, DocumentEvidence, Codebook):
    def __init__(self, path, alias_path=None):
        path = Path(path).resolve()
        if not path.is_file():
            raise ValueError('RESEARCH_CATALOG_MISSING')
        self.con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.enable_load_extension(False)
        self.con.execute('PRAGMA query_only=ON')
        self.con.create_function('token_contains', 2, token_contains, deterministic=True)
        self.lock = threading.RLock()
        try:
            self.meta = dict(self.con.execute('SELECT key,value FROM meta'))
            version = self.con.execute('PRAGMA user_version').fetchone()[0]
            if version not in (2, 3, 4) or self.meta.get('published') != '1':
                raise ValueError('RESEARCH_CATALOG_UNPUBLISHED_OR_INCOMPATIBLE')
            self.schema_version = version
            # schema 3 adds dataset_columns(dataset_id,name,ordinal); schema 2 databases
            # stay readable and simply report that file column order is not stored.
            self.has_ordinal = bool(self.con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dataset_columns'").fetchone())
            self.aliases = json.loads(Path(alias_path).read_text(encoding='utf-8')) if alias_path else []
            self.alias_digest = hashlib.sha256(json.dumps(self.aliases, sort_keys=True,
                                                        ensure_ascii=False).encode()).hexdigest()
        except Exception:
            self.con.close()
            raise

    def _cursor_context(self, context):
        digest=hashlib.sha256(json.dumps(context,sort_keys=True,ensure_ascii=False,
                                        separators=(',',':')).encode()).hexdigest()
        return {'kind':context['kind'],'build':self.meta['build_id'],
                'query_context_sha256':digest, 'retrieval_revision':RETRIEVAL_REVISION,
                'aliases_sha256':self.alias_digest}

    def _cursor(self, context, offset):
        return super()._cursor(self._cursor_context(context), offset)

    def _offset(self, cursor, context):
        return super()._offset(cursor, self._cursor_context(context))

    def _terms(self, query, expand=None):
        """Literal-first retrieval terms with concept-scoped alias expansion.

        A query equal to a concept term (e.g. 肾功能) expands by default. A query
        that only matches one English member (e.g. cystatin) stays literal and
        returns a hint instead of pulling in the whole, often generic, concept set.
        expand=True restores the legacy behaviour; expand=False disables expansion.
        """
        if not isinstance(query, str) or not 0 < len(query.strip()) <= 128:
            raise ValueError('query 必须为 1–128 字符；一次查一个指标')
        if expand is not None and type(expand) is not bool:
            raise ValueError('expand 必须为布尔值；true 恢复旧的全展开行为')
        query = query.strip()
        literal, expanded, weak, expansions, hints = [query], [], [], [], []
        for alias in self.aliases:
            term, english = alias.get('term'), alias.get('english')
            if not isinstance(term, str) or not isinstance(english, list) or not english:
                continue
            concept_hit = term.casefold() == query.casefold()
            member_hit = query.casefold() in [t.casefold() for t in english if isinstance(t, str)]
            if expand is False or not (concept_hit or member_hit):
                continue
            if concept_hit or expand is True:
                raw_weak = alias.get('weak')
                weak_list = raw_weak if isinstance(raw_weak, list) else sorted(DEFAULT_WEAK)
                weak_set = {w.casefold() for w in weak_list if isinstance(w, str)}
                weak += [t for t in english if t.casefold() in weak_set]
                # The concept term itself is kept in the expanded set: for a concept
                # query it equals the literal term and is deduplicated, while with
                # expand=true it restores the legacy retrieval behaviour.
                expanded += [term] + [t for t in english if t.casefold() not in weak_set]
                expansions.append({'term': term, 'english': english,
                                   'weak_terms': [t for t in english if t.casefold() in weak_set],
                                   'weak_scope': '弱词仅按变量名匹配' if weak_set else None,
                                   'meaning': 'candidate retrieval terms, not measurement equivalence'})
            elif member_hit:
                hints.append({'term': term, 'english': english,
                              'action': '如需按概念扩展，请显式设置 expand=true'})
        drop = {t.casefold() for t in literal}
        literal = list(dict.fromkeys(literal))
        expanded = [t for t in dict.fromkeys(expanded) if t.casefold() not in drop]
        expanded_set = {t.casefold() for t in expanded}
        weak = [t for t in dict.fromkeys(weak)
                if t.casefold() not in drop and t.casefold() not in expanded_set]
        return {'literal': literal, 'expanded': expanded, 'weak': weak,
                'effective': literal + expanded + weak, 'expansions': expansions, 'hints': hints}

    def _clause(self, term, name_only=False, match_mode='auto'):
        if strict_term(term, match_mode):
            if name_only:
                return 'token_contains(v.name,?)=1', [term]
            sql = "(token_contains(v.name,?)=1 OR token_contains(v.label,?)=1 OR EXISTS(SELECT 1 FROM json_each(v.value_codes) code WHERE token_contains(json_extract(code.value,'$.label'),?)=1) OR EXISTS(SELECT 1 FROM raw_source_links l WHERE l.dataset_id=v.dataset_id AND l.name=v.name AND (token_contains(l.topic_title,?)=1 OR token_contains(l.raw_label,?)=1)))"
            args = [term]*5
            if self.schema_version >= 4:
                sql = '(' + sql + " OR EXISTS(SELECT 1 FROM reviewed_variable_facts f WHERE f.dataset_id=v.dataset_id AND f.name=v.name AND (token_contains(json_extract(f.fact,'$.construct'),?)=1 OR token_contains(json_extract(f.fact,'$.search_terms'),?)=1)))"
                args += [term]*2
            return sql, args
        pattern = '%' + escape_like(term) + '%'
        if name_only:
            return "v.name LIKE ? ESCAPE '\\'", [pattern]
        sql = "(v.name LIKE ? ESCAPE '\\' OR v.label LIKE ? ESCAPE '\\' OR EXISTS(SELECT 1 FROM json_each(v.value_codes) code WHERE json_extract(code.value,'$.label') LIKE ? ESCAPE '\\') OR EXISTS(SELECT 1 FROM raw_source_links l WHERE l.dataset_id=v.dataset_id AND l.name=v.name AND (l.topic_title LIKE ? ESCAPE '\\' OR l.raw_label LIKE ? ESCAPE '\\')))"
        args = [pattern]*5
        if self.schema_version >= 4:
            sql = '(' + sql + " OR EXISTS(SELECT 1 FROM reviewed_variable_facts f WHERE f.dataset_id=v.dataset_id AND f.name=v.name AND (json_extract(f.fact,'$.construct') LIKE ? ESCAPE '\\' OR json_extract(f.fact,'$.search_terms') LIKE ? ESCAPE '\\')))"
            args += [pattern]*2
        return sql,args

    def _or_clause(self, pairs, match_mode='auto'):
        clauses, args = [], []
        for term, name_only in pairs:
            sql, values = self._clause(term, name_only, match_mode)
            clauses.append(sql)
            args.extend(values)
        return ' OR '.join(clauses), args

    def _search_parts(self, query, group=None, namespace=None, year=None, expand=None, match_mode='auto', module=None):
        if match_mode not in ('auto', 'exact', 'broad'):
            raise ValueError('match_mode must be auto/exact/broad')
        terms = self._terms(query, expand)
        filters, filter_args = self._filters(group, namespace, year)
        if module is not None:
            if not isinstance(module, str) or not 0 < len(module) <= 80:
                raise ValueError('module 无效')
            filters.append('d.module=?'); filter_args.append(module)
        literal_sql, literal_args = self._or_clause([(t, False) for t in terms['literal']], match_mode)
        expanded_sql, expanded_args = self._or_clause(
            [(t, False) for t in terms['expanded']] + [(t, True) for t in terms['weak']], match_mode)
        return {'terms': terms, 'filters': filters, 'filter_args': filter_args,
                'literal_sql': literal_sql, 'literal_args': literal_args,
                'expanded_sql': expanded_sql, 'expanded_args': expanded_args}

    def _where_for(self, parts):
        conditions = list(parts['filters'])
        combined = '(' + parts['literal_sql'] + ')'
        if parts['expanded_sql']:
            combined += ' OR (' + parts['expanded_sql'] + ')'
        conditions.append('(' + combined + ')')
        return ' AND '.join(conditions), (parts['filter_args'] + parts['literal_args']
                                          + parts['expanded_args'])

    def _select(self, literal_sql):
        return self.SELECT.replace(
            ' FROM ', ', CASE WHEN (' + literal_sql + ') THEN 1 ELSE 0 END AS is_literal FROM ', 1)

    def _filters(self, group=None, namespace=None, year=None):
        where, args = [], []
        for field, value in [('d.group_name', group), ('d.namespace', namespace)]:
            if value is not None:
                if not isinstance(value, str) or len(value) > 80:
                    raise ValueError('group/namespace 无效')
                where.append(field + '=?')
                args.append(value)
        if year is not None:
            if type(year) is not int or year not in {2008, 2011, 2012, 2013, 2014, 2015, 2018, 2020}:
                raise ValueError('year 不属于当前数据目录')
            where.append('v.year=?')
            args.append(year)
        return where, args


    def _citation(self, row):
        return {'basis': 'Stata metadata (not participant observations)',
                'dataset_id': row['dataset_id'], 'variable_name': row['name'],
                'archive_path': row['path'], 'member': row['member'],
                'member_sha256': row['sha256'], 'source_url': row['source_url']}

    def _variable(self, row, full=False):
        codes = json.loads(row['value_codes'])
        item = {k: row[k] for k in ['dataset_id', 'name', 'label', 'storage_type',
                                    'year', 'wave', 'group_name', 'namespace', 'module', 'member']}
        item.update(value_label_set=row['value_label_set'], value_code_count=len(codes) if codes is not None else None,
                    coding_status=row['coding_status'],
                    citation=self._citation(row))
        if full:
            item['value_codes'] = codes
        return item

    SELECT = '''SELECT v.*,d.group_name,d.namespace,d.module,d.member,d.sha256,
                r.path,r.source_url FROM data_variables v JOIN datasets d USING(dataset_id)
                JOIN resources r USING(resource_id)'''

    def get_research_overview(self):
        groups = []
        for group in self.con.execute('SELECT DISTINCT group_name FROM resources ORDER BY group_name'):
            name = group[0]
            datasets = self.con.execute('SELECT * FROM datasets WHERE group_name=? ORDER BY module,dataset_id', (name,)).fetchall()
            groups.append({'group': name, 'namespace': datasets[0]['namespace'] if datasets else None,
                           'dataset_files': len(datasets),
                           'variable_entries': self.con.execute('SELECT count(*) FROM data_variables v JOIN datasets d USING(dataset_id) WHERE d.group_name=?', (name,)).fetchone()[0],
                           'modules': sorted(set(d['module'] for d in datasets)),
                           'document_count': self.con.execute('SELECT count(*) FROM documents WHERE group_name=?',(name,)).fetchone()[0],
                           'next_tool_for_documents': 'list_research_documents'})
        return envelope(data={'groups': groups, 'main_panel_years': MAIN_YEARS,
            'harmonized_D_years': [2011, 2013, 2015, 2018],
            'next_tool_for_topic_discovery': 'list_indicator_topics',
            'schema_version': self.schema_version,
            'build_id': self.meta['build_id'], 'retrieval_revision': RETRIEVAL_REVISION,
            'aliases_sha256': self.alias_digest, 'evidence_coverage': self.evidence_coverage(),
            'document_trust_states': dict(self.con.execute('SELECT trust_status,count(*) FROM documents GROUP BY trust_status')) if self.schema_version>=4 else {'legacy_no_review_record':self.con.execute('SELECT count(*) FROM documents').fetchone()[0]},
            'document_formats': dict(self.con.execute('SELECT format,count(*) FROM documents GROUP BY format')) if self.schema_version>=4 else {'pdf':self.con.execute('SELECT count(*) FROM documents').fetchone()[0]},
            'document_policy': 'Quarantined documents excluded by default; list_research_documents(include_unverified=true) shows review flags. DOC locators are text segments, not physical pages.',
            'index_counts': {t: self.con.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
                             for t in ['resources', 'datasets', 'data_variables', 'documents', 'document_pages','raw_source_links']},
            'coding_states': dict(self.con.execute('SELECT coding_status,count(*) FROM data_variables GROUP BY coding_status')),
            'workflow': [
                '用本概览理解可用模块，提出可检验的研究问题，明确暴露、结局和时间顺序。',
                '对每个核心指标调用 find_indicator_across_years；用 search_data_variables 展开候选。',
                '用 get_data_variable 核对标签和编码；get_variable_evidence 查看文档定位及核实定义，compare_variables 核对跨年待办。',
                'search_all_codebooks + read_codebook_page 核对问法、回忆期、检测方法、样本范围和构建；存疑资料不能当作已核实定义。',
                '建立变量×年份证据表；只有指标定义与样本范围兼容时才建议纵向分析。',
                '区分研究假设和已证实的数据事实；样本数、缺失率、分布、ID合并质量须在分析阶段从实际数据计算。',
                '给出主分析、混杂控制、时间顺序、反向因果/失访/缺失及敏感性分析方案；论证只限已有证据。'],
            'metadata_adapter': self.meta['metadata_adapter'], 'participant_rows_indexed': 0},
            warnings=['本概览的模块存在不保证模块中所有概念均被测量。',
                      '2008/2012 pilot、2014 life history、退出访谈与主追踪调查须分别考虑样本和测量时点。',
                      'Harmonized Version D 不含 2020；不能自动延伸为第五轮。',
                      'CHARLS 研究方案应根据观测数据及其假设设计；本工具不会把观测关联当作已确立的干预效应。'])

    def list_indicator_topics(self, namespace='raw', limit=10, cursor=None):
        if namespace not in {'raw', 'harmonized', 'pilot', 'life_history'}:
            raise ValueError('namespace 必须为 raw/harmonized/pilot/life_history')
        if type(limit) is not int or not 1 <= limit <= 15:
            raise ValueError('limit 必须为 1–15')
        context = {'kind': 'topics', 'build': self.meta['build_id'], 'namespace': namespace,
                   'limit': limit, 'aliases': self.aliases}
        # Store a small digest instead of embedding the entire synonym dictionary
        # in the returned continuation token.
        import hashlib
        context['aliases'] = hashlib.sha256(json.dumps(self.aliases,sort_keys=True).encode()).hexdigest()
        at = self._offset(cursor, context)
        if at > len(self.aliases):
            raise QueryError('invalid_cursor', '游标超出主题目录')
        topics = []
        for alias in self.aliases[at:at+limit]:
            parts = self._search_parts(alias['term'], namespace=namespace)
            where, args = self._where_for(parts)
            rows = self.con.execute('SELECT v.year,count(*) AS candidate_count FROM data_variables v JOIN datasets d USING(dataset_id) WHERE ' + where + ' GROUP BY v.year ORDER BY v.year', args).fetchall()
            topics.append({'topic': alias['term'], 'retrieval_terms': parts['terms']['effective'],
                           'candidate_years': [dict(r) for r in rows],
                           'next_step': 'find_indicator_across_years(query='+alias['term']+')',
                           'measurement_confirmed': False})
        more = at+len(topics) < len(self.aliases)
        return envelope(data={'namespace': namespace, 'topics': topics, 'total_topics': len(self.aliases)},
                        warnings=['主题目录是透明关键词检索产生的候选分布，不能直接视为已经验证的指标可用性。', CAUTION],
                        truncated=more, next_cursor=self._cursor(context, at+len(topics)) if more else None)

    def list_datasets(self, group=None, namespace=None, limit=20, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 40:
            raise ValueError('limit 必须为 1–40')
        where, args = self._filters(group, namespace)
        context = {'kind': 'datasets', 'build': self.meta['build_id'], 'group': group,
                   'namespace': namespace, 'limit': limit}
        at = self._offset(cursor, context)
        clause = (' WHERE ' + ' AND '.join(where)) if where else ''
        rows = self.con.execute('SELECT d.*,r.path AS archive_path,r.source_url FROM datasets d JOIN resources r USING(resource_id)' + clause + ' ORDER BY d.group_name,d.module,d.dataset_id LIMIT ? OFFSET ?', args + [limit+1, at]).fetchall()
        results = []
        for row in rows[:limit]:
            item = dict(row)
            if results and encoded_size(results+[item]) > 10000:
                break
            results.append(item)
        more = len(rows) > len(results)
        return envelope('ok' if results else 'no_results',
                        {'datasets': results, 'wave_basis': WAVE_BASIS},
                        warnings=['nobs 是各文件记录数，不能相加作为研究样本量；尚未检查 ID 唯一性与合并关系。',
                                  'wave 为目录库内部编号，不等于官方波次编号；引用前请核对官方文档。'],
                        truncated=more, next_cursor=self._cursor(context, at+len(results)) if more else None)

    FIELDS = ('brief', 'standard', 'full')

    def _dataset_variable_item(self, row, fields):
        """Per-variable item. 'standard' keeps only variable-level fields; dataset-level
        fields (dataset_id, group, year, wave, module, member, hashes, paths) are repeated
        once in response.data.dataset instead, which keeps a whole file inside the cap."""
        if fields == 'brief':
            return {k: row[k] for k in ['name', 'label', 'storage_type', 'coding_status']}
        ordinal = row['ordinal'] if 'ordinal' in row.keys() else None
        if fields == 'standard':
            codes = json.loads(row['value_codes'])
            item = {'name': row['name'], 'label': row['label'], 'storage_type': row['storage_type'],
                    'coding_status': row['coding_status'], 'value_label_set': row['value_label_set'],
                    'value_code_count': len(codes) if codes is not None else None,
                    'citation': {'basis': 'Stata metadata (not participant observations)',
                                 'variable_name': row['name'],
                                 'provenance_reference': 'response.data.dataset (dataset_id, member, archive_path, sha256, source_url)'}}
            if ordinal is not None:
                item['ordinal'] = ordinal
            return item
        item = self._variable(row)
        if ordinal is not None:
            item['ordinal'] = ordinal
        return item

    def list_dataset_variables(self, dataset_id, limit=50, cursor=None, coding_status=None,
                               name_prefix=None, fields='standard', order='name'):
        """List one .dta file's variables from Stata header metadata (no observations)."""
        if not isinstance(dataset_id, str) or not re.fullmatch(r'ds_[a-f0-9]{24}', dataset_id):
            raise ValueError('dataset_id 无效；先 list_datasets/search_data_variables')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit 必须为 1–100')
        if coding_status is not None and coding_status not in {'labelled', 'unlabelled', 'unavailable', 'missing_label_table'}:
            raise ValueError('coding_status 必须为 labelled/unlabelled/unavailable/missing_label_table')
        if fields not in self.FIELDS:
            raise ValueError('fields 必须为 brief/standard/full')
        if name_prefix is not None and (not isinstance(name_prefix, str) or not 0 < len(name_prefix) <= 32):
            raise ValueError('name_prefix 必须为 1–32 字符')
        if order not in ('name', 'file'):
            raise ValueError('order 必须为 name 或 file')
        head = self.con.execute('SELECT d.*,r.path AS archive_path,r.source_url FROM datasets d JOIN resources r USING(resource_id) WHERE d.dataset_id=?', (dataset_id,)).fetchone()
        if not head:
            return envelope('no_results', {'dataset_id': dataset_id, 'variables': [], 'variable_count': 0},
                            warnings=[NO_MATCH, '未知 dataset_id；先 list_datasets 或 search_data_variables。'])
        context = {'kind': 'dataset_variables', 'build': self.meta['build_id'], 'dataset': dataset_id,
                   'coding_status': coding_status, 'name_prefix': name_prefix, 'fields': fields,
                   'order': order, 'limit': limit}
        at = self._offset(cursor, context)
        clause, args = ' WHERE v.dataset_id=?', [dataset_id]
        if coding_status is not None:
            clause += ' AND v.coding_status=?'
            args.append(coding_status)
        if name_prefix is not None:
            clause += " AND v.name LIKE ? ESCAPE '\\'"
            args.append(escape_like(name_prefix) + '%')
        select = self.SELECT
        if self.has_ordinal:
            select = select.replace(' FROM ', ', (SELECT c.ordinal FROM dataset_columns c WHERE c.dataset_id=v.dataset_id AND c.name=v.name) AS ordinal FROM ', 1)
        ordinal_available = False
        order_sql = 'v.name'
        if order == 'file' and self.has_ordinal:
            ordinal_available = bool(self.con.execute(
                'SELECT 1 FROM dataset_columns WHERE dataset_id=? LIMIT 1', (dataset_id,)).fetchone())
            if ordinal_available:
                order_sql = 'CASE WHEN ordinal IS NULL THEN 1 ELSE 0 END, ordinal, v.name'
        rows = self.con.execute(select + clause + ' ORDER BY ' + order_sql + ' LIMIT ? OFFSET ?',
                                args + [limit+1, at]).fetchall()
        results = []
        for row in rows[:limit]:
            item = self._dataset_variable_item(row, fields)
            if results and encoded_size(results + [item]) > 10000:
                break
            results.append(item)
        more = len(rows) > len(results)
        matched = self.con.execute('SELECT count(*) FROM data_variables v' + clause, args).fetchone()[0]
        states = {r[0]: r[1] for r in self.con.execute(
            'SELECT coding_status,count(*) FROM data_variables WHERE dataset_id=? GROUP BY coding_status', (dataset_id,))}
        dataset = {k: head[k] for k in ['dataset_id', 'group_name', 'namespace', 'year', 'wave', 'module',
                                        'member', 'nobs', 'nvar', 'stata_version', 'data_label', 'sha256',
                                        'archive_path', 'source_url']}
        return envelope('ok' if results else 'no_results',
                        {'dataset': dataset, 'variables': results, 'variable_count': matched,
                         'dataset_variable_count': sum(states.values()),
                         'coding_states': states, 'offset': at, 'fields': fields,
                         'order_requested': order, 'ordinal_available': ordinal_available,
                         'schema_version': self.schema_version, 'wave_basis': WAVE_BASIS,
                         'order': ('file column order (ordinal, ascending)' if ordinal_available
                                   else 'variable name (ascending)')},
                        warnings=[CAUTION,
                                  '本清单来自 .dta 头部元数据，不含观测值。',
                                  'nobs 是该文件的记录数，不能当作研究分析样本量，也不能跨模块相加。',
                                  'fields=standard 只回精简溯源；归档路径、成员哈希与来源 URL 见 response.data.dataset；需要逐变量完整 citation 时传 fields="full"。',
                                  'variable_count 为当前 coding_status/name_prefix 过滤下的命中数；dataset_variable_count 与 coding_states 恒为该文件整体。'] +
                                 (['order="file" 需要 schema 3 的 dataset_columns 列序数据；本库没有该数据，已按变量名返回。'] if (order == 'file' and not ordinal_available) else []),
                        truncated=more, next_cursor=self._cursor(context, at+len(results)) if more else None)

    def search_data_variables(self, query, group=None, namespace=None, year=None, limit=10, cursor=None, expand=None,
                              fields='standard', match_mode='auto', module=None):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError('limit 必须为 1–20')
        check_fields(fields)
        parts = self._search_parts(query, group, namespace, year, expand, match_mode, module)
        terms = parts['terms']
        where, args = self._where_for(parts)
        literal_where = ' AND '.join(parts['filters'] + ['(' + parts['literal_sql'] + ')'])
        literal_args = parts['filter_args'] + parts['literal_args']
        total_count = self.con.execute('SELECT count(*) FROM data_variables v JOIN datasets d USING(dataset_id) WHERE ' + where, args).fetchone()[0]
        literal_count = self.con.execute('SELECT count(*) FROM data_variables v JOIN datasets d USING(dataset_id) WHERE ' + literal_where, literal_args).fetchone()[0]
        context = {'kind': 'variables', 'build': self.meta['build_id'], 'query': query,
                   'group': group, 'namespace': namespace, 'year': year, 'limit': limit,
                   'expand': expand, 'terms': terms['effective'], 'fields': fields,
                   'match_mode': match_mode, 'module': module}
        at = self._offset(cursor, context)
        evidence_order = ("CASE WHEN EXISTS(SELECT 1 FROM reviewed_variable_facts f WHERE f.dataset_id=v.dataset_id AND f.name=v.name AND json_extract(f.fact,'$.evidence_fields.construct.status')='document_explicit') THEN 0 ELSE 1 END," if self.schema_version>=4 else '')
        rows = self.con.execute(self._select(parts['literal_sql']) + ' WHERE ' + where +
                                ' ORDER BY CASE WHEN v.name=? COLLATE NOCASE THEN 0 ELSE 1 END,is_literal DESC,'+evidence_order+'v.year,d.group_name,d.module,v.name,d.dataset_id LIMIT ? OFFSET ?',
                                parts['literal_args'] + args + [query, limit+1, at]).fetchall()
        results = []
        datasets = {}
        for row in rows[:limit]:
            item = self._variable(row)
            if fields != 'full':
                datasets[row['dataset_id']] = {'dataset_id': row['dataset_id'],
                    'group': row['group_name'], 'namespace': row['namespace'], 'module': row['module'],
                    'member': row['member'], 'citation': self._citation(row)}
                datasets[row['dataset_id']]['citation'].pop('variable_name', None)
                item.pop('member', None); item.pop('wave', None)
                item['citation'] = {'dataset_id': row['dataset_id'], 'variable_name': row['name'],
                                    'provenance_reference': 'response.data.datasets[dataset_id]'}
            item['match_tier'] = 'literal' if row['is_literal'] else 'expanded'
            strong_terms = terms['literal'] + terms['expanded']
            item['matched_fields'] = [field for field in ['name','label']
                                      if any(text_matches(row[field], t, match_mode)
                                             for t in (terms['effective'] if field=='name' else strong_terms))]
            matched_codes = [c for c in (json.loads(row['value_codes']) or [])
                             if any(text_matches(c['label'], t, match_mode) for t in strong_terms)]
            if matched_codes:
                item['matched_fields'].append('value_labels')
                item['matched_value_labels'] = matched_codes[:3]
            links = self.con.execute('SELECT topic_title,raw_label,citation FROM raw_source_links WHERE dataset_id=? AND name=? COLLATE NOCASE ORDER BY source_id',(row['dataset_id'],row['name'])).fetchall()
            selected=[link for link in links if any(text_matches(link['topic_title']+' '+link['raw_label'], t, match_mode) for t in strong_terms)]
            if selected:
                item['matched_fields'].append('documented_raw_source_list')
                item['definition_candidates']=[{'topic':s['topic_title'],'raw_label':s['raw_label'],
                                               'citation':json.loads(s['citation'])} for s in selected[:2]]
            if self.schema_version>=4:
                fact=self._reviewed_fact(row['dataset_id'],row['name'])
                item['reviewed_definition_available']=fact is not None
                if fact and any(text_matches(fact.get('construct','')+' '+fact.get('search_terms',''), t, match_mode) for t in strong_terms):
                    item['matched_fields'].append('reviewed_document_fact')
                item['document_reference_count']=self.con.execute('SELECT count(*) FROM variable_document_links WHERE dataset_id=? AND name=? COLLATE NOCASE',(row['dataset_id'],row['name'])).fetchone()[0]
                item['next_evidence_tool']='get_variable_evidence'
            if fields == 'brief':
                item = {k:v for k,v in item.items() if k in {'dataset_id','name','label','year','namespace','module','group_name','coding_status','value_code_count','matched_fields','match_tier','reviewed_definition_available','document_reference_count','citation'}}
            results.append(item)
        more = len(rows) > len(results)
        response = envelope('ok' if results else 'no_results',
                        {'results': results, 'effective_terms': terms['effective'],
                         'literal_terms': terms['literal'], 'expanded_terms': terms['expanded'],
                         'weak_terms': terms['weak'], 'expansion_hints': terms['hints'],
                         'expansions': terms['expansions'],
                         'literal_match_count': literal_count, 'expanded_match_count': total_count},
                        warnings=[CAUTION] + ([] if results else [NO_MATCH]) +
                                 (['未按概念扩展；如需扩大检索请显式设置 expand=true。'] if terms['hints'] else []),
                        truncated=more,
                        next_cursor=self._cursor(context, at+len(results)) if more and results else None)

        response['data'].update(fields=fields, match_mode=match_mode,
                                retrieval_revision=RETRIEVAL_REVISION)
        if fields != 'full':
            response['data']['datasets'] = datasets
        return finish_page(response, 'results', limit, at, total_count,
                           lambda offset:self._cursor(context, offset),
                           ('datasets', 'dataset_id') if fields != 'full' else None)

    def get_data_variable(self, dataset_id, variable_name, limit=40, cursor=None):
        if not isinstance(dataset_id, str) or not re.fullmatch(r'ds_[a-f0-9]{24}', dataset_id):
            raise ValueError('dataset_id 无效；先 list_datasets/search_data_variables')
        if not isinstance(variable_name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', variable_name):
            raise ValueError('variable_name 无效')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit 必须为 1–100')
        row = self.con.execute(self.SELECT + ' WHERE v.dataset_id=? AND v.name=? COLLATE NOCASE', (dataset_id, variable_name)).fetchone()
        if not row:
            return envelope('no_results', warnings=[NO_MATCH])
        context = {'kind': 'codes', 'build': self.meta['build_id'], 'dataset': dataset_id,
                   'variable': row['name'], 'limit': limit}
        at = self._offset(cursor, context)
        codes = json.loads(row['value_codes'])
        if codes is None:
            if cursor is not None:
                raise QueryError('invalid_cursor','没有可靠编码可分页')
            item = self._variable(row)
            item['value_codes'] = None
            return envelope('needs_review',data=item,citations=[self._citation(row)],
                            warnings=['此文件的取值标签无法可靠解析或引用的标签表缺失；保留 null，须核对官方 codebook/问卷。',CAUTION])
        if at > len(codes):
            raise QueryError('invalid_cursor', '游标超出编码长度')
        item = self._variable(row)
        item['reviewed_definition_available']=self._reviewed_fact(dataset_id,row['name']) is not None
        item['next_evidence_tool']='get_variable_evidence'
        links=self.con.execute('SELECT topic_title,citation FROM raw_source_links WHERE dataset_id=? AND name=? COLLATE NOCASE ORDER BY source_id',(dataset_id,row['name'])).fetchall()
        item['documented_source_topics']=[{'topic':link['topic_title'],'citation':json.loads(link['citation'])} for link in links[:3]]
        item['documented_source_topic_count']=len(links)
        selected = []
        for code in codes[at:at+limit]:
            if selected and encoded_size(selected+[code]) > 8000:
                break
            selected.append(code)
        item.update(value_codes=selected, code_offset=at,
                    missing_value_interpretation='not inferred from labels; verify codebook and observed Stata missing values')
        more = at+len(selected) < len(codes)
        return envelope(data=item, citations=[self._citation(row)], warnings=[CAUTION,
                        '取值标签来自 .dta 的值标签表，不代表全部观测值，也不代表已判定特殊缺失或分数构建。'],
                        truncated=more, next_cursor=self._cursor(context, at+len(selected)) if more else None)

    def find_indicator_across_years(self, query, namespace='raw', max_per_year=3, expand=None, match_mode='auto'):
        if namespace not in {'raw', 'harmonized', 'pilot', 'life_history'}:
            raise ValueError('namespace 必须为 raw/harmonized/pilot/life_history')
        if type(max_per_year) is not int or not 1 <= max_per_year <= 5:
            raise ValueError('max_per_year 必须为 1–5')
        terms = self._terms(query, expand)
        years = MAIN_YEARS if namespace in {'raw', 'harmonized'} else [2008, 2012] if namespace == 'pilot' else [2014]
        rows = []
        for year in years:
            parts = self._search_parts(query, namespace=namespace, year=year, expand=expand, match_mode=match_mode)
            where, args = self._where_for(parts)
            count = self.con.execute('SELECT count(*) FROM data_variables v JOIN datasets d USING(dataset_id) WHERE ' + where, args).fetchone()[0]
            literal_where = ' AND '.join(parts['filters'] + ['(' + parts['literal_sql'] + ')'])
            literal_count = self.con.execute('SELECT count(*) FROM data_variables v JOIN datasets d USING(dataset_id) WHERE ' + literal_where, parts['filter_args'] + parts['literal_args']).fetchone()[0]
            result = self.search_data_variables(query, namespace=namespace, year=year, limit=max_per_year, expand=expand,match_mode=match_mode)
            candidates = []
            for match in result['data']['results']:
                candidate = {k: match[k] for k in ['dataset_id','name','label','module','group_name','value_code_count']}
                candidate['matched_fields']=match['matched_fields']
                candidate['evidence_basis'] = 'candidate from metadata or explicit documented source list; read definition before choosing a measurement'
                fact=self._reviewed_fact(match['dataset_id'],match['name'])
                if fact:
                    candidate['reviewed_definition']={k:fact.get(k) for k in ['construct','construct_key','unit']}
                    from .indicator_evidence import definition_summary
                    summary=definition_summary(fact)
                    candidate['field_statuses']={k:v['status'] for k,v in summary['field_evidence'].items()}
                    candidate['presence_status']=summary['presence_status']
                    candidate['definition_locators']=[{'document_id':s['document_id'],'locator':s['page'],'locator_type':s['locator_type']} for s in fact['sources']]
                candidate['next_evidence_tool']='get_variable_evidence'
                candidates.append(candidate)
            group = {2008:'2008-Pilot',2012:'2012-Pilot-Wave2',2014:'2014-Life-History',
                     2011:'2011-Wave1',2013:'2013-Wave2',2015:'2015',2018:'2018-Wave4',2020:'2020-Wave5'}.get(year)
            document_matches = []
            if namespace != 'harmonized':
                docs = self.search_all_codebooks(query,group=group,limit=1,expand=expand,match_mode=match_mode)
                for hit in docs['data']['results']:
                    docmatch={k:hit[k] for k in ['document_id','pdf_page_number','title','source_kind','locator_type','locator','trust_status']}
                    docmatch['snippet']=hit['snippet'][:160]
                    document_matches.append(docmatch)
            rows.append({'year': year, 'status': 'candidate_found' if count else 'documentation_candidate' if document_matches else 'no_index_match',
                         'presence_status':'confirmed_present' if any(c.get('presence_status')=='confirmed_present' for c in candidates) else 'candidate_only' if count else 'no_index_match',
                         'candidate_count': count, 'literal_match_count': literal_count, 'candidates': candidates,
                         'documentation_candidates': document_matches,
                         'more_candidates': count > len(candidates)})
        reference = []
        if namespace == 'raw' and any(row['status'] != 'candidate_found' for row in rows):
            docs = self.search_all_codebooks(query,group='Harmonized',limit=1,expand=expand,match_mode=match_mode)
            reference = [{k:hit[k] for k in ['document_id','pdf_page_number','title','source_kind','snippet']}
                         for hit in docs['data']['results']]
        budget_truncated=False
        while encoded_size(rows)>6800 and any(len(r['candidates'])>1 for r in rows):
            largest=max((r for r in rows if len(r['candidates'])>1),key=lambda r:encoded_size(r['candidates']))
            largest['candidates'].pop();largest['more_candidates']=True;largest['candidate_truncation_reason']='byte_budget';budget_truncated=True
        for row in rows:
            row['returned_candidates']=len(row['candidates'])
            if row['more_candidates']:
                row.setdefault('candidate_truncation_reason','limit')
                row['next_lookup']={'tool':'search_data_variables','arguments':{
                    'query':query,'namespace':namespace,'year':row['year'],
                    'expand':expand,'match_mode':match_mode,'fields':'brief'}}
            row['presence_status']='confirmed_present' if any(c.get('presence_status')=='confirmed_present' for c in row['candidates']) else 'candidate_only' if row['candidate_count'] else 'no_index_match'
        return envelope(truncated=budget_truncated,data={'query': query, 'namespace': namespace, 'year_matrix': rows,
                              'effective_terms': terms['effective'], 'literal_terms': terms['literal'],
                              'expanded_terms': terms['expanded'], 'weak_terms': terms['weak'],
                              'expansion_hints': terms['hints'], 'expansions': terms['expansions'],
                              'related_harmonized_documentation': reference,
                              'comparability': 'not established; confirm each candidate with its codebook'},
                        warnings=[CAUTION, NO_MATCH,
                                  'candidate_count 包括重复发布文件、多个条目和本人/配偶等变量，不是独立指标数。',
                                  '文档来源清单中的变量可能是测量值、资格、访谈状态或构建输入；不能全部当作独立测量指标。',
                                  'documentation_candidate 表示文档提及，尚未定位到实际数据变量；需读原文再核对变量。'])

