"""Does literal-first ordering hide pages that are useful but reachable only through alias expansion?

Two measurements on the 2013fix2 catalogue, both read-only and both re-runnable:

A. Mechanical displacement over ALL alias terms in config/research-aliases.json. For every concept term the
   candidate set is built exactly like the runtime (literal term OR expansion terms, trusted documents only)
   and ranked twice on that same set:
     v1  = match_tier -> kind -> group -> document -> page            (runtime before 2026-10-11)
     v1L = literal_match -> match_tier -> kind -> group -> document -> page   (historical comparator)
   Reported per term: how many expansion-only (non-literal) pages sit in the top-10 under each ordering,
   which pages are pushed out of the top-10 by the change, and which pages the change pulls in. This answers
   "how much moved", not "was it a loss".

B. Semantic gold for 12 concept queries. Each required page is a page that (i) really belongs to the concept,
   (ii) does NOT contain the term the user typed, and (iii) does contain an expansion term - i.e. exactly the
   kind of page literal-first could bury. Anchor provenance is recorded per entry; every entry is self-checked
   here (page must exist and satisfy (ii) and (iii)), and the rank of each required page is reported under v1
   and v1L. Sources stay in reports/private/. These historical broad candidate rules differ from
   research-2 token boundaries and must not be presented as current runtime acceptance.

Writes reports/expansion-impact-<utc>.json (not versioned). No observation rows, no writes to the catalogue.
Usage: .venv/bin/python scripts/expansion_impact.py [--catalog PATH] [--aliases PATH]
"""
import argparse
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CATALOG = 'data/processed/research-catalog-20261011-2013fix2.sqlite'
DEFAULT_ALIASES = 'config/research-aliases.json'
KIND_RANK = {'codebook': 0, 'questionnaire': 1}
CJK = re.compile(r'[\u4e00-\u9fff]')
REQUIRED = []  # Real-source fixtures are loaded only from a private file.



def matches(text, term):
    return term in text or term.casefold() in text.casefold()


def alias_terms(aliases_path):
    aliases = json.loads(Path(aliases_path).read_text('utf-8'))
    out = []
    for alias in aliases:
        term, english = alias.get('term'), alias.get('english')
        if isinstance(term, str) and isinstance(english, list):
            members = [t for t in english if isinstance(t, str)]
            if members:
                out.append((term, members))
    return out


def candidates(con, term, members):
    """Historical broad concept candidate rule, not the research-2 runtime rule."""
    pages = con.execute("SELECT p.document_id,p.page,p.text,r.kind,d.group_name FROM document_pages p "
                        "JOIN documents d USING(document_id) JOIN resources r USING(resource_id) "
                        "WHERE d.trust_status='verified_local'").fetchall()
    rows = []
    for row in pages:
        literal = matches(row['text'], term)
        hit_members = [m for m in members if matches(row['text'], m)]
        if not literal and not hit_members:
            continue
        text = row['text']
        all_terms = ([term] if literal else []) + hit_members
        token = any(re.search(r'(?<![A-Za-z0-9_])' + re.escape(t) + r'(?![A-Za-z0-9_])', text)
                    for t in all_terms if not CJK.search(t))
        exact = any(t in text for t in all_terms)
        rows.append({'document_id': row['document_id'], 'page': row['page'], 'kind': row['kind'],
                     'group': row['group_name'], 'literal': literal, 'members': hit_members,
                     'tier': 0 if token else (1 if exact else 2)})
    return rows


LITERAL_CAP = 5      # M1: only the first N literal hits keep the reserved front slots


def _base_key(r):
    return (r['tier'], KIND_RANK.get(r['kind'], 2), r['group'] or '', r['document_id'], r['page'])


def rank(rows, ordering):
    """v1 = pre-literal-first, v1L = literal-first for every literal hit, m1 = capped literal-first."""
    if ordering == 'v1':
        return sorted(rows, key=_base_key)
    if ordering == 'v1L':
        return sorted(rows, key=lambda r: (0 if r['literal'] else 1, *_base_key(r)))
    if ordering == 'm1':
        literal_sorted = sorted((r for r in rows if r['literal']), key=_base_key)
        ordinal = {(r['document_id'], r['page']): i for i, r in enumerate(literal_sorted, 1)}
        def key(r):
            if r['literal'] and ordinal[(r['document_id'], r['page'])] <= LITERAL_CAP:
                return (0, *_base_key(r))
            return (1, *_base_key(r))          # literal hits beyond the cap merge with expansion-only hits
        return sorted(rows, key=key)
    raise ValueError(ordering)


