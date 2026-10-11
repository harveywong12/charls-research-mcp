"""Task-based (L1) evaluation of document retrieval: does search_all_codebooks put the answer page in reach?

Three orderings are compared on the same candidate set: the pre-change order (old), the ordering the tool
historically used (v1) and a candidate document-level de-duplication (v2, OFFLINE only, not implemented).

Each task is a real research need from this project's own verified records (reports/private/VARIABLE-MAP-I4.md,
reports/private/HUMAN-REVIEW-CHECKLIST.md, reports/private/LITERATURE-CHEN2019.md), with the answer fixed as a specific
document locator plus an anchor string that must appear on that locator. Anchors were taken from records
committed BEFORE this evaluation (they were never picked after looking at retrieval results), and every
anchor is re-verified here by a whitespace-insensitive containment check.

Measured with the real tool path (ResearchCatalog.search_all_codebooks, i.e. the new match_quality
ordering), paginating up to --rounds pages of 10 hits:
  answer_rank      rank of the first hit that equals the answer locator (None if not found in the budget)
  answer_in_top_k  whether that rank is within k
  rounds_to_answer ceil(rank / 10), i.e. how many result pages an agent must read
For comparison the same query is ranked offline under the OLD kind-first ordering on the same candidate
set (literal term, no alias expansion), so the difference is attributable to ordering alone.

Read-only: opens the catalogue with mode=ro, writes only reports/task-eval-<utc>.json (not versioned).
No observation rows are read. Usage: .venv/bin/python scripts/task_eval.py [--rounds 5] [--verify-only]
"""
import argparse
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from charls_codebook_mcp.catalog import ResearchCatalog

DEFAULT_CATALOG = 'data/processed/research-catalog-20261011-2013fix2.sqlite'
DEFAULT_ALIASES = 'config/research-aliases.json'
KIND_RANK = {'codebook': 0, 'questionnaire': 1}
CUTOFFS = (1, 3, 5, 10)
ROUND_HITS = 10
OFFLINE_MODES = ('old', 'v1', 'v1L', 'm1', 'v2')   # historical comparators; m1 = capped literal-first
LITERAL_CAP = 5      # m1: only the first N literal hits keep the reserved front slots

TASKS = []  # Real-source fixtures are loaded only from a private file.

NORM = lambda text: re.sub(r'\s+', '', text)


def verify_anchors(con):
    """Every anchor must really appear on its declared locator, else the gold is wrong."""
    problems = []
    for task in TASKS:
        row = con.execute('SELECT text FROM document_pages WHERE document_id=? AND page=?',
                          (task['document_id'], task['locator'])).fetchone()
        if row is None or NORM(task['anchor']) not in NORM(row['text']):
            problems.append({'id': task['id'], 'document_id': task['document_id'],
                             'locator': task['locator'], 'anchor': task['anchor'],
                             'reason': 'locator missing' if row is None else 'anchor not found on locator'})
    return problems


def candidates_for(con, term):
    """Historical broad candidate rule; differs from research-2 token boundaries."""
    ascii_term = not re.search(r'[\u4e00-\u9fff]', term)
    token_sub = ('p.rowid IN (SELECT p2.rowid FROM document_fts f JOIN document_pages p2 '
                 'ON p2.document_id=f.document_id AND p2.page=CAST(f.page AS INTEGER) WHERE document_fts MATCH ?)')
    clauses = ([token_sub] if ascii_term else []) + ['instr(p.text, ?) > 0', "p.text LIKE ? ESCAPE '\\'"]
    args = ([json.dumps(term)] if ascii_term else []) + [term, '%' + term + '%']
    tier = ('CASE WHEN ' + (token_sub if ascii_term else '0') + ' THEN 0 WHEN (instr(p.text, ?) > 0) THEN 1 ELSE 2 END')
    tier_args = ([json.dumps(term)] if ascii_term else []) + [term]
    literal_case = "CASE WHEN (instr(p.text, ?) > 0 OR p.text LIKE ? ESCAPE '\\') THEN 1 ELSE 0 END"
    sql = ('SELECT p.document_id, p.page, d.group_name, r.kind, ' + tier + ' AS tier, ' + literal_case + ' AS literal '
           'FROM document_pages p JOIN documents d USING(document_id) JOIN resources r USING(resource_id) '
           'WHERE (' + ' OR '.join(clauses) + ") AND d.trust_status='verified_local'")
    return [dict(r) for r in con.execute(sql, tier_args + [term, '%' + term + '%'] + args)]


