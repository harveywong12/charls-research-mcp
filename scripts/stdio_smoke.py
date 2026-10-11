import asyncio
import json
from pathlib import Path
import sys
from mcp import Client,StdioServerParameters
from charls_codebook_mcp.source import sha256
from charls_codebook_mcp.server import Response

ROOT=Path(__file__).resolve().parents[1]
DB=ROOT/'data/processed/codebook.sqlite'
async def smoke():
    before=sha256(DB);runs=[]
    for mode in ['auto','legacy','offline']:
        wire=ROOT/'reports'/f'stdio-{mode}-wire.jsonl'
        if mode=='offline':
            args=[str(ROOT/'scripts/offline_server.py'),'--profile','legacy','--db',str(DB)]
        else:args=[str(ROOT/'scripts/stdio_tap.py'),str(wire),str(DB)]
        params=StdioServerParameters(command=sys.executable,args=args,cwd=str(ROOT/'data/raw'))
        values=[]
        async with Client(params,mode='legacy' if mode in ['legacy','offline'] else 'auto',read_timeout_seconds=30) as client:
            listed=await client.list_tools()
            names={t.name for t in listed.tools}
            expected={'get_document_info','search_codebook','get_variable','read_source'}
            assert names==expected
            for t in listed.tools:
                assert t.annotations.read_only_hint and t.annotations.open_world_hint is False and t.annotations.destructive_hint is False
                assert t.output_schema
            async def call(name,args):
                result=await client.call_tool(name,args)
                value=result.structured_content
                Response.model_validate(value)
                size=len(result.model_dump_json(by_alias=True).encode())
                assert size<32768,(name,size)
                values.append({'tool':name,'status':value['status'],'bytes':size})
                return value
            await call('get_document_info',{})
            search=await call('search_codebook',{'query':'R1DIABE'})
            assert search['data']['results'][0]['matched_variable']=='R1DIABE'
            await call('get_variable',{'variable_name':'R1DIABE'})
            await call('read_source',{'source_id':search['data']['results'][0]['source_id']})
            for q in ['kidney disease','Self-reported Memory','Body Mass Index','糖尿病','日常生活能力','抑郁','health','血压 糖尿病 肾脏疾病 抑郁 日常生活能力 体质指数 记忆']:
                await call('search_codebook',{'query':q,'limit':20})
            for name,args in [('search_codebook',{'query':'" OR * - : )','limit':21}),('search_codebook',{'query':'x'*257}),
                               ('search_codebook',{}),('search_codebook',{'query':['invalid']}),('search_codebook',{'query':'health','limit':True}),
                               ('get_variable',{'variable_name':'R1DIABE','sql':'DELETE FROM pages'}),
                               ('read_source',{'source_id':'/etc/passwd'}),('get_document_info',{'document_id':'version_E'}),
                               ('search_codebook',{'query':'health','cursor':'bad'}),('search_codebook',{'query':'未收录的中文词'})]:
                value=await call(name,args)
                assert value['status'] in {'invalid_argument','invalid_cursor','version_conflict','no_results'}
            # Long-source continuity over the actual stdio transport.
            sid=search['data']['results'][0]['source_id'];chunks=[];cursor=None
            while True:
                r=await call('read_source',{'source_id':sid,'cursor':cursor,'max_chars':3000})
                chunks.append(r['data']['text']);cursor=r['next_cursor']
                if not cursor:break
            assert len(''.join(chunks))==r['data']['total_chars']
            runs.append({'mode':mode,'protocol':client.protocol_version,'tools':sorted(names),'calls':values,
                         'lossless_source_chars':len(''.join(chunks))})
        if mode!='offline':
            frames=[json.loads(line) for line in wire.read_text().splitlines()]
            assert frames and all(isinstance(f,dict) and f.get('jsonrpc')=='2.0' for f in frames)
            runs[-1]['wire_max_line_bytes']=max(len(line.encode()) for line in wire.read_text().splitlines())
            assert runs[-1]['wire_max_line_bytes']<32768
    report={'status':'PASS','runs':runs,'database_hash_unchanged':before==sha256(DB),
            'cwd_independence':'server started from data/raw rather than project root, using installed module and absolute database path',
            'offline_scope':'Python socket/DNS guards and parser-import guards; not OS firewall isolation'}
    (ROOT/'reports/stdio.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'status':'PASS','modes':[r['mode'] for r in runs],'call_counts':[len(r['calls']) for r in runs]},ensure_ascii=False))
if __name__=='__main__':asyncio.run(smoke())
