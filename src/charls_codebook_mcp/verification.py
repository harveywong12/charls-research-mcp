"""Local validation and benchmark tooling, not imported by the MCP query path."""
import asyncio
import json
from pathlib import Path
import platform
import statistics
import sys
import time
from .query import Codebook,literal_fts
from .storage import validate_connection
from .source import sha256

def save(root,name,data):
    p=Path(root)/'reports'/name;p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(data,ensure_ascii=False,indent=2))

def validate(db,root):
    book=Codebook(db)
    try:
        structure=validate_connection(book.con)
        gold_path=Path(root)/'reports/private/gold-codebook.json'
        if not gold_path.is_file():
            return {'status':'PARTIAL','structure':structure,'gold':'BLOCKED: no independent gold set'}
        gold=json.loads(gold_path.read_text());results=[]
        actual=book.get_document_info()['data']['sha256']
        if actual!=gold['document_sha256']:
            return {'status':'FAIL','message':'Gold set belongs to a different PDF'}
        original=Path(root)/'data/raw/Harmonized_CHARLS_D.pdf'
        manifest=json.loads((Path(root)/'data/source/manifest.json').read_text())
        if sha256(original)!=actual or manifest['sha256']!=actual:
            return {'status':'FAIL','message':'Original PDF or manifest hash does not match database provenance'}
        import pypdfium2 as pdfium
        pdf=pdfium.PdfDocument(original)
        cache={}
        def independent(page):
            if page not in cache:
                p=pdf[page-1];t=p.get_textpage();cache[page]=t.get_text_range();t.close();p.close()
            return cache[page]
        for case in gold['cases']:
            passed=False;detail={};group=case['group'];anchor=case.get('anchor')
            anchor_ok=True
            if anchor:
                anchor_ok=anchor['needle'].casefold() in independent(anchor['pdf_page_number']).casefold()
            if group in ['exact','english','chinese']:
                response=book.invoke('search_codebook',query=case['query'],limit=5)
                rows=response['data']['results']
                if group=='exact':
                    passed=bool(rows and rows[0]['matched_variable']==case['expected_variable'] and rows[0]['match_type']=='exact_variable')
                else:
                    passed=any(book.con.execute('SELECT body FROM sections WHERE source_id=?',(r['source_id'],)).fetchone()[0].casefold().find(anchor['needle'].casefold())>=0 and
                        any(s['pdf_page_number']==anchor['pdf_page_number'] for s in json.loads(book.con.execute('SELECT source_spans FROM sections WHERE source_id=?',(r['source_id'],)).fetchone()[0])) for r in rows)
                detail={'source_ids':[r['source_id'] for r in rows],'status':response['status'],'warnings':response['warnings']}
            elif group=='raw_evidence':
                response=book.invoke('get_variable',variable_name=case['variable'])
                if response['data'].get('source_id'):
                    source_id=response['data']['source_id'];parts=[];cursor=None;cited_pages=set()
                    while True:
                        read=book.invoke('read_source',source_id=source_id,cursor=cursor,max_chars=4000)
                        if read['status']!='ok':break
                        parts.append(read['data']['text']);cited_pages.update(c['pdf_page_number'] for c in read['citations'])
                        cursor=read['next_cursor']
                        if not cursor:break
                    full=''.join(parts)
                    stored=book.con.execute('SELECT body FROM sections WHERE source_id=?',(source_id,)).fetchone()[0]
                    passed=full==stored and anchor['needle'].casefold() in full.casefold() and anchor['pdf_page_number'] in cited_pages
                    detail={'source_id':source_id,'continuation_calls':len(parts),'lossless':full==stored,'structured_codes':response['data']['value_codes']}
            else:
                response=book.invoke(case['tool'],**case['args'])
                passed=response['status']==case['status'] and (not case.get('warning') or any(case['warning'] in w for w in response['warnings']))
                detail={'status':response['status'],'warnings':response['warnings']}
            results.append({'id':case['id'],'group':group,'query':case['query'],'passed':passed and anchor_ok,'independent_anchor_verified':anchor_ok,'detail':detail})
        pdf.close()
        metrics={g:{'passed':sum(r['passed'] for r in results if r['group']==g),'total':sum(r['group']==g for r in results)}
                 for g in ['exact','english','chinese','raw_evidence','boundary']}
        result={'status':'PASS' if structure['status']=='PASS' and all(r['passed'] for r in results) else 'FAIL',
                'structure':structure,'metrics':metrics,'passed':sum(r['passed'] for r in results),'total':len(results),
                'results':results,'human_reviewed':0,'scope':'Agent-checked sample only; not a full-book scientific audit'}
        save(root,'acceptance.json',result)
        return result
    finally:book.close()