def _base_key(r):
    """tier -> kind -> group -> document -> page, the ordering the runtime had before literal-first."""
    return (r['tier'], KIND_RANK.get(r['kind'], 2), r['group_name'] or '', r['document_id'], r['page'])


def offline_rank(con, term, answer, mode):
    """Rank of the answer under an offline ordering on the same candidate set.

    old = kind -> group -> document -> page (the pre-2026-10-11 order)
    v1  = match_tier -> kind -> group -> document -> page (historical ordering)
    v2  = match_tier -> page ordinal inside its document -> kind -> group -> document -> page
          (candidate document-level de-duplication: every document contributes its first page before
           any document contributes its second, measured OFFLINE only - not implemented in the runtime)
    """
    rows = candidates_for(con, term)
    if mode == 'old':
        key = lambda r: (KIND_RANK.get(r['kind'], 2), r['group_name'] or '', r['document_id'], r['page'])
    elif mode == 'v1':
        key = _base_key
    elif mode == 'v2':
        ordinal, seen = {}, {}
        for r in sorted(rows, key=lambda r: (r['document_id'], r['page'])):
            seen[r['document_id']] = seen.get(r['document_id'], 0) + 1
            ordinal[(r['document_id'], r['page'])] = seen[r['document_id']]
        key = lambda r: (r['tier'], ordinal[(r['document_id'], r['page'])], KIND_RANK.get(r['kind'], 2),
                         r['group_name'] or '', r['document_id'], r['page'])
    elif mode == 'v1L':
        key = lambda r: (0 if r['literal'] else 1, *_base_key(r))
    elif mode == 'm1':
        literal_sorted = sorted((r for r in rows if r['literal']), key=_base_key)
        ordinal = {(r['document_id'], r['page']): i for i, r in enumerate(literal_sorted, 1)}
        def key(r):
            if r['literal'] and ordinal[(r['document_id'], r['page'])] <= LITERAL_CAP:
                return (0, *_base_key(r))
            return (1, *_base_key(r))
    else:
        raise ValueError(mode)
    rows.sort(key=key)
    for i, r in enumerate(rows, 1):
        if (r['document_id'], r['page']) == answer:
            return i
    return None


MODES = ('default', 'literal')          # default = tool default (alias expansion on), literal = expand=false


def rank_of(catalog, query, answer, rounds, expand):
    """First rank of the answer locator while paging the real tool; None if it is outside the budget.

    Rank is an ABSOLUTE ordinal over returned hits: a call can come back short because the response byte
    budget stops it before `limit`, so counting `limit` per round would overstate every rank.
    """
    cursor, calls, seen = None, 0, 0
    for rnd in range(1, rounds + 1):
        response = catalog.invoke('search_all_codebooks', query=query, limit=ROUND_HITS, cursor=cursor, expand=expand)
        calls += 1
        if response['status'] not in {'ok', 'needs_review'}:
            break
        for hit in response['data']['results']:
            seen += 1
            if (hit['document_id'], hit['locator']) == answer:
                return {'rank': seen, 'rounds': rnd, 'returned_in_rounds': seen, 'query': query,
                        'match_quality': hit.get('match_quality'), 'effective_terms': response['data']['effective_terms'],
                        'calls': calls}
        cursor = response.get('next_cursor')
        if not cursor:
            break
    return {'rank': None, 'rounds': None, 'returned_in_rounds': seen, 'query': query, 'match_quality': None,
            'effective_terms': response['data']['effective_terms'] if calls else [], 'calls': calls}


