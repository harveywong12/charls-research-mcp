"""Local research catalogue builder; never imported by the running MCP server.

Indexes Stata metadata, not observations. ZIP/RAR members stay in a temporary
directory. Source archives are read only and published databases are never replaced.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile

SCHEMA4 = '''
PRAGMA user_version=4;
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE resources(resource_id TEXT PRIMARY KEY,group_name TEXT,kind TEXT,label TEXT,
 path TEXT,source_url TEXT,sha256 TEXT,bytes INTEGER,status TEXT,error TEXT);
CREATE TABLE datasets(dataset_id TEXT PRIMARY KEY,resource_id TEXT,group_name TEXT,
 namespace TEXT,year INTEGER,wave INTEGER,module TEXT,member TEXT,sha256 TEXT,
 nobs INTEGER,nvar INTEGER,stata_version INTEGER,data_label TEXT);
CREATE TABLE data_variables(dataset_id TEXT,name TEXT COLLATE NOCASE,label TEXT,
 storage_type TEXT,value_label_set TEXT,value_codes TEXT,year INTEGER,wave INTEGER,coding_status TEXT,
 PRIMARY KEY(dataset_id,name));
CREATE INDEX variable_name ON data_variables(name COLLATE NOCASE);
CREATE INDEX variable_year ON data_variables(year,dataset_id);
CREATE TABLE raw_source_links(dataset_id TEXT,name TEXT COLLATE NOCASE,topic_title TEXT,
 source_id TEXT,document_id TEXT,year INTEGER,raw_label TEXT,citation TEXT,
 PRIMARY KEY(dataset_id,name,source_id));
CREATE INDEX source_link_lookup ON raw_source_links(dataset_id,name);
CREATE TABLE dataset_columns(dataset_id TEXT NOT NULL,name TEXT NOT NULL COLLATE NOCASE,
 ordinal INTEGER NOT NULL, PRIMARY KEY(dataset_id,name));
CREATE TABLE documents(document_id TEXT PRIMARY KEY,resource_id TEXT,group_name TEXT,
 member TEXT,title TEXT,sha256 TEXT,page_count INTEGER,format TEXT,locator_type TEXT,
 trust_status TEXT,review_issues TEXT);
CREATE TABLE document_sources(document_id TEXT,source_id TEXT,resource_id TEXT,claimed_group TEXT,
 source_url TEXT,label TEXT,provenance TEXT,PRIMARY KEY(document_id,source_id));
CREATE INDEX source_group ON document_sources(claimed_group,document_id);
CREATE TABLE variable_document_links(dataset_id TEXT,name TEXT COLLATE NOCASE,document_id TEXT,
 page INTEGER,start_char INTEGER,end_char INTEGER,basis TEXT,
 PRIMARY KEY(dataset_id,name,document_id,page));
CREATE INDEX variable_document_lookup ON variable_document_links(dataset_id,name);
CREATE TABLE reviewed_variable_facts(dataset_id TEXT,name TEXT COLLATE NOCASE,fact TEXT,
 PRIMARY KEY(dataset_id,name));
CREATE TABLE document_pages(document_id TEXT,page INTEGER,text TEXT,state TEXT,
 PRIMARY KEY(document_id,page));
CREATE VIRTUAL TABLE document_fts USING fts5(document_id UNINDEXED,page UNINDEXED,text);
'''
# Retain the schema-3 fixture/API constant for existing callers. Production builds use SCHEMA4.
SCHEMA = '''
PRAGMA user_version=3;
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE resources(resource_id TEXT PRIMARY KEY,group_name TEXT,kind TEXT,label TEXT,
 path TEXT,source_url TEXT,sha256 TEXT,bytes INTEGER,status TEXT,error TEXT);
CREATE TABLE datasets(dataset_id TEXT PRIMARY KEY,resource_id TEXT,group_name TEXT,
 namespace TEXT,year INTEGER,wave INTEGER,module TEXT,member TEXT,sha256 TEXT,
 nobs INTEGER,nvar INTEGER,stata_version INTEGER,data_label TEXT);
CREATE TABLE data_variables(dataset_id TEXT,name TEXT COLLATE NOCASE,label TEXT,
 storage_type TEXT,value_label_set TEXT,value_codes TEXT,year INTEGER,wave INTEGER,coding_status TEXT,
 PRIMARY KEY(dataset_id,name));
CREATE INDEX variable_name ON data_variables(name COLLATE NOCASE);
CREATE INDEX variable_year ON data_variables(year,dataset_id);
CREATE TABLE raw_source_links(dataset_id TEXT,name TEXT COLLATE NOCASE,topic_title TEXT,
 source_id TEXT,document_id TEXT,year INTEGER,raw_label TEXT,citation TEXT,
 PRIMARY KEY(dataset_id,name,source_id));
CREATE INDEX source_link_lookup ON raw_source_links(dataset_id,name);
CREATE TABLE dataset_columns(dataset_id TEXT NOT NULL,name TEXT NOT NULL COLLATE NOCASE,
 ordinal INTEGER NOT NULL, PRIMARY KEY(dataset_id,name));
CREATE TABLE documents(document_id TEXT PRIMARY KEY,resource_id TEXT,group_name TEXT,
 member TEXT,title TEXT,sha256 TEXT,page_count INTEGER);
CREATE TABLE document_pages(document_id TEXT,page INTEGER,text TEXT,state TEXT,
 PRIMARY KEY(document_id,page));
CREATE VIRTUAL TABLE document_fts USING fts5(document_id UNINDEXED,page UNINDEXED,text);
'''
YEAR_WAVE = {2011: 1, 2013: 2, 2015: 3, 2018: 4, 2020: 5}
MAX_MEMBER_BYTES = 1024**3


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def ident(prefix, *values):
    return prefix + hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()[:24]


def members(path):
    if path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as z:
            return [(i.filename, i.file_size) for i in z.infolist() if not i.is_dir()]
    if path.suffix.lower() == '.rar':
        tar = shutil.which('bsdtar')
        if not tar:
            raise ValueError('RAR_READER_MISSING: install bsdtar/libarchive')
        result = subprocess.run([tar, '-tf', str(path)], capture_output=True, text=True,
                                check=True, timeout=60)
        return [(n, None) for n in result.stdout.splitlines()]
    return [(path.name, path.stat().st_size)]


def extract_member(path, member, target):
    """Stream one named member to a fixed temporary filename; never extract paths."""
    if Path(member).suffix.lower() not in {'.pdf', '.dta', '.doc', '.docx'}:
        raise ValueError('unsupported member')
    if path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as z, z.open(member) as source, target.open('wb') as out:
            if z.getinfo(member).file_size > MAX_MEMBER_BYTES:
                raise ValueError('archive member exceeds 1 GiB')
            shutil.copyfileobj(source, out)
    elif path.suffix.lower() == '.rar':
        # Fixed argv; paths never pass through a shell. The archive is local and
        # allowlisted by its verified download manifest.
        with target.open('wb') as out:
            subprocess.run([shutil.which('bsdtar'), '-xOf', str(path), '--', member],
                           stdout=out, stderr=subprocess.PIPE, check=True, timeout=120)
    else:
        shutil.copyfile(path, target)
    if target.stat().st_size > MAX_MEMBER_BYTES:
        raise ValueError('archive member exceeds 1 GiB')


def group_metadata(group):
    if group == 'Harmonized':
        return 'harmonized', None, None
    year = int(group[:4])
    namespace = 'pilot' if 'Pilot' in group else 'life_history' if 'History' in group else 'raw'
    return namespace, year, YEAR_WAVE.get(year) if namespace == 'raw' else None


def index_stata(con, path, resource, member):
    import pyreadstat
    import pandas
    from pandas.io.stata import StataReader
    import re
    if pyreadstat.__version__ != '1.3.1':
        raise ValueError('METADATA_ADAPTER_VERSION: requires pyreadstat==1.3.1')
    if pandas.__version__ != '2.2.3':
        raise ValueError('METADATA_ADAPTER_VERSION: requires pandas==2.2.3')
    namespace, year, wave = group_metadata(resource['group'])
    sha = digest(path)
    dataset = ident('ds_', resource['resource_id'], member, sha)
    empty, meta = pyreadstat.read_dta(str(path), metadataonly=True, output_format='dict')
    if any(len(values) for values in empty.values()):
        raise ValueError('Metadata-only reader unexpectedly returned observations')
    with StataReader(path,convert_categoricals=False) as reader:
        labels = reader.variable_labels()
        if list(labels) != meta.column_names or reader._nobs != meta.number_rows:
            raise ValueError('Independent Stata header parsers disagree on names or row counts')
        label_sets = dict(zip(labels, reader._lbllist))
        # pandas respects byte-offset boundaries inside label tables; ReadStat
        # can concatenate adjacent labels in some old non-Unicode files. Keep
        # pandas' exact original text, including trailing spaces. Never silently
        # fall back to unconfirmed ReadStat labels if this parser fails.
        try:
            code_tables = reader.value_labels() if any(label_sets.values()) else {}
        except ValueError:
            code_tables = None
    with Path(path).open('rb') as f:
        header = f.read(160)
    version = int(re.search(rb'<release>(\d+)</release>', header)[1]) if header.startswith(b'<stata_dta>') else header[0]
    con.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (dataset, resource['resource_id'], resource['group'], namespace, year, wave,
                 PurePosixPath(member).stem, member, sha, meta.number_rows, meta.number_columns,
                 version, meta.file_label))
    for ordinal, name in enumerate(meta.column_names):
        label_set = label_sets[name]
        if not label_set:
            values=[];coding_status='unlabelled'
        elif code_tables is None:
            values=None;coding_status='unavailable'
        elif label_set not in code_tables:
            values=None;coding_status='missing_label_table'
        else:
            values=[{'value':format(k,'.15g') if isinstance(k,(int,float)) else str(k),'label':v}
                    for k,v in code_tables[label_set].items()]
            coding_status='labelled'
        variable_year, variable_wave = year, wave
        if namespace == 'harmonized' and PurePosixPath(member).name == 'H_CHARLS_D_Data.dta':
            found = re.match(r'^(?:R|S|HH|H|INW)([1-4])', name.upper())
            variable_wave = int(found[1]) if found else None
            variable_year = {1: 2011, 2: 2013, 3: 2015, 4: 2018}.get(variable_wave)
        con.execute('INSERT INTO data_variables VALUES(?,?,?,?,?,?,?,?,?)',
                    (dataset, name, labels[name],
                     meta.readstat_variable_types[name], label_set,
                     json.dumps(values, ensure_ascii=False), variable_year, variable_wave,coding_status))
        con.execute('INSERT INTO dataset_columns VALUES(?,?,?)', (dataset, name, ordinal))


def index_pdf(con, path, resource, member, legacy_db=None, cache_catalog=None):
    import pdfplumber
    from .document_ingest import add_document_sources
    sha = digest(path)
    document = ident('pdf_', sha)
    if con.execute('SELECT 1 FROM documents WHERE document_id=?', (document,)).fetchone():
        add_document_sources(con, document, resource, member)
        return
    cached = []
    if cache_catalog and Path(cache_catalog).is_file():
        cache = sqlite3.connect(Path(cache_catalog).resolve().as_uri()+'?mode=ro',uri=True)
        try:
            match = cache.execute('SELECT document_id FROM documents WHERE sha256=?',(sha,)).fetchone()
            if match:
                cached = cache.execute('SELECT page,text,state FROM document_pages WHERE document_id=? ORDER BY page',(match[0],)).fetchall()
        finally:
            cache.close()
    if legacy_db and Path(legacy_db).is_file():
        legacy = sqlite3.connect(Path(legacy_db).resolve().as_uri() + '?mode=ro', uri=True)
        try:
            for doc, manifest in legacy.execute('SELECT document_id,manifest FROM documents'):
                if not cached and json.loads(manifest).get('sha256') == sha:
                    cached = legacy.execute('SELECT pdf_page_number,raw_text,extraction_status FROM pages WHERE document_id=? ORDER BY pdf_page_number', (doc,)).fetchall()
        finally:
            legacy.close()
    with pdfplumber.open(path) as pdf:
        if cached and len(cached) != len(pdf.pages):
            raise ValueError('Cached document page count does not match source PDF')
        issues = resource.get('review_issues', [])
        con.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (document, resource['resource_id'], resource['group'], member,
                     resource['label'], sha, len(pdf.pages), 'pdf', 'pdf_physical_page',
                     'needs_review' if issues else 'verified_local', json.dumps(issues,ensure_ascii=False)))
        add_document_sources(con, document, resource, member)
        if not cached:
            for number, page in enumerate(pdf.pages, 1):
                text = page.extract_text(x_tolerance=3, y_tolerance=3) or ''
                state = 'text_extracted' if text.strip() else 'empty_no_ocr'
                if '\ufffd' in text or '(cid:' in text:
                    state = 'possible_garbled_text'
                con.execute('INSERT INTO document_pages VALUES(?,?,?,?)', (document, number, text, state))
                con.execute('INSERT INTO document_fts VALUES(?,?,?)', (document, number, text))
                page.close()
                if number % 100 == 0:
                    print(f'  {member}: {number}/{len(pdf.pages)} pages', file=sys.stderr, flush=True)
        else:
            cached = [(n,t,'text_extracted' if s=='parsed' else s) for n,t,s in cached]
            con.executemany('INSERT INTO document_pages VALUES(?,?,?,?)',
                            [(document, n, t, s) for n, t, s in cached])
            con.executemany('INSERT INTO document_fts VALUES(?,?,?)',
                            [(document, n, t) for n, t, _ in cached])


def index_raw_source_links(con, legacy_db):
    """Link explicit per-wave source lists to existing raw variables, not outputs.

    No regex guessing of constructed-to-raw equivalence. Every accepted name is
    listed under an explicit Wave 1..4 header AND exists in that year's raw file.
    """
    import re
    if not legacy_db or not Path(legacy_db).is_file():
        return
    old = sqlite3.connect(Path(legacy_db).resolve().as_uri()+'?mode=ro',uri=True)
    old.row_factory=sqlite3.Row
    try:
        for section in old.execute('SELECT * FROM sections ORDER BY ordinal'):
            body=section['body'];at=body.rfind('CHARLS Variables Used:')
            if at<0:continue
            manifest=json.loads(old.execute('SELECT manifest FROM documents WHERE document_id=?',(section['document_id'],)).fetchone()[0])
            document=ident('pdf_',manifest['sha256'])
            if not con.execute('SELECT 1 FROM documents WHERE document_id=?',(document,)).fetchone():continue
            wave=None;wave_citation=None;offset=at
            spans=json.loads(section['source_spans'])
            def locate(start,end):
                pos=0
                for span in spans:
                    length=span['end_char']-span['start_char']
                    if pos<=start and end<=pos+length:
                        return {'pdf_page_number':span['pdf_page_number'],
                                'start_char':span['start_char']+start-pos,
                                'end_char':span['start_char']+end-pos}
                    pos+=length+1
                return None
            for line in body[at:].splitlines(keepends=True):
                stripped=line.strip()
                heading=re.fullmatch(r'Wave (\d+):',stripped)
                if heading:
                    wave=int(heading[1]) if int(heading[1]) in {1,2,3,4} else None
                    wave_citation=locate(offset,offset+len(line.rstrip('\n'))) if wave else None
                else:
                    row=re.fullmatch(r'([A-Z][A-Z0-9_]{0,63})\s+(.+)',stripped)
                    if row and wave and wave_citation:
                        name,label=row.groups();year={1:2011,2:2013,3:2015,4:2018}[wave]
                        citation=locate(offset,offset+len(line.rstrip('\n')))
                        if citation:
                            raw=old.execute('SELECT raw_text FROM pages WHERE document_id=? AND pdf_page_number=?',
                                            (section['document_id'],citation['pdf_page_number'])).fetchone()[0]
                            if raw[citation['start_char']:citation['end_char']]!=line.rstrip('\n'):
                                raise ValueError('Source-list citation mismatch')
                            for dataset in con.execute('SELECT v.dataset_id,v.name FROM data_variables v JOIN datasets d USING(dataset_id) WHERE d.namespace=? AND v.year=? AND v.name=? COLLATE NOCASE',('raw',year,name)):
                                evidence={'document_id':document,'source_id':section['source_id'],
                                          **citation,'wave_header':wave_citation,'version':'D',
                                          'basis':'explicit CHARLS Variables Used source list; not a one-to-one derivation map'}
                                con.execute('INSERT OR IGNORE INTO raw_source_links VALUES(?,?,?,?,?,?,?,?)',
                                            (dataset['dataset_id'],dataset['name'],section['title'],section['source_id'],
                                             document,year,label,json.dumps(evidence,ensure_ascii=False)))
                offset+=len(line)
    finally:
        old.close()


def build_catalog(data_root, output, legacy_db=None, cache_catalog=None,
                  supplement_manifest=None, review_record=None, evidence_file=None):
    root = Path(data_root).resolve()
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Catalog already exists; choose a new output and switch after building')
    manifest_path = root / 'download-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    extra_path = root/'research-sources.json'
    if extra_path.is_file():
        supplement = json.loads(extra_path.read_text(encoding='utf-8'))
        manifest['items'] += supplement['items']
    if len({i['file'] for i in manifest['items']}) != len(manifest['items']):
        raise ValueError('Duplicate resource paths in source manifests')
    from .document_ingest import load_supplements, index_word, index_variable_mentions, index_reviewed_facts
    manifest = load_supplements(root, manifest, supplement_manifest, review_record)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, scratch = tempfile.mkstemp(prefix='.catalog-building-', suffix='.sqlite', dir=output.parent)
    os.close(fd)
    con = sqlite3.connect(scratch)
    con.row_factory = sqlite3.Row
    errors = []
    try:
        con.executescript(SCHEMA4)
        def input_fingerprint(path):
            """Audit trail for private build inputs: basename plus sha256, never absolute paths."""
            if not path:
                return None
            candidate = Path(path)
            if not candidate.is_file():
                return None
            return {'name': candidate.name, 'sha256': digest(candidate)}
        build_inputs = {name: value for name, value in (
            ('supplement_manifest', input_fingerprint(supplement_manifest)),
            ('review_record', input_fingerprint(review_record)),
            ('evidence_file', input_fingerprint(evidence_file)),
            ('source_manifest', input_fingerprint(manifest_path)),
            ('legacy_db', input_fingerprint(legacy_db))) if value}
        con.executemany('INSERT INTO meta VALUES(?,?)', [
            ('source_root', str(root)), ('manifest_sha256', digest(manifest_path)),
            ('metadata_adapter', 'pyreadstat==1.3.1 metadata-only headers cross-checked with pandas==2.2.3; byte-bounded pandas labels; uncertain codes null; no observations'),
            ('build_inputs', json.dumps(build_inputs, ensure_ascii=False, sort_keys=True)),
            ('published', '0'), ('build_id', ident('cat_', manifest, 'v4-supplementary-evidence',
                                                  digest(__file__),
                                                  digest(Path(__file__).with_name('document_ingest.py')),
                                                  digest(evidence_file) if evidence_file else None,
                                                  digest(legacy_db) if legacy_db and Path(legacy_db).is_file() else None))])
        with tempfile.TemporaryDirectory(prefix='charls-metadata-') as temp:
            for item in manifest['items']:
                relative = PurePosixPath(item['file'])
                path = (root / relative).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    raise ValueError('Source outside root or missing: ' + item['file'])
                if path.stat().st_size != item['bytes']:
                    raise ValueError('Source size changed: ' + item['file'])
                with path.open('rb') as f:
                    if hashlib.file_digest(f, 'sha1').hexdigest().lower() != item['sha1'].lower():
                        raise ValueError('Source checksum changed: ' + item['file'])
                rid = ident('res_', item['file'], item['sha1'])
                resource = {**item, 'resource_id': rid}
                con.execute('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (rid, item['group'], item['kind'], item['label'], str(path),
                             item['sourceUrl'], digest(path), item['bytes'], 'catalogued', None))
                print(item['file'], file=sys.stderr, flush=True)
                if item['kind'] not in {'dataset', 'codebook', 'questionnaire', 'supplementary_document'}:
                    continue
                try:
                    for member, size in members(path):
                        suffix = PurePosixPath(member).suffix.lower()
                        if suffix not in {'.dta', '.pdf', '.doc', '.docx'}:
                            continue
                        if size and size > MAX_MEMBER_BYTES:
                            raise ValueError('Member too large: ' + member)
                        target = Path(temp) / ('member' + suffix)
                        con.execute('SAVEPOINT member')
                        try:
                            extract_member(path, member, target)
                            if suffix == '.dta':
                                index_stata(con, target, resource, member)
                            elif suffix == '.pdf':
                                index_pdf(con, target, resource, member, legacy_db,cache_catalog)
                            else:
                                index_word(con, target, resource, member, ident, digest)
                            con.execute('RELEASE member')
                        except Exception:
                            con.execute('ROLLBACK TO member')
                            con.execute('RELEASE member')
                            raise
                        finally:
                            target.unlink(missing_ok=True)
                except Exception as exc:
                    error = {'file': item['file'], 'error': str(exc)[:500]}
                    errors.append(error)
                    con.execute('UPDATE resources SET status=?,error=? WHERE resource_id=?',
                                ('partial', error['error'], rid))
                con.commit()
        if errors:
            raise ValueError('Catalogue incomplete; no publication: ' + json.dumps(errors, ensure_ascii=False))
        index_raw_source_links(con,legacy_db)
        index_variable_mentions(con)
        index_reviewed_facts(con,evidence_file)
        counts = {t: con.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
                  for t in ['resources', 'datasets', 'data_variables', 'documents', 'document_pages','raw_source_links',
                            'document_sources','variable_document_links','reviewed_variable_facts']}
        document_states = dict(con.execute('SELECT trust_status,count(*) FROM documents GROUP BY trust_status'))
        formats = dict(con.execute('SELECT format,count(*) FROM documents GROUP BY format'))
        coding_states = dict(con.execute('SELECT coding_status,count(*) FROM data_variables GROUP BY coding_status'))
        con.execute("UPDATE meta SET value='1' WHERE key='published'")
        con.commit()
        if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Catalogue integrity failed')
        con.close()
        os.link(scratch, output)
        return {'status': 'built', 'output': str(output), 'counts': counts, 'coding_states':coding_states,'errors': errors,
                'document_trust_states': document_states, 'document_formats': formats, 'participant_rows_indexed': 0}
    finally:
        con.close()
        Path(scratch).unlink(missing_ok=True)