def validate_research(catalog_path, alias_path, root, gold_path=None):
    """Catalogue acceptance: pinned build_id, internal consistency and gold cases.

    The gold file pins one catalogue build_id, so these checks detect drift and
    regression of that build; they are not independent ground truth because the
    assertions are re-derived from the same database.
    """
    from .catalog import ResearchCatalog
    root=Path(root)
    gold_path=Path(gold_path) if gold_path else root/'tests/gold_research.json'
    if not gold_path.is_file():
        return {'status':'BLOCKED','message':'no research gold set at '+str(gold_path)}
    gold=json.loads(gold_path.read_text())
    catalog=ResearchCatalog(catalog_path, alias_path if alias_path and Path(alias_path).is_file() else None)
    try:
        checks=[]
        def add(name,ok,detail=None):
            checks.append({'check':name,'status':'PASS' if ok else 'FAIL','detail':detail})
        meta=dict(catalog.con.execute('SELECT key,value FROM meta'))
        counts={t:catalog.con.execute('SELECT count(*) FROM '+t).fetchone()[0]
                for t in ['resources','datasets','data_variables','documents','document_pages','raw_source_links']}
        add('catalogue_build_id_matches_gold', meta.get('build_id')==gold['catalogue_build_id'], meta.get('build_id'))
        add('catalogue_published', meta.get('published')=='1')
        add('datasets_have_resources',
            catalog.con.execute('SELECT count(*) FROM datasets d LEFT JOIN resources r USING(resource_id) WHERE r.resource_id IS NULL').fetchone()[0]==0)
        add('documents_have_pages',
            catalog.con.execute('SELECT count(*) FROM documents d WHERE NOT EXISTS(SELECT 1 FROM document_pages p WHERE p.document_id=d.document_id)').fetchone()[0]==0)
        add('document_pages_have_fts',
            catalog.con.execute('SELECT count(*) FROM document_pages p WHERE NOT EXISTS(SELECT 1 FROM document_fts f WHERE f.document_id=p.document_id AND f.page=p.page)').fetchone()[0]==0)
        add('no_participant_rows_indexed', catalog.invoke('get_research_overview')['data']['participant_rows_indexed']==0)
        if gold.get('build_inputs'):
            actual=json.loads(meta['build_inputs']) if meta.get('build_inputs') else {}
            expected=gold['build_inputs']
            detail={name:{'expected':value.get('sha256'),
                          'actual':(actual.get(name) or {}).get('sha256')} for name,value in expected.items()}
            add('catalogue_build_inputs_match_gold',
                all((actual.get(name) or {}).get('sha256')==value.get('sha256')
                    for name,value in expected.items()), detail)
        for case in gold['cases']:
            response=catalog.invoke(case['method'],**case.get('args',{}))
            ok=True if 'status' not in case else response['status']==case['status']
            if 'status_in' in case:
                ok=ok and response['status'] in case['status_in']
            detail={}
            if case.get('equals'):
                for path in case['equals']:
                    current=response;missing=False
                    for part in path.split('.'):
                        if isinstance(current,list) and part.isdigit() and int(part)<len(current):current=current[int(part)]
                        elif isinstance(current,dict) and part in current:current=current[part]
                        else:missing=True;break
                    detail[path]=None if missing else current
                ok=ok and detail==case['equals']
            if case.get('contains'):
                flat=json.dumps(response,ensure_ascii=False)
                ok=ok and all(needle in flat for needle in case['contains'])
            add(case['id'],ok,detail or None)
        status='PASS' if all(c['status']=='PASS' for c in checks) else 'FAIL'
        result={'status':status,'catalogue':str(catalog_path),'build_id':meta.get('build_id'),'counts':counts,
                'passed':sum(c['status']=='PASS' for c in checks),'total':len(checks),'checks':checks,
                'basis':'pinned to the catalogue build_id recorded in the gold file; drift/regression detection, not ground truth'}
        save(root,'acceptance-research.json',result)
        return result
    finally:
        catalog.close()