def run_task(catalog, con, task, rounds):
    answer = (task['document_id'], task['locator'])
    found = {}
    for mode in MODES:
        expand = None if mode == 'default' else False
        best = None
        for query in task['queries']:
            hit = rank_of(catalog, query, answer, rounds, expand)
            if hit['rank'] is not None and (best is None or hit['rank'] < best['rank']):
                best = hit
        found[mode] = best or rank_of(catalog, task['queries'][0], answer, rounds, expand)
    offline = {}
    for mode in OFFLINE_MODES:
        best = None
        for query in task['queries']:
            rank = offline_rank(con, query, answer, mode)
            if rank is not None and (best is None or rank < best):
                best = rank
        offline[mode] = best
    entry = {'id': task['id'], 'question': task['question'], 'queries': task['queries'],
             'answer': {'document_id': task['document_id'], 'locator': task['locator'],
                        'pdf_page_number': None if task['document_id'].startswith('doc_') else task['locator'],
                        'text_segment_number': task['locator'] if task['document_id'].startswith('doc_') else None},
             'anchor': task['anchor'], 'anchor_strength': task['anchor_strength'], 'provenance': task['provenance'],
             'offline_ranks': offline,
             'old_order_rank': offline['old'], 'old_order_mrr': round(1 / offline['old'], 4) if offline['old'] else 0.0,
             'v1_offline_rank': offline['v1'], 'v2_offline_rank': offline['v2']}
    for mode in MODES:
        hit = found[mode]
        entry[f'{mode}_rank'] = hit['rank']
        entry[f'{mode}_mrr'] = round(1 / hit['rank'], 4) if hit['rank'] else 0.0
        entry[f'{mode}_query'] = hit['query'] if hit['rank'] else None
        entry[f'{mode}_rounds'] = hit['rounds']
        entry[f'{mode}_match_quality'] = hit['match_quality']
        entry[f'{mode}_returned_in_rounds'] = hit.get('returned_in_rounds')
        entry[f'{mode}_effective_terms'] = hit['effective_terms']
        for k in CUTOFFS:
            entry[f'{mode}_in_top{k}'] = bool(hit['rank'] and hit['rank'] <= k)
    return entry


def summarize(entries):
    total = len(entries)

    def agg(rank_key, mrr_key):
        def rate(k):
            return round(sum(1 for e in entries if e[rank_key] and e[rank_key] <= k) / total, 4)
        return {'Answer@1': rate(1), 'Answer@3': rate(3), 'Answer@5': rate(5), 'Answer@10': rate(10),
                'MRR': round(sum(e[mrr_key] for e in entries) / total, 4),
                'found_within_budget': sum(1 for e in entries if e[rank_key])}

    def offline_agg(mode):
        def rate(k):
            return round(sum(1 for e in entries if e['offline_ranks'][mode] and e['offline_ranks'][mode] <= k) / total, 4)
        return {'Answer@1': rate(1), 'Answer@3': rate(3), 'Answer@5': rate(5), 'Answer@10': rate(10),
                'MRR': round(sum(1 / e['offline_ranks'][mode] if e['offline_ranks'][mode] else 0.0 for e in entries) / total, 4),
                'found_within_budget': sum(1 for e in entries if e['offline_ranks'][mode])}

    summary = {'tasks': total}
    summary.update({f'offline_{mode}': offline_agg(mode) for mode in OFFLINE_MODES})
    for mode in MODES:
        summary[f'new_ordering_{mode}'] = agg(f'{mode}_rank', f'{mode}_mrr')
    summary['mean_rounds_default'] = round(sum((e['default_rounds'] or 0) for e in entries) / total, 2)
    summary['tasks_lost_by_alias_expansion'] = [e['id'] for e in entries
                                                if e['literal_rank'] and not e['default_rank']]
    summary['v2_vs_v1'] = {'improved': [e['id'] for e in entries if e['v2_offline_rank'] and e['v1_offline_rank']
                                        and e['v2_offline_rank'] < e['v1_offline_rank']],
                           'worsened': [e['id'] for e in entries if e['v2_offline_rank'] and e['v1_offline_rank']
                                        and e['v2_offline_rank'] > e['v1_offline_rank']]}
    summary['v1_vs_old'] = {'improved': [e['id'] for e in entries if e['v1_offline_rank'] and e['old_order_rank']
                                         and e['v1_offline_rank'] < e['old_order_rank']],
                            'regressed': [e['id'] for e in entries if e['v1_offline_rank'] and e['old_order_rank']
                                          and e['v1_offline_rank'] > e['old_order_rank']]}
    return summary


