"""Single-worker page extraction and conservative table-boundary recognition."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from .source import identify, sha256
from .storage import SCHEMA, validate_connection

PARSER_VERSION = 'charls-D-table-v1'
ROW = re.compile(r'^\s*(?:(A|[1-4])\s+)?([A-Z][A-Z0-9_]*)\s+(.+?)\s+(Categ|Cont|Char)\s*$')
CHAPTER = re.compile(r'^(Section [A-Z]: .+?)\s+(\d+)$',re.I)

def stable_id(prefix, value):
    return prefix+hashlib.sha256(value.encode()).hexdigest()[:24]

def normalize(text):
    # Only whitespace in the search copy; punctuation, signs and codes retained.
    return re.sub(r'\s+', ' ', text).strip()

def lines_with_offsets(text):
    at = 0
    for line in text.splitlines(keepends=True):
        yield line.rstrip('\r\n'), at, at+len(line)
        at += len(line)

def page_label(text):
    first = text.splitlines()[0] if text else ''
    # Per-page printed header; never use a global offset or PDF filename.
    m = re.match(r'^(?:Section [A-Z]:|[1-5]\. ).+?\s+(\d+)$',first,re.I)
    return m.group(1) if m else None

class IndexWriter:
    def __init__(self, con, document_id):
        self.con, self.doc = con, document_id
        self.current = None
        self.ordinal = 0
        self.table_mode = False
        self.chapter = None
        self.duplicate_variables = []
        self.unparsed_rows = []

    def start(self, title, page, offset, content_type='variable_group'):
        self.finish()
        self.current = {'source_id':stable_id('src_',f'{self.doc}:{page}:{offset}'),
                        'title':title,'chapter':self.chapter,'spans':[],'pieces':[],
                        'variables':[],'content_type':content_type}
        self.table_mode = False  # Starts only at the explicit table header, never at its title.

    def add_span(self, page, start, end, text):
        if start == end and text: return
        self.current['spans'].append({'pdf_page_number':page,'start_char':start,'end_char':end})
        self.current['pieces'].append(text[start:end])

    def page(self, number, text):
        lines = list(lines_with_offsets(text))
        if lines:
            m = CHAPTER.match(lines[0][0])
            if m:
                new_chapter = m.group(1)
                if self.chapter != new_chapter and self.current:
                    self.finish()
                self.chapter = new_chapter
            elif lines[0][0].startswith('References '):
                self.finish(); self.chapter = 'References'
        boundaries = {}
        for i,(line,start,end) in enumerate(lines):
            if self.chapter and self.chapter.startswith('Section ') and line.strip() == 'Wave Variable Label Type' and i:
                title, offset, _ = lines[i-1]
                if not title or CHAPTER.match(title):
                    continue  # continuation header, not a new logical group
                if i == 2 and CHAPTER.match(lines[0][0]): offset = 0
                boundaries[offset] = title.strip()
        if (not self.current or self.current['content_type'] == 'page_context') and 0 not in boundaries:
            self.start(lines[0][0][:180] if lines else f'PDF page {number}',number,0,'page_context')
        at = 0
        for line,start,end in lines:
            if start in boundaries:
                if start > at: self.add_span(number,at,start,text)
                self.start(boundaries[start],number,start)
                at = start
            if line.strip() == 'Wave Variable Label Type':
                self.table_mode = True
                continue
            if line.startswith(('Descriptive Statistics','Categorical Variable Codes','How Constructed:','CHARLS Variables Used:')):
                self.table_mode = False
            if self.table_mode and self.current['content_type'] == 'variable_group':
                m = ROW.match(line)
                if m:
                    wave,name,label,_ = m.groups()
                    self.current['variables'].append((name,label,wave,{'pdf_page_number':number,'start_char':start,'end_char':end}))
                elif re.match(r'^\s*(?:[1-4A]\s+)?[A-Z][A-Z0-9_]+\s+\S+:',line):
                    self.unparsed_rows.append({'pdf_page_number':number,'start_char':start,'reason':'table row needs review'})
        if 0 in boundaries and at == 0 and self.current['title'] != boundaries[0]:
            # Normal case is handled below: start=0 header is encountered at first line.
            raise ValueError('boundary recognition invariant')
        self.add_span(number,at,len(text),text)

    def finish(self):
        s = self.current
        if s is None: return
        body = '\n'.join(s['pieces'])
        self.con.execute('INSERT INTO sections VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (s['source_id'],self.doc,self.ordinal,None,s['title'],s['chapter'],body,
             json.dumps(s['spans']),s['content_type'],'partial' if s['variables'] else 'raw_only','unreviewed'))
        for name,label,wave,e in s['variables']:
            raw_wave_column = wave
            wave_match = re.match(r'^(?:R|S|HH|H|INW)([1-4])',name)
            wave = wave if wave_match and wave_match.group(1) == wave else None
            subject = None
            if re.match(r'^R(?:[1-4]|A)',name): subject='respondent'
            elif re.match(r'^S(?:[1-4]|A)',name): subject='spouse'
            elif re.match(r'^HH(?:[1-4]|A)',name): subject='household'
            elif re.match(r'^H(?:[1-4]|A)',name): subject='couple'
            evidence={'raw_wave_column':raw_wave_column,'table_row':e,'name':e,'label':e,'wave':e if wave else None,
                      'subject':{'pdf_page_number':12,'rule':'1.4 Variable Naming Convention'} if subject else None,
                      'year':None,'null_reasons':{'year':'not_extracted','wave':'not_explicit_in_table' if not wave else None,
                                                'subject':'not_supported_by_checked_naming_rule' if not subject else None},
                      'codes':'raw_only','missing_values':'raw_only','namespace':'harmonized'}
            try:
                self.con.execute('INSERT INTO variables VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (self.doc,name,label,wave,subject,None,s['source_id'],json.dumps(evidence),'partial','unreviewed'))
            except sqlite3.IntegrityError:
                self.duplicate_variables.append({'name':name,'source_id':s['source_id'],'reason':'duplicate not merged'})
        self.con.execute('INSERT INTO search_fts(source_id,document_id,title,variables,body) VALUES (?,?,?,?,?)',
            (s['source_id'],self.doc,s['title'],' '.join(v[0] for v in s['variables']),normalize(body)))
        self.ordinal += 1
        self.current = None

def build_database(pdf_path, manifest_path, output_path, alias_path=None):
    import pdfplumber
    start = time.monotonic()
    output = Path(output_path)
    if output.exists(): raise ValueError('数据库已存在；请构建新文件，停止服务后显式切换路径')
    saved = json.loads(Path(manifest_path).read_text())
    checked = identify(pdf_path,saved)
    if checked['sha256'] != saved.get('sha256') or checked['version'] != saved.get('version') or checked['physical_pages'] != saved.get('physical_pages'):
        raise ValueError('SOURCE_CONFLICT: PDF 与已核对 manifest 不一致')
    alias_config = json.loads(Path(alias_path).read_text()) if alias_path else []
    parser = {'rules':PARSER_VERSION,'pdfplumber':importlib.metadata.version('pdfplumber'),
              'pdfminer.six':importlib.metadata.version('pdfminer.six'),'normalization':'whitespace_only'}
    doc = stable_id('doc_',checked['sha256'])
    build_id = stable_id('build_',json.dumps([checked['sha256'],parser,alias_config],sort_keys=True))
    output.parent.mkdir(parents=True,exist_ok=True)
    fd,temp = tempfile.mkstemp(prefix='.building-',suffix='.sqlite',dir=output.parent)
    import os
    os.close(fd)
    con = sqlite3.connect(temp)
    con.row_factory = sqlite3.Row
    status_counts = {}
    try:
        con.executescript(SCHEMA)
        con.execute('INSERT INTO documents VALUES(?,?,?,?,0)',(doc,build_id,json.dumps(checked),json.dumps(parser)))
        writer = IndexWriter(con,doc)
        with pdfplumber.open(pdf_path) as pdf:
            for n,page in enumerate(pdf.pages,1):
                warnings=[]
                try:
                    raw=page.extract_text(x_tolerance=3,y_tolerance=3) or ''
                    state='parsed'
                    if not raw.strip():
                        state='raw_only'; warnings.append('scanned_page_no_ocr' if page.images else 'blank_page')
                    elif '\ufffd' in raw or '(cid:' in raw:
                        state='partial'; warnings.append('possible_garbled_text')
                except Exception:
                    raw='';state='failed';warnings.append('page_extraction_failed')
                con.execute('INSERT INTO pages VALUES(?,?,?,?,?,?)',(doc,n,page_label(raw),raw,state,json.dumps(warnings)))
                writer.page(n,raw)
                status_counts[state]=status_counts.get(state,0)+1
                page.close()  # Flush per-page parser caches.
        writer.finish()
        for alias in alias_config:
            evidence=alias.get('evidence')
            review='unreviewed'
            if evidence:
                raw=con.execute('SELECT raw_text FROM pages WHERE document_id=? AND pdf_page_number=?',(doc,evidence['pdf_page_number'])).fetchone()
                if not raw or not evidence.get('excerpt') or evidence['excerpt'].lower() not in raw[0].lower():
                    raise ValueError('alias source anchor not found: '+alias['term'])
                review='agent_checked'
            else:
                evidence={'basis':'editorial retrieval terms, not confirmed measurement definitions',
                          'source_anchor_available':False}
            con.execute('INSERT INTO aliases VALUES (?,?,?,?,?)',
                        (alias['term'],json.dumps(alias['english']),json.dumps(evidence),int(alias.get('ambiguous',False)),review))
        con.execute("INSERT INTO search_fts(search_fts) VALUES('integrity-check')")
        con.execute('UPDATE documents SET published=1')
        con.commit()
        report=validate_connection(con)
        if report['errors']: raise ValueError('SOURCE_MAPPING_FAILED: '+str(report['errors'][:3]))
        con.close()
        # Exclusive publication: never replace an earlier or live database.
        os.link(temp,output)
        report.update(document_id=doc,build_id=build_id,page_states=status_counts,
                      duplicate_variables=writer.duplicate_variables,unparsed_table_rows=writer.unparsed_rows,
                      elapsed_seconds=time.monotonic()-start,parser=parser)
        return report
    finally:
        con.close()
        Path(temp).unlink(missing_ok=True)
