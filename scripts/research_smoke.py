"""Existing bounded research checks over a real stdio MCP connection."""
import asyncio
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from mcp import Client,StdioServerParameters
from charls_codebook_mcp.server import Response
from charls_codebook_mcp.registry import tool_names

ROOT=Path(__file__).resolve().parents[1]


def sha(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog',required=True,type=Path,help='Explicit published private catalogue')
    parser.add_argument('--profile',choices=('research','compat'),default='research')
    options=parser.parse_args()
    db=ROOT/'data/processed/codebook.sqlite'
    catalog=options.catalog.resolve()
    files=[catalog]+([db] if options.profile=='compat' else [])
    before={str(p):sha(p) for p in files}
    launch=[str(ROOT/'scripts/offline_server.py'),'--profile',options.profile,'--catalog',str(catalog),'--aliases',str(ROOT/'config/research-aliases.json')]
    if options.profile=='compat':launch+=['--db',str(db)]
    params=StdioServerParameters(command=sys.executable,args=launch,cwd=str(ROOT/'data/raw'))
    calls=[]
    async with Client(params,mode='legacy',read_timeout_seconds=30) as client:
        listed=await client.list_tools()
        names={t.name for t in listed.tools}
        assert names==set(tool_names(options.profile))
        assert all(t.annotations.read_only_hint and not t.annotations.open_world_hint for t in listed.tools)
        async def call(tool,args):
            start=time.perf_counter()
            result=await client.call_tool(tool,args)
            value=result.structured_content
            Response.model_validate(value)
            size=len(result.model_dump_json(by_alias=True).encode())
            assert size<32768,(tool,size)
            assert value['status'] not in {'internal_error','invalid_argument'},(tool,value)
            calls.append({'tool':tool,'status':value['status'],'bytes':size,'elapsed_ms':round((time.perf_counter()-start)*1000,2)})
            return value
        overview=await call('get_research_overview',{})
        index_counts=overview['data']['index_counts']
        assert overview['data']['participant_rows_indexed']==0
        assert index_counts['datasets']>0 and index_counts['data_variables']>0 and index_counts['documents']>0
        if options.profile=='compat':
            await call('get_document_info',{})
            legacy=await call('search_codebook',{'query':'R1DIABE'})
            await call('get_variable',{'variable_name':'R1DIABE'})
            await call('read_source',{'source_id':legacy['data']['results'][0]['source_id']})
        for tool,args,key in [('list_datasets',{'limit':40},'datasets'),('list_indicator_topics',{'limit':15},'topics')]:
            cursor=None;ids=[]
            while True:
                result=await call(tool,{**args,'cursor':cursor})
                ids.extend(item.get('dataset_id',item.get('topic')) for item in result['data'][key])
                cursor=result['next_cursor']
                if not cursor:break
            assert len(ids)==len(set(ids))
            if tool=='list_datasets':
                assert len(ids)==index_counts['datasets']
                first_dataset=ids[0]
        await call('list_dataset_variables',{'dataset_id':first_dataset,'limit':20})
        cystatin=await call('search_data_variables',{'query':'cystatin','namespace':'raw','limit':10})
        assert cystatin['data']['literal_match_count']>=1
        assert 'kidney' not in cystatin['data']['effective_terms']
        assert cystatin['data']['expansion_hints']
        blood=cystatin['data']['results'][0]['dataset_id']
        dense=await call('list_dataset_variables',{'dataset_id':blood,'limit':50})
        assert dense['truncated'] is False and dense['data']['fields']=='standard'
        assert 'internal mapping' in dense['data']['wave_basis']
        assert dense['data']['variable_count']==dense['data']['dataset']['nvar']==dense['data']['dataset_variable_count']
        assert dense['data']['variables'][0]['citation']['provenance_reference'].startswith('response.data.dataset')
        subset=await call('list_dataset_variables',{'dataset_id':blood,'limit':50,'name_prefix':'new','fields':'brief'})
        assert subset['data']['variable_count']==len(subset['data']['variables'])
        assert subset['data']['dataset_variable_count']==dense['data']['dataset_variable_count']
        for query in ['抑郁','糖尿病','握力','认知','血糖']:
            matrix=await call('find_indicator_across_years',{'query':query,'max_per_year':5})
            assert [r['year'] for r in matrix['data']['year_matrix']]==[2011,2013,2015,2018,2020]
        await call('find_indicator_across_years',{'query':'抑郁','namespace':'harmonized','max_per_year':5})
        variables=await call('search_data_variables',{'query':'dc018','year':2020,'limit':20})
        variable=variables['data']['results'][0]
        codes=await call('get_data_variable',{'dataset_id':variable['dataset_id'],'variable_name':variable['name'],'limit':100})
        assert codes['data']['name']=='dc018'
        await call('get_variable_evidence',{'dataset_id':variable['dataset_id'],'variable_name':variable['name']})
        previous=await call('search_data_variables',{'query':'dc011','year':2015,'namespace':'raw','limit':1})
        older=previous['data']['results'][0]
        comparison=await call('compare_variables',{'variables':[
            {'dataset_id':older['dataset_id'],'variable_name':older['name']},
            {'dataset_id':variable['dataset_id'],'variable_name':variable['name']}]})
        assert comparison['data']['cross_year_equivalence']=='not_established'
        assert len(comparison['data']['field_comparisons'])==9
        feasibility=await call('assess_research_feasibility',{'indicators':[
            {'query':'抑郁','role':'exposure'},{'query':'认知','role':'outcome'}],
            'years':[2011,2013,2015,2018,2020],'limit':1})
        assert feasibility['data']['analysis_ready'] is False and feasibility['next_cursor']
        await call('assess_research_feasibility',{'indicators':[
            {'query':'抑郁','role':'exposure'},{'query':'认知','role':'outcome'}],
            'years':[2011,2013,2015,2018,2020],'limit':1,'cursor':feasibility['next_cursor']})
        inventory=await call('list_research_documents',{'limit':20,'include_unverified':True})
        assert inventory['data']['documents']
        docs=await call('search_all_codebooks',{'query':'糖尿病','group':'2020-Wave5','limit':10})
        assert docs['data']['results'] and any(r['source_kind']=='questionnaire' for r in docs['data']['results'])
        hit=docs['data']['results'][0];chunks=[];cursor=None
        while True:
            result=await call('read_codebook_page',{'document_id':hit['document_id'],'page':hit['locator'],'max_chars':700,'cursor':cursor})
            chunks.append(result['data']['text']);cursor=result['next_cursor']
            if not cursor:break
        assert len(''.join(chunks))==result['data']['total_chars']
    assert before=={str(p):sha(p) for p in files}
    assert {c['tool'] for c in calls}==names
    report={'status':'PASS','profile':options.profile,'tools':sorted(names),'calls':calls,'catalogue_hash_unchanged':True,
            'version_D_hash_unchanged':True if options.profile=='compat' else None,'offline_guard':'Python socket/DNS and PDF-import guards; not an OS sandbox',
            'scope':'actual SDK stdio calls; GUI and hosted-model behavior not tested'}
    (ROOT/'reports'/('research-stdio-'+options.profile+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'status':'PASS','tools':len(names),'calls':len(calls),'max_response_bytes':max(c['bytes'] for c in calls)},ensure_ascii=False))


if __name__=='__main__':asyncio.run(main())
