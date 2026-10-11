"""TEST ONLY: transparent byte relay; SDK remains responsible for the protocol."""
import subprocess
import sys
import threading
from pathlib import Path

capture,db=sys.argv[1:]
child=subprocess.Popen([sys.executable,'-m','charls_codebook_mcp.server','--profile','legacy','--db',db],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=None)
def stdin():
    while data:=sys.stdin.buffer.read1(65536):
        child.stdin.write(data);child.stdin.flush()
    child.stdin.close()
thread=threading.Thread(target=stdin,daemon=True);thread.start()
with Path(capture).open('wb') as f:
    while line:=child.stdout.readline():
        f.write(line);f.flush();sys.stdout.buffer.write(line);sys.stdout.buffer.flush()
sys.exit(child.wait(timeout=10))