def part_a(con, aliases):
    per_term, lost_total, gained_total, expansion_only_top10 = [], 0, 0, 0
    for term, members in aliases:
        rows = candidates(con, term, members)
        v1, v1l = rank(rows, 'v1'), rank(rows, 'v1L')
        top1 = {(r['document_id'], r['page']) for r in v1[:10]}
        topL = {(r['document_id'], r['page']) for r in v1l[:10]}
        lost = [r for r in v1[:10] if (r['document_id'], r['page']) not in topL]
        gained = [r for r in v1l[:10] if (r['document_id'], r['page']) not in top1]
        exp_top1 = sum(1 for r in v1[:10] if not r['literal'])
        exp_topL = sum(1 for r in v1l[:10] if not r['literal'])
        lost_total += len(lost)
        gained_total += len(gained)
        expansion_only_top10 += exp_top1 - exp_topL
        per_term.append({'term': term, 'literal_pages': sum(1 for r in rows if r['literal']),
                         'expansion_only_pages': sum(1 for r in rows if not r['literal']),
                         'expansion_only_in_top10_v1': exp_top1, 'expansion_only_in_top10_v1L': exp_topL,
                         'lost_from_top10': [{'document_id': r['document_id'], 'page': r['page'], 'kind': r['kind'],
                                              'tier': r['tier'], 'members': r['members'][:3]} for r in lost],
                         'gained_into_top10': [{'document_id': r['document_id'], 'page': r['page'], 'kind': r['kind'],
                                                'tier': r['tier'], 'literal': r['literal']} for r in gained]})
    return {'aliases': len(aliases), 'terms_with_literal_pages': sum(1 for p in per_term if p['literal_pages']),
            'terms_without_literal_pages': sum(1 for p in per_term if not p['literal_pages']),
            'expansion_only_top10_before': sum(p['expansion_only_in_top10_v1'] for p in per_term),
            'expansion_only_top10_after': sum(p['expansion_only_in_top10_v1L'] for p in per_term),
            'expansion_only_slots_replaced': expansion_only_top10,
            'lost_pages_total': lost_total, 'gained_pages_total': gained_total,
            'terms_with_any_loss': [p['term'] for p in per_term if p['lost_from_top10']],
            'terms_with_any_gain': [p['term'] for p in per_term if p['gained_into_top10']],
            'per_term': per_term}


def load_required(path):
    """Private base REQUIRED plus an optional external gold file produced by a separate reviewer.

    External schema (reports/private/expansion-gold-extra.json): {entries: [{concept, provenance, required_pages:
    [{document_id, locator, why}]}]}. Every entry is self-checked in part B, so a wrong page fails loudly.
    """
    merged = list(REQUIRED)
    if path is None:
        return merged, []
    payload = json.loads(Path(path).read_text('utf-8'))
    external = []
    for entry in payload.get('entries', []):
        pages = [(pg['document_id'], pg['locator'], pg.get('why', '')) for pg in entry.get('required_pages', [])]
        if pages:
            external.append((entry['concept'], pages, entry.get('provenance', 'external gold file')))
    return merged + external, [e[0] for e in external]


