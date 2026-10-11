#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$project_dir/.venv/bin/python" -m charls_codebook_mcp.server --db "$project_dir/data/processed/codebook.sqlite"
