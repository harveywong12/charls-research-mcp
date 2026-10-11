"""Field-level evidence and bounded study feasibility. No observations or LLM calls."""
import hashlib
import json
from .query import envelope
from .presentation import finish_page, RETRIEVAL_REVISION

EVIDENCE_STATES = ('document_explicit', 'inferred', 'unconfirmed', 'conflict')
EVIDENCE_FIELDS = ('construct', 'measurement_type', 'unit', 'coding', 'recall_period',
                   'eligible_population', 'construction', 'timing', 'sampling')
COMPARISON_FIELDS = ('construct', 'measurement_type', 'unit', 'coding', 'recall_period',
                     'eligible_population', 'construction', 'timing', 'sampling')


def field_evidence(fact):
    """Old whole-record review is retained, never promoted to field-level certainty."""
    recorded = (fact or {}).get('evidence_fields', {})
    result = {}
    for field in EVIDENCE_FIELDS:
        value = recorded.get(field)
        if value:
            result[field] = value
        else:
            result[field] = {'value': (fact or {}).get(field), 'status': 'unconfirmed',
                             'source_indexes': [], 'reason': ('旧版审核未细分本字段；保留原值，须对照来源后升级状态'
                                                            if fact else '尚无逐字段核实记录')}
    return result


def definition_summary(fact):
    fields = field_evidence(fact)
    return {'construct': (fact or {}).get('construct'),
            'construct_key': (fact or {}).get('construct_key'),
            'unit': (fact or {}).get('unit'),
            'field_evidence': fields,
            'presence_status': 'confirmed_present' if fields['construct']['status'] == 'document_explicit'
                               else 'candidate_only',
            'definition_completeness': sum(f['status'] == 'document_explicit' for f in fields.values()),
            'unconfirmed_fields': [k for k,v in fields.items() if v['status'] != 'document_explicit']}


class IndicatorEvidence:
    def evidence_coverage(self):
        rows = self.con.execute('SELECT fact FROM reviewed_variable_facts').fetchall() if self.schema_version >= 4 else []
        facts = [json.loads(row[0]) for row in rows]
        by_year, fields, constructs = {}, {}, set()
        for fact in facts:
            year = str(fact['year'])
            by_year[year] = by_year.get(year, 0) + 1
            constructs.add(fact.get('construct_key'))
            for key, item in field_evidence(fact).items():
                counts = fields.setdefault(key, {status:0 for status in EVIDENCE_STATES})
                counts[item['status']] += 1
        return {'reviewed_variable_records':len(facts), 'records_by_year':by_year,
                'construct_count':len(constructs-{None}), 'field_status_counts':fields,
                'exact_name_references_are_definitions':False,
                'review_scope':'model-assisted source review; no human adjudication implied'}

    def _indicator_year(self, query, namespace, year, max_per_year=1, expand=None, match_mode='auto'):
        result = self.search_data_variables(query, namespace=namespace, year=year,
                                            limit=max_per_year, expand=expand, fields='brief',
                                            match_mode=match_mode)
        candidates = []
        for variable in result['data']['results']:
            fact = self._reviewed_fact(variable['dataset_id'], variable['name'])
            item = {k:variable[k] for k in ('dataset_id','name','label','module','year','namespace')}
            if fact:
                summary = definition_summary(fact)
                item.update(presence_status=summary['presence_status'],
                            construct_key=summary['construct_key'],
                            confirmed_fields={k:v['value'] for k,v in summary['field_evidence'].items()
                                              if v['status']=='document_explicit'},
                            unconfirmed_fields=summary['unconfirmed_fields'],
                            definition_locators=[{k:source[k] for k in ('document_id','locator_type','start_char','end_char')}
                                                 | {'locator':source['page']} for source in fact['sources']])
            else:
                item.update(presence_status='candidate_only', confirmed_fields={},
                            unconfirmed_fields=list(EVIDENCE_FIELDS), definition_locators=[])
            candidates.append(item)
        count = result['data']['expanded_match_count']
        return {'year':year, 'candidate_count':count, 'candidates':candidates,
                'status':'confirmed_present' if any(c['presence_status']=='confirmed_present' for c in candidates)
                         else 'candidate_only' if count else 'no_index_match',
                'more_candidates':count>len(candidates), 'absence_established':False}

    def assess_research_feasibility(self, indicators, years, namespace='raw', limit=2, cursor=None):
        """Batch a variable evidence ledger; overlap is not an analytic sample."""
        if type(indicators) is not list or not 1 <= len(indicators) <= 12:
            raise ValueError('indicators 为 1–12 个 {query,role} 对象')
        for item in indicators:
            if type(item) is not dict or set(item) != {'query','role'}:
                raise ValueError('每个指标必须且只能包含 query 和 role')
            if item['role'] not in ('exposure','outcome','covariate'):
                raise ValueError('role 必须为 exposure/outcome/covariate')
            self._terms(item['query'])
        if type(years) is not list or not 1 <= len(years) <= 8 or any(type(y) is not int for y in years) or len(set(years)) != len(years):
            raise ValueError('years 为 1–8 个不重复年份')
        allowed = {'raw':{2011,2013,2015,2018,2020}, 'harmonized':{2011,2013,2015,2018},
                   'pilot':{2008,2012}, 'life_history':{2014}}
        if namespace not in allowed or any(type(y) is not int or y not in allowed[namespace] for y in years):
            raise ValueError('years 与 namespace 不兼容；Harmonized D 不含 2020')
        if type(limit) is not int or not 1 <= limit <= 3:
            raise ValueError('limit 必须为 1–3 个指标；使用 next_cursor 续查完整批次')
        years = sorted(years)
        digest = hashlib.sha256(json.dumps(indicators,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        context = {'kind':'feasibility','build':self.meta['build_id'],'indicators_sha256':digest,
                   'years':years,'namespace':namespace,'limit':limit}
        at = self._offset(cursor, context)
        if at > len(indicators):
            raise ValueError('游标超出指标清单')
        results = []
        common_candidates, common_confirmed = set(years), set(years)
        # Small bounded batch: global overlap uses every supplied indicator, even when
        # only a page of detailed results fits. No participant linkage is implied.
        for index, spec in enumerate(indicators):
            matrix = [self._indicator_year(spec['query'], namespace, year) for year in years]
            common_candidates &= {r['year'] for r in matrix if r['candidate_count']}
            common_confirmed &= {r['year'] for r in matrix if r['status']=='confirmed_present'}
            if at <= index < at + limit:
                results.append({**spec, 'year_matrix':matrix,
                                'candidate_selection':'requires researcher review; first hit is not automatically selected'})
        response = envelope(data={'indicators':results, 'total_indicators':len(indicators),
            'namespace':namespace, 'years':years, 'common_candidate_years':sorted(common_candidates),
            'common_document_confirmed_years':sorted(common_confirmed),
            'cross_year_comparability':'not_established', 'analysis_ready':False,
            'constraints':['明确暴露与结局测量顺序、回忆期及研究假设',
                           '核对受访者/配偶、主追踪/退出访谈、代答与纳入条件',
                           '核对构建、特殊缺失、权重及跨年测量差异',
                           '分析阶段从实际数据确认 ID 合并、样本量、缺失及失访'],
            'build_id':self.meta['build_id'],'retrieval_revision':RETRIEVAL_REVISION},
            warnings=['共同年份仅表示候选或文档证据重叠，不证明同一批受访者均有测量。',
                      '未命中不证明未收集；推断、冲突和未确认字段不能作为已核实设计依据。'])
        return finish_page(response,'indicators',limit,at,len(indicators),
                           lambda offset:self._cursor(context,offset))