def p95(values):
    return sorted(values)[max(0,int(len(values)*.95+.999)-1)]

def benchmark(db,root,samples=100):
    if not 20<=samples<=1000:raise ValueError('samples must be 20–1000')
    import psutil
    from mcp import Client,StdioServerParameters
    queries=['R1DIABE','R1CESD10','kidney disease','Self-reported Memory','Body Mass Index','糖尿病','日常生活能力','抑郁']
    book=Codebook(db);pure=[];local=[]
    for i in range(samples+10):
        q=queries[i%len(queries)]
        effective=book.invoke('search_codebook',query=q)['data']['effective_terms']
        expression=' OR '.join('('+literal_fts(t)+')' for t in effective)
        a=time.perf_counter()
        book.con.execute('SELECT source_id,bm25(search_fts) FROM search_fts WHERE search_fts MATCH ? ORDER BY bm25(search_fts),source_id LIMIT 5',(expression,)).fetchall()
        b=time.perf_counter();book.invoke('search_codebook',query=q);c=time.perf_counter()
        if i>=10:pure.append((b-a)*1000);local.append((c-b)*1000)
    book.close();before=sha256(db)
    async def run():
        process=psutil.Process();existing={p.pid for p in process.children(recursive=True)}
        params=StdioServerParameters(command=sys.executable,args=['-m','charls_codebook_mcp.server','--db',str(Path(db).resolve())],cwd=str(Path(root).resolve()))
        start=time.perf_counter();latencies=[];sizes=[];rss=[];ports=[]
        async with Client(params,mode='legacy',read_timeout_seconds=30) as client:
            tools=await client.list_tools();startup=(time.perf_counter()-start)*1000
            children=[p for p in process.children(recursive=True) if p.pid not in existing]
            for i in range(samples+10):
                a=time.perf_counter();r=await client.call_tool('search_codebook',{'query':queries[i%len(queries)]});b=time.perf_counter()
                value=r.structured_content
                if not value or value['status']!='ok':raise ValueError('benchmark query failed')
                if i>=10:latencies.append((b-a)*1000);sizes.append(len(r.model_dump_json(by_alias=True).encode()))
                for p in children:
                    if p.is_running():
                        rss.append(p.memory_info().rss)
                        ports.extend([str(c.laddr) for c in p.net_connections(kind='inet') if c.status=='LISTEN'])
            protocol=client.protocol_version
        remaining=[p.pid for p in process.children(recursive=True) if p.pid not in existing and p.is_running()]
        return {'startup_to_tool_discovery_ms':startup,'stdio_p95_ms':p95(latencies),'stdio_median_ms':statistics.median(latencies),
                'stdio_max_ms':max(latencies),'max_call_tool_result_bytes':max(sizes),'server_peak_sampled_rss_mib':max(rss)/1024**2,
                'protocol':protocol,'tools':[t.name for t in tools.tools],'listening_inet_ports':sorted(set(ports)),
                'remaining_child_pids':remaining}
    result=asyncio.run(run())
    result.update(samples=samples,warmup=10,pure_fts_sql_p95_ms=p95(pure),local_query_p95_ms=p95(local),
                  database_hash_unchanged=before==sha256(db),machine={'os':platform.platform(),'cpu':platform.machine(),
                  'logical_cpus':psutil.cpu_count(),'ram_gib':psutil.virtual_memory().total/1024**3},
                  method='Single serial SDK Client with legacy initialize handshake; 10 warmup calls then measured samples; psutil polling each call; no OS network isolation')
    save(root,'benchmark.json',result)
    return result
