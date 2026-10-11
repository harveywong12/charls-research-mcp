"""Publish a new private catalogue with additive source-anchored evidence."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from .document_ingest import index_reviewed_facts


def enrich_catalog(source, evidence, output):
    source, evidence, output = map(lambda p:Path(p).resolve(), (source,evidence,output))
    if source == output or output.exists():
        raise ValueError('Choose a fresh output; the active catalogue is never overwritten')
    payload=json.loads(evidence.read_text(encoding='utf-8'))
    output.parent.mkdir(parents=True,exist_ok=True)
    temporary=output.with_name(output.name+'.partial')
    if temporary.exists(): raise ValueError('Partial output already exists')
    original=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    target=sqlite3.connect(temporary)
    target.row_factory=sqlite3.Row
    try:
        if original.execute('PRAGMA user_version').fetchone()[0]!=4:
            raise ValueError('Evidence enrichment requires a published schema 4 catalogue')
        meta=dict(original.execute('SELECT key,value FROM meta'))
        if meta.get('published')!='1': raise ValueError('Source catalogue is unpublished')
        original.backup(target)
        target.execute("UPDATE meta SET value='0' WHERE key='published'")
        before=target.execute('SELECT count(*) FROM reviewed_variable_facts').fetchone()[0]
        # Additive only: duplicate facts fail rather than replacing a user's review.
        index_reviewed_facts(target,evidence)
        after=target.execute('SELECT count(*) FROM reviewed_variable_facts').fetchone()[0]
        digest=hashlib.sha256(evidence.read_bytes()).hexdigest()
        code={name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
              for name in ('catalog_enrich.py','document_ingest.py')}
        build='cat_'+hashlib.sha256(json.dumps({'parent':meta['build_id'],'evidence':digest,
                                              'code':code},sort_keys=True).encode()).hexdigest()[:24]
        inputs=json.loads(meta.get('build_inputs','{}'))
        inputs['evidence_enrichment']={'parent_build_id':meta['build_id'],
                                      'name':evidence.name,'sha256':digest,'builder_sha256':code}
        target.execute('UPDATE meta SET value=? WHERE key=\'build_inputs\'',(json.dumps(inputs,sort_keys=True),))
        target.execute('UPDATE meta SET value=? WHERE key=\'build_id\'',(build,))
        target.execute("UPDATE meta SET value='1' WHERE key='published'")
        target.commit()
        if target.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise ValueError('Enriched catalogue integrity failed; not published')
        target.close();target=None
        os.link(temporary, output)
        temporary.unlink()
        return {'status':'published','catalogue':str(output),'parent_build_id':meta['build_id'],
                'build_id':build,'previous_fact_records':before,'fact_records':after,
                'added_review_inputs':len(payload['facts']),'evidence_sha256':digest,
                'participant_rows_added':0,'original_catalogue_modified':False}
    except Exception:
        if target is not None:target.close();target=None
        if temporary.exists():temporary.unlink()
        raise
    finally:
        original.close()
        if target is not None:target.close()
