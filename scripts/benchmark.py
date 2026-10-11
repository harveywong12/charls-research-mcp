from pathlib import Path
import json
from charls_codebook_mcp.verification import benchmark
root=Path(__file__).resolve().parents[1]
print(json.dumps(benchmark(root/'data/processed/codebook.sqlite',root,100),ensure_ascii=False,indent=2))
