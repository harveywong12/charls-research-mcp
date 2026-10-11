"""TEST ONLY: deny Internet sockets and PDF parser imports before service startup."""
import importlib.abc
import socket
import sys

original_connect=socket.socket.connect
def denied_connect(self,*args,**kwargs):
    if self.family in (socket.AF_INET,socket.AF_INET6):raise RuntimeError('test denied Internet socket')
    return original_connect(self,*args,**kwargs)
socket.socket.connect=denied_connect
socket.create_connection=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('test denied network'))
socket.getaddrinfo=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('test denied DNS'))
class DenyPdf(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,*args):
        if fullname.split('.')[0] in {'pdfplumber','pdfminer','pypdfium2'}:
            raise RuntimeError('test denied PDF parser import')
sys.meta_path.insert(0,DenyPdf())
from charls_codebook_mcp.server import main
sys.exit(main())
