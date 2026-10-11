import argparse
import importlib.metadata
import json
from pathlib import Path
import platform
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[2]

def emit(value): print(json.dumps(value,ensure_ascii=False,indent=2))

def main(argv=None):
    parser=argparse.ArgumentParser(prog='charls-codebook')
    parser.add_argument('--root',type=Path,default=ROOT)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('doctor')
    fetch=sub.add_parser('fetch');fetch.add_argument('--import-pdf',type=Path)
    build=sub.add_parser('build');build.add_argument('--pdf',type=Path);build.add_argument('--output',type=Path)
    build.add_argument('--alias-file',type=Path,help='Optional private reviewed legacy aliases; public defaults are retrieval aids only')
    catalog=sub.add_parser('catalog-build',help='Build local multi-year research index; requires requirements-catalog.txt')
    catalog.add_argument('--data-root',required=True,type=Path)
    catalog.add_argument('--output',type=Path)
    catalog.add_argument('--cache-catalog',type=Path,help='Reuse PDF extraction only when the PDF hash matches')
    catalog.add_argument('--supplement-manifest',type=Path,help='Explicit private supplementary download ledger')
    catalog.add_argument('--review-record',type=Path,help='Private validation/review record tied to that ledger')
    catalog.add_argument('--evidence-file',type=Path,help='Private source-anchored reviewed variable facts')
    enrich=sub.add_parser('catalog-enrich',help='Add anchored facts to a fresh local catalogue without re-reading data files')
    enrich.add_argument('--catalog',type=Path,required=True)
    enrich.add_argument('--evidence-file',type=Path,required=True)
    enrich.add_argument('--output',type=Path,required=True)
    for name in ['validate','benchmark','serve','search','variable','source']:
        p=sub.add_parser(name);p.add_argument('--db',type=Path)
        if name=='validate':p.add_argument('--catalog',type=Path)
        if name=='search':p.add_argument('query');p.add_argument('--limit',type=int,default=5);p.add_argument('--cursor')
        if name=='variable':p.add_argument('variable_name')
        if name=='source':p.add_argument('source_id');p.add_argument('--cursor');p.add_argument('--max-chars',type=int,default=8000)
        if name=='benchmark':p.add_argument('--samples',type=int,default=100)
    args=parser.parse_args(argv);root=args.root.resolve();db=getattr(args,'db',None) or root/'data/processed/codebook.sqlite'
    try:
        if args.command=='doctor':
            c=sqlite3.connect(':memory:');c.execute('CREATE VIRTUAL TABLE probe USING fts5(text)')
            c.execute('INSERT INTO probe VALUES (?)',('verified FTS5',));ok=c.execute('SELECT count(*) FROM probe WHERE probe MATCH ?',('verified',)).fetchone()[0]==1;c.close()
            source_status={'status':'BLOCKED','reason':'缺少来源 manifest 或 PDF'}
            if (root/'data/source/manifest.json').is_file() and (root/'data/raw/Harmonized_CHARLS_D.pdf').is_file():
                from .source import identify
                m=json.loads((root/'data/source/manifest.json').read_text())
                actual=identify(root/'data/raw/Harmonized_CHARLS_D.pdf',m)
                source_status={'status':'verified' if actual['sha256']==m['sha256'] else 'conflict',
                               'version':actual['version'],'physical_pages':actual['physical_pages'],'sha256':actual['sha256']}
            database_status={'status':'missing'}
            if db.is_file():
                from .query import Codebook
                b=Codebook(db)
                try:database_status={'status':'published_readonly','document':b.get_document_info()['data']}
                finally:b.close()
            emit({'python':platform.python_version(),'executable':sys.executable,'sqlite':sqlite3.sqlite_version,'FTS5':ok,
                  'packages':{p:importlib.metadata.version(p) for p in ['mcp','pdfplumber','pdfminer.six']},
                  'source':source_status,'database':database_status})
        elif args.command=='fetch':
            from .source import acquire
            emit(acquire(root,args.import_pdf))
        elif args.command=='build':
            from .build import build_database
            report=build_database(args.pdf or root/'data/raw/Harmonized_CHARLS_D.pdf',root/'data/source/manifest.json',
                                  args.output or db,args.alias_file or root/'config/aliases.json')
            (root/'reports').mkdir(exist_ok=True)
            (root/'reports/build.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            emit(report)
        elif args.command=='catalog-enrich':
            from .catalog_enrich import enrich_catalog
            emit(enrich_catalog(args.catalog,args.evidence_file,args.output))
        elif args.command=='catalog-build':
            from .catalog_build import build_catalog
            emit(build_catalog(args.data_root,args.output or root/'data/processed/research-catalog.sqlite',db,args.cache_catalog,
                               args.supplement_manifest,args.review_record,args.evidence_file))
        elif args.command=='serve':
            from .server import main as serve
            return serve(['--db',str(db.resolve())])
        elif args.command=='benchmark':
            from .verification import benchmark
            emit(benchmark(db,root,args.samples))
        elif args.command=='validate':
            from .verification import validate,validate_research
            result=validate(db,root);emit(result)
            if getattr(args,'catalog',None):
                research=validate_research(args.catalog,root/'config/research-aliases.json',root)
                emit(research)
                return 0 if result['status']=='PASS' and research['status']=='PASS' else 1
            return 0 if result['status']=='PASS' else 1
        else:
            from .query import Codebook
            book=Codebook(db)
            try:
                if args.command=='search':result=book.invoke('search_codebook',query=args.query,limit=args.limit,cursor=args.cursor)
                elif args.command=='variable':result=book.invoke('get_variable',variable_name=args.variable_name)
                else:result=book.invoke('read_source',source_id=args.source_id,cursor=args.cursor,max_chars=args.max_chars)
                emit(result)
            finally:book.close()
    except Exception as exc:
        print('ERROR: '+str(exc),file=sys.stderr)
        return 2
    return 0

if __name__=='__main__':sys.exit(main())
