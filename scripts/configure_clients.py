"""Generate local stdio launch templates without editing any client's settings."""
import argparse
import json
import os
from pathlib import Path
from charls_codebook_mcp.registry import tool_names, PROFILES


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog',type=Path,help='Explicit versioned local research catalogue')
    parser.add_argument('--profile',choices=PROFILES,help='research (default with catalogue), compat or legacy')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    executable = root / ('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python')
    if not executable.is_file():
        raise SystemExit('Run uv sync --frozen first / 请先安装项目依赖')
    entry = {'command': str(executable), 'args': ['-u', '-m', 'charls_codebook_mcp.server']}
    catalog=args.catalog.resolve() if args.catalog else root/'data/processed/research-catalog.sqlite'
    if args.catalog and not catalog.is_file():
        raise SystemExit('Specified catalogue is missing')
    profile=args.profile or ('research' if catalog.is_file() else 'legacy')
    entry['args'] += ['--profile',profile]
    if profile in ('legacy','compat'):
        entry['args'] += ['--db',str(root/'data/processed/codebook.sqlite')]
    if profile in ('research','compat'):
        if not catalog.is_file(): raise SystemExit('Research profile requires a local catalogue')
        entry['args'] += ['--catalog',str(catalog),'--aliases',str(root/'config/research-aliases.json')]
    output = root / 'config/generated'
    output.mkdir(parents=True, exist_ok=True)
    generic = {'mcpServers': {'charls_codebook': entry}}
    vscode = {'servers': {'charls_codebook': {'type': 'stdio', **entry}}}
    for name, value in [('generic-mcp.json', generic), ('claude-cursor.json', generic),
                        ('vscode-mcp.json', vscode)]:
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    codex = ('[mcp_servers.charls_codebook]\ncommand = ' + json.dumps(entry['command']) +
             '\nargs = ' + json.dumps(entry['args']) +
             '\nstartup_timeout_sec = 30\ntool_timeout_sec = 30\n')
    (output / 'codex.toml').write_text(codex, encoding='utf-8')
    print('Generated templates only / 仅生成模板:', output)
    print('Tool profile:',profile,', tools:',', '.join(tool_names(profile)))
    print('Merge into the chosen client config and restart it / 请合并到所选客户端配置并重启')


if __name__ == '__main__':
    main()