def part_b(con, aliases, required):
    member_of = {term: members for term, members in aliases}
    checks, results = [], []
    for term, pages, provenance in required:
        members = member_of.get(term, [])
        entry = {'term': term, 'provenance': provenance, 'required': []}
        for document_id, page, why in pages:
            row = con.execute('SELECT text FROM document_pages WHERE document_id=? AND page=?', (document_id, page)).fetchone()
            status = 'ok'
            if row is None:
                status = 'locator_missing'
            elif matches(row['text'], term):
                status = 'contains_literal_term_it_should_not'      # (ii) violated
            elif not any(matches(row['text'], m) for m in members):
                status = 'no_expansion_term_on_page'                # (iii) violated
            checks.append({'term': term, 'document_id': document_id, 'page': page, 'status': status})
            if status != 'ok':
                continue
            rows = candidates(con, term, members)
            target = (document_id, page)
            ranks = {}
            for ordering in ('v1', 'v1L', 'm1'):
                ordered = rank(rows, ordering)
                position = next((i for i, r in enumerate(ordered, 1) if (r['document_id'], r['page']) == target), None)
                ranks[ordering] = {'rank': position,
                                   'in_top10': bool(position and position <= 10),
                                   'in_top50': bool(position and position <= 50)}
            entry['required'].append({'document_id': document_id, 'page': page, 'why': why, **ranks})
        results.append(entry)
    bad = [c for c in checks if c['status'] != 'ok']
    def hits(key, cutoff):
        return sum(1 for e in results for r in e['required'] if r[key][cutoff])
    return {'concepts': len(required), 'required_pages': len(checks), 'self_check_passed': len(checks) - len(bad),
            'self_check_problems': bad, 'per_concept': results, 'literal_cap_m1': LITERAL_CAP,
            'in_top10_v1': hits('v1', 'in_top10'), 'in_top10_v1L': hits('v1L', 'in_top10'), 'in_top10_m1': hits('m1', 'in_top10'),
            'in_top50_v1': hits('v1', 'in_top50'), 'in_top50_v1L': hits('v1L', 'in_top50'), 'in_top50_m1': hits('m1', 'in_top50')}


def main():
    global REQUIRED
    parser = argparse.ArgumentParser()
    parser.add_argument('--catalog', default=DEFAULT_CATALOG)
    parser.add_argument('--aliases', default=DEFAULT_ALIASES)
    parser.add_argument('--base-gold',type=Path,default=Path('reports/private/expansion-base-gold.json'))
    parser.add_argument('--required-file', type=Path, default=Path('reports/private/expansion-gold-extra.json'),
                        help='optional external gold file (schema documented in load_required)')
    args = parser.parse_args()
    if not args.base_gold.is_file(): raise SystemExit('Provide private --base-gold; real fixtures are not distributed')
    REQUIRED=json.loads(args.base_gold.read_text(encoding='utf-8'))
    con = sqlite3.connect(f'file:{args.catalog}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    aliases = alias_terms(args.aliases)
    required, external_concepts = load_required(args.required_file if args.required_file.is_file() else None)
    a = part_a(con, aliases)
    b = part_b(con, aliases, required)
    b['external_gold_file'] = str(args.required_file) if args.required_file.is_file() else None
    b['external_concepts'] = external_concepts
    con.close()
    result = {'generated_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'catalogue': args.catalog,
              'aliases_file': args.aliases, 'part_a_mechanical': a, 'part_b_semantic_gold': b,
              'note': 'part A measures how much moved between v1 and v1L; only part B says whether the moved pages were needed.'}
    reports = Path('reports')
    reports.mkdir(exist_ok=True)
    out = reports / f"expansion-impact-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"A. aliases={a['aliases']} (有字面命中 {a['terms_with_literal_pages']} / 无 {a['terms_without_literal_pages']})")
    print(f"   仅扩展命中占 top-10 的槽位: v1={a['expansion_only_top10_before']} -> v1L={a['expansion_only_top10_after']}"
          f"（净替换 {a['expansion_only_slots_replaced']}）")
    print(f"   被挤出 top-10 的页 {a['lost_pages_total']} 个，新进 {a['gained_pages_total']} 个；"
          f"涉及概念 {len(a['terms_with_any_loss'])} 个")
    if a['terms_with_any_loss']:
        print("   有损失的概念:", ", ".join(a['terms_with_any_loss'][:12]))
    print(f"B. 概念必读页自检 {b['self_check_passed']}/{b['required_pages']} 通过（问题：{b['self_check_problems']}）")
    print(f"   必读页命中: top-10 v1={b['in_top10_v1']} / v1L={b['in_top10_v1L']} / m1(cap={b['literal_cap_m1']})={b['in_top10_m1']}；"
          f"top-50 v1={b['in_top50_v1']} / v1L={b['in_top50_v1L']} / m1={b['in_top50_m1']}")
    for e in b['per_concept']:
        for r in e['required']:
            flag='  <== 被v1L遮住' if r['v1L']['in_top50'] is False and r['v1']['in_top50'] is True else ''
            print(f"     {e['term']:<8} {r['document_id'][:20]} p{r['page']:<5} rank v1 {r['v1']['rank']} -> v1L {r['v1L']['rank']} -> m1 {r['m1']['rank']}{flag}")
    print(f"  written: {out}")


if __name__ == '__main__':
    main()
