"""Private, local supplementary document ingestion. No network or observations.

Download ledgers, review flags and curated facts are inputs, never package assets.
An exact variable token is a document reference, not a confirmed definition.
"""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess


def load_supplements(root, manifest, ledger_path=None, review_path=None):
    if not ledger_path:
        if review_path:
            raise ValueError('A review record requires its download ledger')
        return manifest
    ledger = json.loads(Path(ledger_path).read_text(encoding='utf-8'))
    review = json.loads(Path(review_path).read_text(encoding='utf-8')) if review_path else {}
    if review.get('download_manifest') and Path(review['download_manifest']).resolve() != Path(ledger_path).resolve():
        raise ValueError('Review record belongs to a different download ledger')
    if review.get('errors'):
        raise ValueError('Supplement validation contains unresolved file errors')
    by_path = {i['file']: i for i in manifest['items']}
    flags = {}
    for issue in review.get('cross_year_identical_document_links', []):
        flags.setdefault(issue['sha256'], []).append({'issue': 'ambiguous_year_version',
            'note': issue['note'], 'claimed_group': issue['page_group'], 'source_url': issue['source_url']})
    bad_sha1 = {i['actual_sha1']: i for i in review.get('unresolved_metadata_discrepancies', [])}
    for item in ledger['items']:
        if item.get('status') not in {'downloaded', 'already_saved', 'duplicate_by_sha256'}:
            continue
        path = Path(item['saved_path']).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('Supplement path is missing or outside the data root')
        with path.open('rb') as f:
            sha = hashlib.file_digest(f, 'sha256').hexdigest()
        if sha != item['sha256'] or path.stat().st_size != item['bytes']:
            raise ValueError('Supplement bytes differ from the download ledger: ' + path.name)
        if item.get('sha1') in bad_sha1:
            flags.setdefault(sha, []).append({'issue': 'official_checksum_mismatch',
                'note': bad_sha1[item['sha1']]['cause'],
                'official_sha1': bad_sha1[item['sha1']]['official_sha1'], 'actual_sha1': item['sha1']})
        elif item.get('official_sha1_match') is False:
            flags.setdefault(sha, []).append({'issue': 'official_checksum_mismatch',
                'note': 'Downloaded bytes do not match the displayed official checksum.'})
        relative = path.relative_to(root).as_posix()
        entry = by_path.get(relative)
        if entry is None:
            entry = {'file': relative, 'group': relative.split('/')[0], 'kind': 'supplementary_document',
                'label': item['filename'], 'sourceUrl': item['source_url'], 'bytes': item['bytes'],
                'sha1': item['sha1']}
            manifest['items'].append(entry)
            by_path[relative] = entry
        entry.setdefault('document_sources', []).append({'group': item['group'],
            'url': item['source_url'], 'label': item['filename'], 'pages': item.get('sources', [])})
    # Duplicate mirrors share a single document and a single conservative review state.
    for entry in manifest['items']:
        path = root / entry['file']
        with path.open('rb') as f:
            sha = hashlib.file_digest(f, 'sha256').hexdigest()
        entry['review_issues'] = list({json.dumps(i, sort_keys=True): i for i in flags.get(sha, [])}.values())
    return manifest


def add_document_sources(con, document, resource, member):
    sources = [{'group': resource['group'], 'url': resource['sourceUrl'], 'label': resource['label']}]
    sources += resource.get('document_sources', [])
    for source in sources:
        payload = {'resource_id': resource['resource_id'], 'member': member, **source}
        sid = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
        con.execute('INSERT OR IGNORE INTO document_sources VALUES(?,?,?,?,?,?,?)',
            (document, sid, resource['resource_id'], source['group'], source['url'], source['label'],
             json.dumps(payload, ensure_ascii=False)))
    issues = resource.get('review_issues', [])
    if issues:
        row = con.execute('SELECT review_issues FROM documents WHERE document_id=?', (document,)).fetchone()
        combined = json.loads(row[0]) + issues
        combined = list({json.dumps(i, sort_keys=True): i for i in combined}.values())
        con.execute("UPDATE documents SET trust_status='needs_review',review_issues=? WHERE document_id=?",
                    (json.dumps(combined, ensure_ascii=False), document))


def index_word(con, path, resource, member, ident, digest):
    converter = shutil.which('textutil')
    if not converter:
        raise ValueError('WORD_READER_MISSING: textutil is required for local DOC/DOCX ingestion')
    sha = digest(path)
    document = ident('doc_', sha)
    if con.execute('SELECT 1 FROM documents WHERE document_id=?', (document,)).fetchone():
        add_document_sources(con, document, resource, member)
        return
    result = subprocess.run([converter, '-convert', 'txt', '-stdout', str(path)],
                            capture_output=True, check=True, timeout=120)
    text = result.stdout.decode('utf-8', errors='strict')
    # These are stable character segments, NOT rendered pages. Do not invent Word pagination.
    chunks = [text[n:n+5000] for n in range(0, len(text), 5000)] or ['']
    issues = resource.get('review_issues', [])
    con.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (document, resource['resource_id'], resource['group'], member, resource['label'], sha,
         len(chunks), path.suffix.lower()[1:], 'text_segment', 'needs_review' if issues else 'verified_local',
         json.dumps(issues, ensure_ascii=False)))
    add_document_sources(con, document, resource, member)
    for n, text in enumerate(chunks, 1):
        state = 'text_extracted' if text.strip() else 'empty_no_ocr'
        if '\ufffd' in text: state = 'possible_garbled_text'
        con.execute('INSERT INTO document_pages VALUES(?,?,?,?)', (document, n, text, state))
        con.execute('INSERT INTO document_fts VALUES(?,?,?)', (document, n, text))


