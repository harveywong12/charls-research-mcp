"""Research-2 regressions over wholly fictional documents and variables."""
import asyncio
import hashlib
import json
import sqlite3

import pytest
from mcp import Client

from charls_codebook_mcp.catalog import ResearchCatalog, token_contains
from charls_codebook_mcp.catalog_build import SCHEMA4
from charls_codebook_mcp.catalog_enrich import enrich_catalog
from charls_codebook_mcp.indicator_evidence import field_evidence, EVIDENCE_FIELDS
from charls_codebook_mcp.presentation import finish_page, PAGE_BUDGET
from charls_codebook_mcp.query import envelope, encoded_size
from charls_codebook_mcp.registry import tool_names
from charls_codebook_mcp.server import make_server


@pytest.fixture
def evidence_catalog(tmp_path):
    path=tmp_path/'fictional.sqlite'
    con=sqlite3.connect(path);con.executescript(SCHEMA4)
    con.executemany('INSERT INTO meta VALUES(?,?)',[('published','1'),('build_id','fictional'),('build_inputs','{}'),('metadata_adapter','synthetic')])
    facts=[]
    for i,(year,name,unit) in enumerate([(2015,'example_old','unit_a'),(2020,'example_new','unit_b')],1):
        group='2015' if year==2015 else '2020-Wave5'
        ds='ds_'+format(i,'024x');doc='pdf_'+format(i,'024x');rid='r'+str(i);drid='d'+str(i)
        text=f'{name} measures fictional Synthetic outcome in {unit}.\n'
        sha=hashlib.sha256(text.encode()).hexdigest()
        con.execute('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?,?,?)',(rid,group,'dataset','fictional','/fictional.zip','https://example.org',sha,1,'catalogued',None))
        con.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(ds,rid,group,'raw',year,1,'fictional','fictional.dta',sha,1,1,118,'fictional'))
        con.execute('INSERT INTO data_variables VALUES(?,?,?,?,?,?,?,?,?)',(ds,name,'Synthetic outcome','float','',json.dumps([]),year,1,'unlabelled'))
        con.execute('INSERT INTO dataset_columns VALUES(?,?,?)',(ds,name,0))
        con.execute('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?,?,?)',(drid,group,'questionnaire','fictional','/fictional.pdf','https://example.org',sha,1,'catalogued',None))
        con.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?)',(doc,drid,group,'fictional.pdf','fictional',sha,1,'pdf','pdf_physical_page','verified_local','[]'))
        con.execute('INSERT INTO document_sources VALUES(?,?,?,?,?,?,?)',(doc,'source'+str(i),drid,group,'https://example.org','fictional','{}'))
        con.execute('INSERT INTO document_pages VALUES(?,?,?,?)',(doc,1,text,'text_extracted'))
        con.execute('INSERT INTO document_fts VALUES(?,?,?)',(doc,1,text))
        fields={field:{'value':None,'status':'unconfirmed','source_indexes':[],'reason':'No fictional source support'} for field in EVIDENCE_FIELDS}
        for field,value in [('construct','Synthetic outcome'),('unit',unit)]:
            fields[field]={'value':value,'status':'document_explicit','source_indexes':[0],'reason':'Explicit fictional passage'}
        source={'document_sha256':sha,'document_id':doc,'page':1,'anchor':text.strip(),'start_char':0,'end_char':len(text.strip()),'locator_type':'pdf_physical_page'}
        fact={'year':year,'namespace':'raw','module':'fictional','variable_name':name,'construct':'Synthetic outcome','construct_key':'fictional_outcome','unit':unit,'search_terms':'Synthetic outcome','review_status':'reviewed_against_source_and_metadata','sources':[source],'evidence_fields':fields}
        con.execute('INSERT INTO reviewed_variable_facts VALUES(?,?,?)',(ds,name,json.dumps(fact)))
        facts.append(fact)
    con.commit();con.close()
    aliases=tmp_path/'aliases.json';aliases.write_text('[]')
    catalog=ResearchCatalog(path,aliases)
    yield catalog,path,aliases,facts
    catalog.close()


@pytest.mark.parametrize('text,term,expected',[
    ('bl_tg','TG',True),('mortgage','TG',False),('neighborhood','GHB',False),
    ('GHB (arbitrary)','GHB',True),('ABC123','ABC',False),('abc_123','ABC',True),
    ('total cholesterol','total cholesterol',True)])
def test_token_boundaries(text,term,expected):
    assert bool(token_contains(text,term)) is expected


def test_field_evidence_not_promoted():
    result=field_evidence({'construct':'legacy value','unit':'arbitrary'})
    assert all(value['status']=='unconfirmed' for value in result.values())
    assert result['unit']['value']=='arbitrary'


def test_field_comparison_and_evidence_dedup(evidence_catalog):
    catalog,_,_,_=evidence_catalog
    a={'dataset_id':'ds_'+format(1,'024x'),'variable_name':'example_old'}
    b={'dataset_id':'ds_'+format(2,'024x'),'variable_name':'example_new'}
    evidence=catalog.get_variable_evidence(**a)['data']
    assert evidence['field_evidence']['construct']['status']=='document_explicit'
    assert 'evidence_fields' not in evidence['reviewed_fact']
    assert 'field_evidence' not in evidence['definition_summary']
    result=catalog.compare_variables([a,b])['data']
    verdict={row['field']:row['verdict'] for row in result['field_comparisons']}
    assert verdict['construct']=='documented_agreement'
    assert verdict['unit']=='documented_difference'
    assert verdict['sampling']=='insufficient_evidence'
    assert result['cross_year_equivalence']=='not_established'
    assert result['comparability_status']=='requires_adjustment_review'


