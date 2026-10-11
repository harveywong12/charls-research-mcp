"""Explicit local acquisition only; never imported by the MCP service."""
import datetime
import hashlib
import json
from pathlib import Path
import shutil
import ssl
import time
import urllib.request

PDF_URL = 'https://charls.charlsdata.com/Public/ashelf/public/uploads/document/harmonized_charls/application/Harmonized_CHARLS_D.pdf'
LANDING_URL = 'https://charls.pku.edu.cn/en/Data/Harmonized_CHARLS.htm'
OFFICIAL_SHA1 = 'af828e94462f26f56cb0b2c188a9c3dad5cfcac0'
MAX_BYTES = 100 * 1024**2

def sha256(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def identify(pdf_path, provenance=None):
    import pdfplumber
    p = Path(pdf_path)
    if not 0 < p.stat().st_size <= MAX_BYTES:
        raise ValueError('PDF 大小无效或超过 100 MiB')
    with p.open('rb') as f:
        if f.read(5) != b'%PDF-':
            raise ValueError('不是 PDF 文件')
    with pdfplumber.open(p) as pdf:
        cover = pdf.pages[0].extract_text() or ''
        acknowledgement = pdf.pages[2].extract_text() or ''
        if 'Harmonized CHARLS' not in cover or 'VERSION D (2011-2018), JUNE 2021' not in cover:
            raise ValueError('VERSION_CONFLICT: 封面不是已支持的 Harmonized CHARLS Version D')
        if 'Waves 1 through 4' not in acknowledgement:
            raise ValueError('VERSION_CONFLICT: 轮次核对失败')
        pdf.pages[-1].extract_text()  # Parse the final page as a truncation check.
        count = len(pdf.pages)
        metadata = {str(k): str(v) for k,v in pdf.metadata.items()}
    with p.open('rb') as f:
        sha1 = hashlib.file_digest(f, 'sha1').hexdigest()
    info = dict(provenance or {})
    info.update(original_filename=info.get('original_filename', p.name),
                source_page=LANDING_URL, requested_url=info.get('requested_url'),
                final_url=info.get('final_url'),
                acquired_at=info.get('acquired_at', datetime.datetime.now(datetime.timezone.utc).isoformat()),
                sha256=sha256(p), sha1=sha1, bytes=p.stat().st_size,
                official_sha1_match=(sha1 == OFFICIAL_SHA1), physical_pages=count,
                title='Harmonized CHARLS Documentation', version='D', release='June 2021',
                coverage={'waves':[1,2,3,4], 'year_range':'2011-2018'},
                verification={'cover':{'pdf_page_number':1,'excerpt':'VERSION D (2011-2018), JUNE 2021'},
                              'waves':{'pdf_page_number':3,'excerpt':'This document used CHARLS Waves 1 through 4 as of June 2021.'},
                              'official_page_sha1':OFFICIAL_SHA1},
                pdf_metadata=metadata, review_status='agent_checked', unknown_fields={})
    return info

def acquire(root, local_pdf=None):
    root = Path(root)
    target = root/'data/raw/Harmonized_CHARLS_D.pdf'
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError('目标 PDF 已存在；不会覆盖。使用 build 或明确的新项目目录。')
    logs = []
    part = target.with_suffix('.part')
    try:
        if local_pdf:
            # Explicit CLI import; this path is never accepted by an MCP tool.
            origin = Path(local_pdf)
            if not origin.is_file() or origin.stat().st_size > MAX_BYTES:
                raise ValueError('导入文件无效')
            shutil.copyfile(origin, part)
            provenance = {'method':'local_import','original_filename':origin.name}
        else:
            for attempt in range(1,4):
                start = time.monotonic()
                try:
                    req = urllib.request.Request(PDF_URL, headers={'User-Agent':'CHARLS-Codebook-Local-Indexer/0.1'})
                    with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as response:
                        if response.status != 200 or not response.url.startswith('https://'):
                            raise ValueError('HTTP 状态或 HTTPS 验证失败')
                        length = response.headers.get('Content-Length')
                        if length and int(length) > MAX_BYTES:
                            raise ValueError('PDF 超过大小上限')
                        total = 0
                        with part.open('wb') as out:
                            while chunk := response.read(65536):
                                total += len(chunk)
                                if total > MAX_BYTES or time.monotonic()-start > 60:
                                    raise ValueError('大小或总时限超过上限')
                                out.write(chunk)
                        if length and total != int(length):
                            raise ValueError('Content-Length 不一致')
                        provenance = {'method':'https_download','original_filename':target.name,'requested_url':PDF_URL,
                                      'final_url':response.url,'http_status':response.status}
                    identify(part, provenance)
                    logs.append({'attempt':attempt,'status':'downloaded','bytes':total})
                    break
                except Exception as exc:
                    part.unlink(missing_ok=True)
                    logs.append({'attempt':attempt,'error':str(exc),'elapsed_seconds':time.monotonic()-start})
            else:
                raise ValueError('BLOCKED: 官方 PDF 下载失败。请手动提供 Version D PDF，再执行 fetch --import-pdf PATH。')
        manifest = identify(part, provenance)
        part.rename(target)
        save_manifest(root, manifest)
        return manifest
    finally:
        part.unlink(missing_ok=True)
        if logs:
            (root/'reports').mkdir(exist_ok=True)
            (root/'reports/download.json').write_text(json.dumps({'requested_url':PDF_URL,'attempts':logs},ensure_ascii=False,indent=2))

def save_manifest(root, manifest):
    p = Path(root)/'data/source/manifest.json'
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