def index_variable_mentions(con):
    """Only exact tokens in trustworthy same-group documents; not semantic mappings."""
    variables = {}
    for row in con.execute('SELECT v.dataset_id,v.name,d.group_name FROM data_variables v JOIN datasets d USING(dataset_id)'):
        if len(row['name']) >= 4:
            variables.setdefault(row['group_name'], {}).setdefault(row['name'].casefold(), []).append((row['dataset_id'], row['name']))
    for doc in con.execute("SELECT * FROM documents WHERE trust_status='verified_local'"):
        groups = {r[0] for r in con.execute('SELECT DISTINCT claimed_group FROM document_sources WHERE document_id=?', (doc['document_id'],))}
        names = {}
        for group in groups:
            for name, entries in variables.get(group, {}).items(): names.setdefault(name, []).extend(entries)
        for page in con.execute("SELECT * FROM document_pages WHERE document_id=? AND state='text_extracted'", (doc['document_id'],)):
            seen = set()
            for token in re.finditer(r'(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_]{3,63}(?![A-Za-z0-9_])', page['text']):
                for dataset, name in names.get(token[0].casefold(), []):
                    key = (dataset, name)
                    if key in seen: continue
                    seen.add(key)
                    con.execute('INSERT OR IGNORE INTO variable_document_links VALUES(?,?,?,?,?,?,?)',
                        (dataset, name, doc['document_id'], page['page'], token.start(), token.end(), 'exact_token_same_group_reference'))


def index_reviewed_facts(con, path):
    if not path: return
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if payload.get('schema_version') not in (1, 2):
        raise ValueError('Unsupported private evidence schema')
    for fact in payload['facts']:
        rows = con.execute('SELECT v.dataset_id,v.name,d.group_name FROM data_variables v JOIN datasets d USING(dataset_id) WHERE v.year=? AND v.name=? COLLATE NOCASE AND d.module=? AND d.namespace=?',
            (fact['year'], fact['variable_name'], fact['module'], fact.get('namespace', 'raw'))).fetchall()
        if not rows or not fact.get('sources'):
            raise ValueError('Reviewed fact has no actual variable or source')
        name_confirmed = False
        for source in fact['sources']:
            doc = con.execute('SELECT * FROM documents WHERE sha256=?', (source['document_sha256'],)).fetchone()
            if doc is None or doc['trust_status'] != 'verified_local':
                raise ValueError('Reviewed evidence references a missing or quarantined document')
            groups = {r[0] for r in con.execute('SELECT claimed_group FROM document_sources WHERE document_id=?', (doc['document_id'],))}
            if any(row['group_name'] not in groups for row in rows):
                raise ValueError('Reviewed evidence group disagrees with the variable year')
            page = con.execute('SELECT * FROM document_pages WHERE document_id=? AND page=?', (doc['document_id'], source['page'])).fetchone()
            quote = source['anchor']
            if not quote or page is None or page['text'].count(quote) != 1:
                raise ValueError('Reviewed evidence anchor is missing or not unique')
            start = page['text'].index(quote)
            source.update(document_id=doc['document_id'], start_char=start, end_char=start+len(quote), locator_type=doc['locator_type'])
            name_confirmed |= bool(re.search(r'(?<![A-Za-z0-9_])'+re.escape(fact['variable_name'])+r'(?![A-Za-z0-9_])', page['text'], re.I))
        if not name_confirmed:
            raise ValueError('Reviewed fact has no explicit variable-name evidence')
        if fact.get('review_status') != 'reviewed_against_source_and_metadata':
            raise ValueError('Reviewed evidence requires an explicit review status')
        from .indicator_evidence import EVIDENCE_FIELDS, EVIDENCE_STATES
        for field, evidence in fact.get('evidence_fields', {}).items():
            if field not in EVIDENCE_FIELDS or not isinstance(evidence, dict):
                raise ValueError('Unknown or malformed evidence field')
            if evidence.get('status') not in EVIDENCE_STATES or not evidence.get('reason'):
                raise ValueError('Every evidence field requires a status and rationale')
            indexes=evidence.get('source_indexes', [])
            if type(indexes) is not list or any(type(i) is not int or not 0<=i<len(fact['sources']) for i in indexes):
                raise ValueError('Evidence field source index is invalid')
            if evidence['status']=='document_explicit' and (not indexes or evidence.get('value') is None):
                raise ValueError('Explicit evidence requires a value and anchored sources')
            if evidence['status']=='conflict' and len(set(indexes))<2:
                raise ValueError('Conflicting evidence requires at least two anchored sources')
        if payload['schema_version']==2 and not fact.get('evidence_fields'):
            raise ValueError('Evidence schema 2 requires field-level review')
        for row in rows:
            con.execute('INSERT INTO reviewed_variable_facts VALUES(?,?,?)',
                (row['dataset_id'], row['name'], json.dumps(fact, ensure_ascii=False)))
