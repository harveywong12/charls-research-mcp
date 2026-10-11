"""Fixed, bounded SQL templates. No acquisition, PDF parser or subprocess imports."""
import base64
import hashlib
import json
import re
import sqlite3
import threading
from .storage import open_readonly

MAX_RESPONSE_BYTES = 10000  # Leaves room for SDK text/structured duplication and escaping.
NO_HIT = '仅表示当前 Version D、当前索引和查询下未可靠命中；不能据此断言整个 CHARLS 未测量。'
RAW_WARNING = '编码、特殊缺失、构建和跨轮次说明保留为共享原文，未逐变量结构化；须读取 source_id 核对。'

def encoded_size(value):
    return len(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf-8'))

def envelope(status='ok',data=None,citations=None,warnings=None,truncated=False,next_cursor=None):
    return {'status':status,'data':data,'citations':citations or [],'warnings':warnings or [],
            'truncated':truncated,'next_cursor':next_cursor}

def escape_like(value):
    return value.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')

def literal_fts(query):
    terms = re.findall(r'[^\W_]+(?:_[^\W_]+)*',query,flags=re.UNICODE)
    if not terms or len(terms)>16: raise ValueError('查询需含文字或数字，最多 16 个词项')
    return ' AND '.join('"'+t.replace('"','""')+'"' for t in terms)

class QueryError(ValueError):
    def __init__(self,code,message): self.code,self.message=code,message

class Codebook:
    def __init__(self,path):
        self.con=open_readonly(path)
        self.lock=threading.RLock()

    def close(self): self.con.close()

    def invoke(self,method,**kwargs):
        with self.lock:
            try: return getattr(self,method)(**kwargs)
            except QueryError as e: return envelope(e.code,{'message':e.message})
            except (ValueError,TypeError) as e: return envelope('invalid_argument',{'message':str(e)[:300]})
            except sqlite3.Error: return envelope('internal_error',{'message':'数据库读取失败，不能视为无结果'})
            except Exception: return envelope('internal_error',{'message':'服务内部错误'})

    def _document(self,document_id=None):
        if document_id is not None and (not isinstance(document_id,str) or len(document_id)>80):
            raise ValueError('document_id 无效')
        docs=self.con.execute('SELECT * FROM documents ORDER BY document_id').fetchall()
        if document_id is None:
            if len(docs)!=1:
                raise QueryError('document_ambiguity','多个文档，请明确 document_id：'+','.join(d['document_id'] for d in docs))
            return docs[0]
        matches=[d for d in docs if d['document_id']==document_id]
        if not matches: raise QueryError('version_conflict','document_id 不属于当前已启用数据库')
        return matches[0]

    def _citation(self,s,span):
        label=self.con.execute('SELECT printed_page_label FROM pages WHERE document_id=? AND pdf_page_number=?',
                               (s['document_id'],span['pdf_page_number'])).fetchone()
        return {'source_id':s['source_id'],'document_id':s['document_id'],'version':'D',
                **span,'printed_page_label':label[0] if label else None}

    def _range_citations(self,row,start,end):
        cites=[];position=0
        for span in json.loads(row['source_spans']):
            length=span['end_char']-span['start_char']
            left=max(start,position);right=min(end,position+length)
            if right>left:
                part=dict(span,start_char=span['start_char']+left-position,end_char=span['start_char']+right-position)
                cites.append(self._citation(row,part))
            position+=length+1
        return cites

    def _cursor(self,context,offset):
        payload={'context':context,'offset':offset}
        data=json.dumps(payload,sort_keys=True,separators=(',',':')).encode()
        # Digest detects accidental corruption. Not a security/authentication token.
        return base64.urlsafe_b64encode(data).decode().rstrip('=')+'.'+hashlib.sha256(data).hexdigest()[:20]

    def _offset(self,cursor,context):
        if cursor is None:return 0
        if not isinstance(cursor,str) or len(cursor)>2048: raise QueryError('invalid_cursor','游标格式错误')
        try:
            b,digest=cursor.split('.')
            data=base64.urlsafe_b64decode(b+'='*(-len(b)%4))
            payload=json.loads(data)
            if hashlib.sha256(data).hexdigest()[:20]!=digest:raise ValueError()
        except Exception: raise QueryError('invalid_cursor','游标格式或校验错误') from None
        if payload.get('context')!=context:
            raise QueryError('stale_cursor','游标与当前文档、build_id、查询或参数不一致')
        offset=payload.get('offset')
        if type(offset) is not int or offset<0:raise QueryError('invalid_cursor','游标偏移错误')
        return offset

    def get_document_info(self,document_id=None):
        d=self._document(document_id); m=json.loads(d['manifest'])
        states={r[0]:r[1] for r in self.con.execute('SELECT extraction_status,count(*) FROM pages WHERE document_id=? GROUP BY extraction_status',(d['document_id'],))}
        counts={t:self.con.execute(f'SELECT count(*) FROM {t} WHERE document_id=?',(d['document_id'],)).fetchone()[0] for t in ['pages','sections','variables']}
        data={k:m[k] for k in ['title','version','release','coverage','sha256','bytes','physical_pages','source_page','requested_url','final_url','acquired_at']}
        data.update(document_id=d['document_id'],build_id=d['build_id'],parser=json.loads(d['parser']),
                    counts=counts,page_states=states,structured_fields=['name','label','wave (when explicit)','subject (checked naming rule)'],
                    coding_coverage='raw_only',human_reviewed=0,rights='Original documentation attribution remains with its authors.')
        return envelope(data=data,warnings=[RAW_WARNING,'raw_text 是解析器提取文本；原 PDF 是最终版式核对对象。'])

    def search_codebook(self,query,document_id=None,limit=5,cursor=None):
        if not isinstance(query,str) or not 0<len(query.strip())<=256:raise ValueError('query 不能为空或超过 256 字符')
        if type(limit) is not int or not 1<=limit<=20:raise ValueError('limit 必须为 1–20 的整数')
        query=query.strip(); doc=self._document(document_id)
        warnings=[]; expansions=[]; effective=[]
        aliases=self.con.execute('SELECT * FROM aliases ORDER BY term').fetchall()
        for a in aliases:
            if a['term'] in query if re.search(r'[\u4e00-\u9fff]',a['term']) else a['term'].casefold()==query.casefold():
                terms=json.loads(a['english'])
                effective.extend(terms)
                expansions.append({'term':a['term'],'english':terms,'evidence':json.loads(a['evidence']),'review_status':a['review_status']})
                warnings.append('术语扩展仅用于候选检索，不表示变量定义等价。')
                if a['ambiguous']:warnings.append('缩写或口径有歧义，请读取原文并明确研究定义。')
        if len(effective)>8:raise ValueError('同义扩展超过 8 项')
        if not effective:
            if re.search(r'[\u4e00-\u9fff]',query):
                return envelope('no_results',{'results':[],'effective_terms':[],'expansions':[]},warnings=['中文术语未收录，请改用英文或变量名。',NO_HIT])
            effective=[query]
        expression=' OR '.join('('+literal_fts(t)+')' for t in effective)
        context={'kind':'search','document_id':doc['document_id'],'build_id':doc['build_id'],
                 'query':query,'effective':effective,'limit':limit,'order':'priority,bm25_asc,source_id'}
        offset=self._offset(cursor,context)
        if offset>1000:raise QueryError('invalid_cursor','分页最多 1000 个结果；请缩小查询')
        like='%'+escape_like(query)+'%'
        rows=self.con.execute('''SELECT s.*, bm25(search_fts,0,0,8,12,1) AS score,
          snippet(search_fts,4,'[',']',' … ',32) AS search_snippet,
          CASE WHEN EXISTS(SELECT 1 FROM variables v WHERE v.source_id=s.source_id AND v.name=? COLLATE NOCASE) THEN 0
            WHEN s.title LIKE ? ESCAPE '\\' OR EXISTS(SELECT 1 FROM variables v WHERE v.source_id=s.source_id AND v.label LIKE ? ESCAPE '\\') THEN 1
            ELSE 2 END AS priority
          FROM search_fts JOIN sections s ON s.source_id=search_fts.source_id
          WHERE search_fts MATCH ? AND s.document_id=?
          ORDER BY priority ASC, score ASC, s.source_id ASC LIMIT ? OFFSET ?''',
          (query,like,like,expression,doc['document_id'],limit+1,offset)).fetchall()
        results=[]
        for row in rows[:limit]:
            variables=self.con.execute('SELECT name FROM variables WHERE source_id=? ORDER BY name',(row['source_id'],)).fetchall()
            names=[v[0] for v in variables]
            hit=next((n for n in names if n.casefold()==query.casefold()),None)
            body=row['body']; words=re.findall(r'\w+',effective[0]); term=words[0] if words else ''
            found=body.lower().find(term.lower()); at=max(0,found-100) if found>=0 else 0
            # Original extraction text, never use the whitespace-normalized search copy as evidence.
            snippet=body[at:at+320]
            spans=json.loads(row['source_spans'])
            cites=self._range_citations(row,at,at+len(snippet))
            item={'source_id':row['source_id'],'title':row['title'],'matched_variable':hit,'snippet':snippet,
                  'snippet_start_char':at,'snippet_end_char':at+len(snippet),
                  'search_snippet':row['search_snippet'],'search_snippet_is_normalized':True,
                  'source_page_range':[spans[0]['pdf_page_number'],spans[-1]['pdf_page_number']],
                  'source_span_count':len(spans),
                  'citations':cites,'match_type':['exact_variable','label_or_title','full_text'][row['priority']],
                  'extraction_status':row['extraction_status'],'review_status':row['review_status'],
                  'score':row['score'],'score_interpretation':'BM25 排序分数，越小越好；不是可信度概率',
                  'version':'D'}
            if encoded_size(results+[item])>MAX_RESPONSE_BYTES-1800:
                if not results:
                    # Compact entry-level citations; full spans remain accessible by read_source.
                    item['citations']=[cites[0],cites[-1]] if len(cites)>1 else cites
                    item['citation_summary']=True
                else:break
            results.append(item)
        more=len(rows)>len(results)
        next_cursor=self._cursor(context,offset+len(results)) if more and results else None
        if not results:warnings.append(NO_HIT)
        if any(r['match_type']=='full_text' for r in results):warnings.append('正文提及属于候选匹配，不代表独立 Harmonized 变量。')
        out=envelope('ok' if results else 'no_results',{'results':results,'effective_terms':effective,'expansions':expansions},
                        warnings=list(dict.fromkeys(warnings)),truncated=more,next_cursor=next_cursor)
        while encoded_size(out)>MAX_RESPONSE_BYTES and len(results)>1:
            results.pop();out['truncated']=True;out['next_cursor']=self._cursor(context,offset+len(results))
        if encoded_size(out)>MAX_RESPONSE_BYTES and results:
            cites=results[0]['citations'];results[0]['citations']=[cites[0],cites[-1]] if len(cites)>1 else cites
            results[0]['citation_summary']=True
        return out

    def get_variable(self,variable_name,document_id=None):
        if not isinstance(variable_name,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',variable_name):
            raise ValueError('variable_name 必须是 1–64 位真实变量名')
        doc=self._document(document_id)
        row=self.con.execute('SELECT * FROM variables WHERE document_id=? AND name=? COLLATE NOCASE',
                             (doc['document_id'],variable_name)).fetchone()
        if not row:
            candidate=self.search_codebook(variable_name,doc['document_id'],limit=3)
            return envelope('raw_only' if candidate['data']['results'] else 'no_results',
                {'variable':None,'candidates':candidate['data']['results'],'namespace':'unconfirmed'},
                warnings=[NO_HIT,'结构化表中未确认此变量；正文引用可能属于原始 CHARLS 命名空间。'])
        evidence=json.loads(row['evidence'])
        section=self.con.execute('SELECT * FROM sections WHERE source_id=?',(row['source_id'],)).fetchone()
        data={k:row[k] for k in ['name','label','wave','subject','year','source_id','extraction_status','review_status']}
        data.update(document_id=doc['document_id'],version='D',namespace='harmonized',evidence=evidence,
                    value_codes=None,missing_values=None,construction=None)
        cites=[self._citation(section,evidence['table_row'])]
        if row['subject']:
            cites.append({'document_id':doc['document_id'],'version':'D','pdf_page_number':12,'printed_page_label':'11','basis':'1.4 Variable Naming Convention'})
        return envelope('needs_review',data,cites,[RAW_WARNING,'标签按 PDF 原文保留，可能本来就存在缩写或截短。'])

    def read_source(self,source_id,cursor=None,max_chars=8000):
        if not isinstance(source_id,str) or not re.fullmatch(r'src_[a-f0-9]{24}',source_id):raise ValueError('source_id 只能是本库生成的 ID')
        if type(max_chars) is not int or not 1<=max_chars<=8000:raise ValueError('max_chars 必须为 1–8000 的整数')
        row=self.con.execute('SELECT * FROM sections WHERE source_id=?',(source_id,)).fetchone()
        if not row:return envelope('no_results',{'source_id':source_id},warnings=['未知 source_id'])
        doc=self._document(row['document_id']); body=row['body']
        context={'kind':'source','document_id':doc['document_id'],'build_id':doc['build_id'],'source_id':source_id,'max_chars':max_chars}
        offset=self._offset(cursor,context)
        if offset>len(body):raise QueryError('invalid_cursor','游标超出原文长度')
        take=min(max_chars,len(body)-offset)
        def result(size):
            end=offset+size;cites=self._range_citations(row,offset,end)
            more=end<len(body)
            return envelope('ok',{'source_id':source_id,'title':row['title'],'text':body[offset:end],
                                  'start_char':offset,'end_char':end,'total_chars':len(body),'remaining_chars':len(body)-end,
                                  'join_rule':'source spans joined with one newline',
                                  'extraction_status':row['extraction_status'],'review_status':row['review_status']},
                            cites,[RAW_WARNING],more,self._cursor(context,end) if more else None)
        out=result(take)
        while encoded_size(out)>MAX_RESPONSE_BYTES and take>1:
            take=max(1,take//2);out=result(take)
        return out
