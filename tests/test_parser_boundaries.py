import json
import sqlite3
from charls_codebook_mcp.build import IndexWriter
from charls_codebook_mcp.storage import SCHEMA,validate_connection

def test_illustration_not_harmonized_entry_and_continuation(tmp_path):
    p=tmp_path/'synthetic-boundaries.sqlite';c=sqlite3.connect(p);c.row_factory=sqlite3.Row;c.executescript(SCHEMA)
    c.execute('INSERT INTO documents VALUES(?,?,?,?,1)',('doc_fixture','fixture','{}','{}'))
    texts=[
        '3. Structure of Codebook 17\nExample\nWave Variable Label Type\n1 R1ILLUSTRATION r1illustration:w1 example Categ\nDescriptive Statistics\n',
        'Section Z: Synthetic 24\nActual group\nWave Variable Label Type\n1 R1REAL r1real:w1 synthetic actual Categ\n',
        'Section Z: Synthetic 25\n2 R2REAL r2real:w2 synthetic continuation Categ\nDescriptive Statistics\nCHARLS Variables Used:\nRAW777 quoted source only\n',
        'Section Z: Synthetic 26\nNext group\nWave Variable Label Type\n3 R3REAL r3real:w3 synthetic next Categ\nDescriptive Statistics\n']
    w=IndexWriter(c,'doc_fixture')
    for n,t in enumerate(texts,1):
        c.execute('INSERT INTO pages VALUES(?,?,?,?,?,?)',('doc_fixture',n,None,t,'parsed','[]'));w.page(n,t)
    w.finish();c.commit()
    assert {r[0] for r in c.execute('SELECT name FROM variables')}=={'R1REAL','R2REAL','R3REAL'}
    a=c.execute("SELECT source_id FROM variables WHERE name='R1REAL'").fetchone()[0]
    b=c.execute("SELECT source_id FROM variables WHERE name='R2REAL'").fetchone()[0]
    assert a==b and c.execute("SELECT source_id FROM variables WHERE name='R3REAL'").fetchone()[0]!=a
    assert not w.unparsed_rows
    assert validate_connection(c)['status']=='PASS';c.close()
