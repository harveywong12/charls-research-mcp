"""Exercise generated launch configurations using the official model-independent SDK."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tomllib
from mcp import Client, StdioServerParameters
from charls_codebook_mcp.registry import tool_names, profile_from_args

ROOT = Path(__file__).resolve().parents[1]


def check_always_allow(expected):
    """Report how a client's auto-approval list compares with the server tool set.

    Read-only: this never edits client settings. A machine without a Zoo Code
    settings file reports SKIPPED instead of failing, so the script stays usable
    on other clients and platforms.
    """
    path = Path.home()/('Library/Application Support/Positron/User/globalStorage/'
                        'zoocodeorganization.zoo-code/settings/mcp_settings.json')
    if not path.is_file():
        return {'status': 'SKIPPED', 'reason': 'no Zoo Code settings file on this machine', 'path': str(path)}
    data = json.loads(path.read_text(encoding='utf-8'))
    entry = (data.get('mcpServers') or {}).get('charls_codebook') or {}
    if not entry:
        return {'status': 'NOT_CONFIGURED', 'reason': 'charls_codebook entry not present', 'path': str(path)}
    allow = entry.get('alwaysAllow')
    if allow is None:
        return {'status': 'NO_LIST', 'reason': 'alwaysAllow unset; every call needs manual approval', 'path': str(path)}
    missing = sorted(set(expected)-set(allow))
    extra = sorted(set(allow)-set(expected))
    status = 'MISSING_ENTRIES' if missing else ('EXTRA_ENTRIES' if extra else 'MATCH')
    return {'status': status, 'missing': missing, 'extra': extra,
            'listed': len(allow), 'expected': len(expected), 'path': str(path)}


async def main():
    configurations = []
    for name, key in [('generic-mcp.json', 'mcpServers'), ('claude-cursor.json', 'mcpServers'),
                      ('vscode-mcp.json', 'servers')]:
        data = json.loads((ROOT / 'config/generated' / name).read_text())
        configurations.append((name, data[key]['charls_codebook']))
    data = tomllib.loads((ROOT / 'config/generated/codex.toml').read_text())
    configurations.append(('codex.toml', data['mcp_servers']['charls_codebook']))
    db = ROOT / 'data/processed/codebook.sqlite'
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    results = []
    for name, entry in configurations:
        params = StdioServerParameters(command=entry['command'], args=entry['args'], cwd=str(ROOT / 'data/raw'))
        async with Client(params, mode='legacy', read_timeout_seconds=30) as client:
            tools = await client.list_tools()
            expected=set(tool_names(profile_from_args(entry['args'])))
            assert {t.name for t in tools.tools} == expected
            calls=0
            if 'get_document_info' in expected:
                info = (await client.call_tool('get_document_info', {})).structured_content
                search = (await client.call_tool('search_codebook', {'query': 'R1DIABE'})).structured_content
                variable = (await client.call_tool('get_variable', {'variable_name': 'R1DIABE'})).structured_content
                source = (await client.call_tool('read_source', {'source_id': search['data']['results'][0]['source_id']})).structured_content
                assert all(v['status'] == 'ok' for v in [info, search, source])
                assert variable['status'] in {'ok', 'needs_review', 'raw_only'}
                assert variable['data']['name'] == 'R1DIABE'
                assert search['data']['results'][0]['matched_variable'] == 'R1DIABE'
                calls=4
            if 'get_research_overview' in expected:
                overview=(await client.call_tool('get_research_overview',{})).structured_content
                assert overview['status']=='ok' and overview['data']['participant_rows_indexed']==0
                calls+=1
            results.append({'configuration': name, 'status': 'PASS', 'calls': calls, 'tools':len(expected), 'protocol': client.protocol_version})
    assert before == hashlib.sha256(db.read_bytes()).hexdigest()
    always_allow = check_always_allow(expected)
    if always_allow['status'] == 'MISSING_ENTRIES':
        print('WARNING: client alwaysAllow is missing tools:', always_allow['missing'], file=sys.stderr)
    report = {'status': 'PASS', 'runs': results, 'database_hash_unchanged': True,
              'always_allow': always_allow,
              'scope': 'Config parsing and real SDK stdio launch; NOT a GUI-client or hosted-model integration test'}
    (ROOT / 'reports').mkdir(exist_ok=True)
    (ROOT / 'reports/client-configs.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    asyncio.run(main())
