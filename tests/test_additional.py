import json
import sqlite3
import pytest
from charls_codebook_mcp.query import Codebook,encoded_size,MAX_RESPONSE_BYTES
from charls_codebook_mcp.storage import open_readonly

def test_unpublished_rejected(synthetic_db):
    c=sqlite3.connect(synthetic_db);c.execute('UPDATE documents SET published=0');c.commit();c.close()
    with pytest.raises(ValueError,match='UNPUBLISHED'):open_readonly(synthetic_db)

def test_build_bound_cursor(synthetic_db):
    b=Codebook(synthetic_db);cursor=b.invoke('search_codebook',query='alpha',limit=1)['next_cursor'];b.close()
    c=sqlite3.connect(synthetic_db);c.execute("UPDATE documents SET build_id='new_build'");c.commit();c.close()
    b=Codebook(synthetic_db)
    try:assert b.invoke('search_codebook',query='alpha',limit=1,cursor=cursor)['status']=='stale_cursor'
    finally:b.close()

def test_source_cursor_param_bound(book):
    sid=book.invoke('get_variable',variable_name='R1TEST')['data']['source_id']
    r=book.invoke('read_source',source_id=sid,max_chars=100)
    assert book.invoke('read_source',source_id=sid,max_chars=200,cursor=r['next_cursor'])['status']=='stale_cursor'

def test_search_serialized_budget_and_original_snippet(book):
    r=book.invoke('search_codebook',query='alpha',limit=20)
    assert encoded_size(r)<=MAX_RESPONSE_BYTES
    for hit in r['data']['results']:
        body=book.con.execute('SELECT body FROM sections WHERE source_id=?',(hit['source_id'],)).fetchone()[0]
        assert body[hit['snippet_start_char']:hit['snippet_end_char']]==hit['snippet']
        assert hit['search_snippet_is_normalized'] is True

def test_cursor_tampering_and_offset_cap(book):
    r=book.invoke('search_codebook',query='alpha',limit=1);cursor=r['next_cursor']
    assert book.invoke('search_codebook',query='alpha',limit=1,cursor=cursor[:-1]+'Z')['status']=='invalid_cursor'

def test_source_empty_invalid_sizes(book):
    sid=book.invoke('get_variable',variable_name='R1TEST')['data']['source_id']
    for n in [0,8001,True,1.5]:assert book.invoke('read_source',source_id=sid,max_chars=n)['status']=='invalid_argument'

def test_sdk_missing_and_extra_uniform(book):
    import asyncio
    from mcp import Client
    from charls_codebook_mcp.server import make_server,Response
    async def check():
        async with Client(make_server(book),mode='legacy') as c:
            for name,args in [('search_codebook',{}),('read_source',{'source_id':'bad','shell':'whoami'}),('get_document_info',{'document_id':5})]:
                r=await c.call_tool(name,args);v=r.structured_content
                assert Response.model_validate(v).status=='invalid_argument'
    asyncio.run(check())
