"""Small schema and verified, URI-escaped production read-only connections."""
import json
from pathlib import Path
import sqlite3

SCHEMA_VERSION = 1
SCHEMA = '''
PRAGMA journal_mode=DELETE;
PRAGMA foreign_keys=ON;
CREATE TABLE documents(document_id TEXT PRIMARY KEY, build_id TEXT NOT NULL,
  manifest TEXT NOT NULL, parser TEXT NOT NULL, published INTEGER NOT NULL DEFAULT 0);
CREATE TABLE pages(document_id TEXT NOT NULL REFERENCES documents, pdf_page_number INTEGER NOT NULL,
  printed_page_label TEXT, raw_text TEXT NOT NULL, extraction_status TEXT NOT NULL,
  warnings TEXT NOT NULL, PRIMARY KEY(document_id,pdf_page_number));
CREATE TABLE sections(source_id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents,
  ordinal INTEGER NOT NULL, parent_id TEXT, title TEXT NOT NULL, chapter TEXT,
  body TEXT NOT NULL, source_spans TEXT NOT NULL, content_type TEXT NOT NULL,
  extraction_status TEXT NOT NULL, review_status TEXT NOT NULL);
CREATE TABLE variables(document_id TEXT NOT NULL REFERENCES documents,
  name TEXT NOT NULL COLLATE NOCASE, label TEXT NOT NULL, wave TEXT, subject TEXT, year INTEGER,
  source_id TEXT NOT NULL REFERENCES sections, evidence TEXT NOT NULL,
  extraction_status TEXT NOT NULL, review_status TEXT NOT NULL,
  PRIMARY KEY(document_id,name));
CREATE INDEX variable_source ON variables(source_id);
CREATE TABLE aliases(term TEXT PRIMARY KEY, english TEXT NOT NULL, evidence TEXT NOT NULL,
  ambiguous INTEGER NOT NULL, review_status TEXT NOT NULL);
CREATE VIRTUAL TABLE search_fts USING fts5(source_id UNINDEXED, document_id UNINDEXED,
  title, variables, body, tokenize='unicode61');
PRAGMA user_version=1;
'''

def open_readonly(path):
    p = Path(path).expanduser().absolute()
    if not p.is_file():
        raise ValueError('DATABASE_MISSING: 未发布数据库；服务不会自动建库')
    con = sqlite3.connect(p.as_uri()+'?mode=ro', uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.enable_load_extension(False)
    con.execute('PRAGMA query_only=ON')
    try:
        if con.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
            raise ValueError('SCHEMA_CONFLICT: 数据库 schema 不兼容')
        docs = con.execute('SELECT * FROM documents').fetchall()
        if not docs or any(d['published'] != 1 for d in docs):
            raise ValueError('DATABASE_UNPUBLISHED: 数据库未成功发布')
        for d in docs:
            if json.loads(d['manifest'])['version'] != 'D':
                raise ValueError('VERSION_CONFLICT: 本服务只启用 Version D')
        for table in ['pages','sections','variables','aliases','search_fts']:
            con.execute(f'SELECT 1 FROM {table} LIMIT 1')  # Internal fixed allow-list.
    except Exception:
        con.close()
        raise
    return con

def validate_connection(con):
    errors = []
    if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        errors.append('integrity_check')
    if con.execute('PRAGMA foreign_key_check').fetchall():
        errors.append('foreign_key_check')
    counts = {t:con.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
              for t in ['documents','pages','sections','variables','aliases','search_fts']}
    coverage = {}
    for row in con.execute('SELECT * FROM sections ORDER BY ordinal'):
        pieces = []
        for span in json.loads(row['source_spans']):
            p = con.execute('SELECT raw_text FROM pages WHERE document_id=? AND pdf_page_number=?',
                            (row['document_id'],span['pdf_page_number'])).fetchone()
            if not p or not 0 <= span['start_char'] <= span['end_char'] <= len(p[0]):
                errors.append(row['source_id']+': invalid_span'); continue
            pieces.append(p[0][span['start_char']:span['end_char']])
            coverage.setdefault((row['document_id'],span['pdf_page_number']),[]).append((span['start_char'],span['end_char']))
        if '\n'.join(pieces) != row['body']:
            errors.append(row['source_id']+': body_span_mismatch')
    for p in con.execute('SELECT * FROM pages'):
        intervals = sorted(coverage.get((p['document_id'],p['pdf_page_number']),[]))
        at = 0
        for start,end in intervals:
            if start != at: errors.append(f"page {p['pdf_page_number']}: gap/overlap")
            at = end
        if at != len(p['raw_text']): errors.append(f"page {p['pdf_page_number']}: incomplete")
    for v in con.execute('SELECT * FROM variables'):
        e = json.loads(v['evidence'])['table_row']
        p = con.execute('SELECT raw_text FROM pages WHERE document_id=? AND pdf_page_number=?',
                        (v['document_id'],e['pdf_page_number'])).fetchone()
        if v['name'] not in p[0][e['start_char']:e['end_char']] or v['label'] not in p[0][e['start_char']:e['end_char']]:
            errors.append(v['name']+': field_evidence_mismatch')
    if counts['sections'] != counts['search_fts'] or con.execute('SELECT count(*) FROM sections s LEFT JOIN search_fts f ON s.source_id=f.source_id WHERE f.source_id IS NULL').fetchone()[0]:
        errors.append('fts_count_or_membership')
    return {'status':'PASS' if not errors else 'FAIL','counts':counts,'errors':errors}