def main():
    global TASKS
    parser = argparse.ArgumentParser()
    parser.add_argument('--catalog', default=DEFAULT_CATALOG)
    parser.add_argument('--aliases', default=DEFAULT_ALIASES)
    parser.add_argument('--rounds', type=int, default=5)
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--gold-file',type=Path,default=Path('reports/private/task-gold.json'))
    args = parser.parse_args()
    if not args.gold_file.is_file(): raise SystemExit('Provide a private --gold-file; real fixtures are not distributed')
    TASKS=json.loads(args.gold_file.read_text(encoding='utf-8'))
    if not TASKS: raise SystemExit('Private task gold is empty')

    con = sqlite3.connect(f'file:{args.catalog}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    problems = verify_anchors(con)
    print(f"anchor self-check: {len(TASKS) - len(problems)}/{len(TASKS)} pass")
    for p in problems:
        print(f"  FAIL {p['id']} {p['document_id']} p{p['locator']} anchor={p['anchor']!r} ({p['reason']})")
    if args.verify_only:
        con.close()
        return 1 if problems else 0

    catalog = ResearchCatalog(args.catalog, args.aliases)
    try:
        entries = [run_task(catalog, con, task, args.rounds) for task in TASKS]
    finally:
        catalog.close()
        con.close()
    result = {'generated_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'catalogue': args.catalog, 'round_hits': ROUND_HITS, 'rounds_budget': args.rounds,
              'anchor_check': {'passed': len(TASKS) - len(problems), 'total': len(TASKS), 'problems': problems},
              'summary': summarize(entries), 'tasks': entries}
    reports = Path('reports')
    reports.mkdir(exist_ok=True)
    out = reports / f"task-eval-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    s = result['summary']
    for label, key in (('offline: old ordering', 'offline_old'),
                       ('offline: v1 = tool now', 'offline_v1'),
                       ('offline: v1L = literal-first', 'offline_v1L'),
                       ('offline: m1 = capped v1L', 'offline_m1'),
                       ('offline: v2 = doc de-dup', 'offline_v2'),
                       ('tool: expand=false', 'new_ordering_literal'),
                       ('tool: tool default', 'new_ordering_default')):
        m = s[key]
        print(f"{label:<28} Answer@1={m['Answer@1']:.3f} Answer@3={m['Answer@3']:.3f} Answer@5={m['Answer@5']:.3f} "
              f"Answer@10={m['Answer@10']:.3f} MRR={m['MRR']:.3f} found={m['found_within_budget']}/{s['tasks']}")
    print(f"mean rounds to answer (default)={s['mean_rounds_default']}")
    print(f"v1 vs old: improved {s['v1_vs_old']['improved']}  regressed {s['v1_vs_old']['regressed']}  "
          f"| v2 vs v1: improved {s['v2_vs_v1']['improved']}  worsened {s['v2_vs_v1']['worsened']}  "
          f"| lost by alias expansion: {s['tasks_lost_by_alias_expansion']}")
    for e in entries:
        print(f"  {e['id']} off old={e['old_order_rank']} v1={e['v1_offline_rank']} v2={e['v2_offline_rank']} "
              f"m1={e['offline_ranks']['m1']} | tool {e['literal_rank']}/{e['default_rank']} "
              f"({e['literal_match_quality']}) {e['question'][:28]}")
    print(f"  written: {out}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
