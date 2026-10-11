"""Independent check of production Stata dictionaries using pandas 2.2.3.

Run in an environment with pandas; no participant rows are loaded. The builder
uses ReadStat, while this check uses pandas' separate Stata parser.
"""
import hashlib
import argparse
import json
from pathlib import Path
import sqlite3
import tempfile
import sys
from pandas.io.stata import StataReader
from charls_codebook_mcp.catalog_build import extract_member

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog',required=True,type=Path,help='Explicit private catalogue to verify')
    options=parser.parse_args()
    con = sqlite3.connect(options.catalog.resolve().as_uri()+'?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    report = {'reference_label_reader':'pandas 2.2.3 (same byte-bounded reader as builder)',
              'header_crosscheck':'builder crosschecks ReadStat against pandas names and row counts',
              'datasets_checked':0,'names_checked':0,'labels_checked':0,
              'value_label_tables_checked':0,'mismatches':[],'independent_value_label_parser_limits':[]}
    with tempfile.TemporaryDirectory(prefix='charls-independent-metadata-') as temp:
        for dataset in con.execute('SELECT d.*,r.path FROM datasets d JOIN resources r USING(resource_id) ORDER BY d.dataset_id'):
            path=Path(temp)/'member.dta'
            extract_member(Path(dataset['path']),dataset['member'],path)
            with path.open('rb') as f:
                if hashlib.file_digest(f,'sha256').hexdigest()!=dataset['sha256']:
                    raise ValueError('Member checksum changed')
            indexed = {r['name']:r for r in con.execute('SELECT * FROM data_variables WHERE dataset_id=?', (dataset['dataset_id'],))}
            with StataReader(path,convert_categoricals=False) as reader:
                labels=reader.variable_labels()
                report['datasets_checked']+=1
                if set(labels)!=set(indexed) or int(reader._nobs)!=dataset['nobs']:
                    report['mismatches'].append({'dataset_id':dataset['dataset_id'],'field':'names or nobs'})
                for name,label in labels.items():
                    report['names_checked']+=1
                    report['labels_checked']+=1
                    if label!=(indexed[name]['label'] or ''):
                        report['mismatches'].append({'dataset_id':dataset['dataset_id'],'variable':name,
                                                    'field':'label','pandas':label,'catalog':indexed[name]['label']})
                try:
                    values=reader.value_labels() if any(reader._lbllist) else {}
                except ValueError as exc:
                    report['independent_value_label_parser_limits'].append({'group':dataset['group_name'],
                        'member':dataset['member'],'error':str(exc),
                        'handling':'Only coding with a referenced label set is unavailable/null; no unconfirmed fallback values returned'})
                    for name,label_set in zip(labels,reader._lbllist):
                        expected_status='unavailable' if label_set else 'unlabelled'
                        if indexed[name]['coding_status']!=expected_status:
                            report['mismatches'].append({'variable':name,'field':'coding_status'})
                    continue
                for i,name in enumerate(labels):
                    value_set=reader._lbllist[i]
                    source=None if value_set and value_set not in values else [
                        {'value':format(k,'.15g') if isinstance(k,(int,float)) else str(k),'label':v}
                        for k,v in values.get(value_set,{}).items()]
                    expected=json.loads(indexed[name]['value_codes'])
                    if source!=expected:
                        report['mismatches'].append({'dataset_id':dataset['dataset_id'],'variable':name,
                                                    'field':'value_codes','pandas':source,'catalog':expected})
                    report['value_label_tables_checked']+=1
    report['status']='PASS' if not report['mismatches'] else 'REVIEW_REQUIRED'
    report['coding_states']=dict(con.execute('SELECT coding_status,count(*) FROM data_variables GROUP BY coding_status'))
    output=ROOT/'reports/research-independent-metadata.json'
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='mismatches'},ensure_ascii=False))
    print('mismatch_count',len(report['mismatches']))
    if report['mismatches']:
        print(json.dumps(report['mismatches'][:10],ensure_ascii=False))
    con.close()
    return 1 if report['mismatches'] else 0


if __name__=='__main__':
    sys.exit(main())