def test_batch_overlap_uses_unreturned_indicators(evidence_catalog):
    catalog,_,_,_=evidence_catalog
    indicators=[{'query':'Synthetic outcome','role':'exposure'},{'query':'missing fictional indicator','role':'outcome'}]
    first=catalog.assess_research_feasibility(indicators,[2015,2020],limit=1)
    assert len(first['data']['indicators'])==1
    assert first['data']['common_candidate_years']==[]
    assert first['data']['common_document_confirmed_years']==[]
    assert first['data']['analysis_ready'] is False
    second=catalog.assess_research_feasibility(indicators,[2015,2020],limit=1,cursor=first['next_cursor'])
    assert second['data']['indicators'][0]['query']=='missing fictional indicator'
    assert not second['next_cursor']
    confirmed=catalog.assess_research_feasibility(indicators[:1],[2015,2020])
    assert confirmed['data']['common_document_confirmed_years']==[2015,2020]


@pytest.mark.parametrize('args',[
    {'indicators':[],'years':[2015]},
    {'indicators':[{'query':'x','role':'wrong'}],'years':[2015]},
    {'indicators':[{'query':'x','role':'outcome','extra':'x'}],'years':[2015]},
    {'indicators':[{'query':'x','role':'outcome'}],'years':[True]},
    {'indicators':[{'query':'x','role':'outcome'}],'years':[2015,2015]},
    {'indicators':[{'query':'x','role':'outcome'}],'years':[2020],'namespace':'harmonized'},
    {'indicators':[{'query':'x','role':'outcome'}],'years':[2015],'limit':True},
])
def test_batch_invalid_arguments(evidence_catalog,args):
    assert evidence_catalog[0].invoke('assess_research_feasibility',**args)['status']=='invalid_argument'


def test_cursor_binds_alias_and_runtime_revision(evidence_catalog,monkeypatch):
    import charls_codebook_mcp.catalog as module
    catalog,path,aliases,_=evidence_catalog
    first=catalog.search_data_variables('Synthetic outcome',limit=1)
    assert first['next_cursor']
    aliases.write_text('[{"term":"fictional","english":["example"]}]')
    other=ResearchCatalog(path,aliases)
    try:
        assert other.invoke('search_data_variables',query='Synthetic outcome',limit=1,cursor=first['next_cursor'])['status']=='stale_cursor'
    finally:other.close()
    monkeypatch.setattr(module,'RETRIEVAL_REVISION','fictional-next')
    assert catalog.invoke('search_data_variables',query='Synthetic outcome',limit=1,cursor=first['next_cursor'])['status']=='stale_cursor'


def test_whole_envelope_budget_and_progress():
    items=[{'id':str(i),'text':'合成'*900} for i in range(4)]
    response=envelope(data={'results':items,'documents':{str(i):{'text':'出处'*300} for i in range(4)}})
    result=finish_page(response,'results',4,0,4,lambda offset:str(offset),('documents','id'))
    assert encoded_size(result)<=PAGE_BUDGET
    assert 1<=result['data']['returned_count']<4
    assert result['next_cursor']==str(result['data']['returned_count'])
    assert result['data']['truncation_reason']=='byte_budget'
    assert set(result['data']['documents'])=={r['id'] for r in result['data']['results']}
    huge=envelope(data={'results':[{'text':'合成'*15000}]})
    result=finish_page(huge,'results',1,0,1,str)
    assert result['data']['oversize_item'] is True and result['data']['returned_count']==1


@pytest.mark.parametrize('profile',['research','compat','legacy'])
def test_profiles_sdk_and_native_types(profile,book,evidence_catalog):
    async def run():
        async with Client(make_server(book,evidence_catalog[0],profile),mode='legacy') as client:
            assert {t.name for t in (await client.list_tools()).tools}==set(tool_names(profile))
            if profile!='legacy':
                for args in [[],False,{'indicators':[{'query':'x','role':True}],'years':[2015]}, {'indicators':[{'query':'x','role':'outcome'}],'years':[True]}]:
                    # Some SDKs validate the top-level arguments object before transport.
                    if not isinstance(args,dict):continue
                    value=(await client.call_tool('assess_research_feasibility',args)).structured_content
                    assert value['status']=='invalid_argument'
    asyncio.run(run())


def test_enrichment_is_additive_and_immutable(evidence_catalog,tmp_path):
    _,path,_,facts=evidence_catalog
    evidence=tmp_path/'facts.json';evidence.write_text(json.dumps({'schema_version':2,'facts':[facts[0]]}))
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    target=tmp_path/'new.sqlite'
    with pytest.raises(sqlite3.IntegrityError):enrich_catalog(path,evidence,target)
    assert not target.exists() and not target.with_name('new.sqlite.partial').exists()
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    with pytest.raises(ValueError):enrich_catalog(path,evidence,path)
