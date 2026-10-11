"""Read-only relevance evaluation of the search_all_codebooks ordering (new tiers vs the old kind-first order).

Label rule (objective proxy, confirmed with the user on 2026-10-11):
  ASCII query  -> a page is relevant only when the term occurs case-sensitively as a standalone
                  token, i.e. the characters before and after it are not [A-Za-z0-9_].
  CJK query    -> a page is relevant when the term occurs case-sensitively at all (no word boundaries).
Recall denominators use every trusted page satisfying the same rule, not the candidate set, because
relevance is defined independently of FTS and LIKE.

Candidate set mirrors the runtime for a single literal term with expand disabled:
  trusted documents only, and ( FTS MATCH "term" if the term is ASCII ) OR instr(text, term) > 0 OR text LIKE %term% .
No alias expansion, no observation rows, no writes. Output: reports/relevance-eval-<utc>.json (reports/ is not versioned).

Queries whose term never occurs as a standalone token in this corpus have no relevant page by construction
(label artefacts: "UA" only occurs inside longer upper-case runs such as INDIVIDUAL, and "bloodweight" is glued
to neighbouring words by PDF extraction). Aggregates are therefore reported twice: over all queries, and over
the queries that have at least one relevant page.

Usage: .venv/bin/python scripts/relevance_eval.py [--catalog PATH] [--preview 8]
"""
import argparse
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CATALOG = 'data/processed/research-catalog-20261011-2013fix2.sqlite'
KIND_RANK = {'codebook': 0, 'questionnaire': 1}
CUTOFFS = (1, 3, 5, 10, 50)
METRIC_KEYS = ('p@1', 'p@3', 'p@5', 'p@10', 'mrr', 'r@10', 'r@50')

SHORT_ASCII = ['GHB', 'TG', 'UA', 'ID', 'BP', 'WBC', 'PLT', 'MCV', 'HCT', 'CRP', 'BUN', 'HDL', 'LDL', 'BMI', 'PSU', 'TC']
IDENTIFIERS = ['cystatinc', 'bl_cysc', 'newcrea', 'bl_crea', 'bloodweight', 'communityID', 'eGFR', 'cystatin', 'creatinine']
CJK_TERMS = ['胱抑素', '肌酐', '糖尿病', '高血压', '抑郁', '认知', '握力', '吸烟', '教育', '城乡', '空腹', '冻存盒编号']
CATEGORIES = [('short_ascii', SHORT_ASCII), ('identifier', IDENTIFIERS), ('cjk', CJK_TERMS)]
CAT_OF = {term: name for name, terms in CATEGORIES for term in terms}
QUERIES = [term for _, terms in CATEGORIES for term in terms]
ASCII_TOKEN = lambda term: re.compile(r'(?<![A-Za-z0-9_])' + re.escape(term) + r'(?![A-Za-z0-9_])')
CJK = re.compile(r'[\u4e00-\u9fff]')
NO_RANK = 10 ** 6
ORDERS = ('old', 'new', 'v2')          # old = pre-change order, new = runtime order, v2 = document de-dup (OFFLINE only).
# This harness uses literal single terms only, so alias expansion never runs here and a capped literal-first
# variant would be a no-op; the alias/concept evidence lives in scripts/expansion_impact.py instead.


def kind_rank(kind):
    return KIND_RANK.get(kind, 2)


def fetch_candidates(con, term):
    """Pages the runtime could return for one literal term, with the tier the new ordering assigns."""
    ascii_term = not CJK.search(term)
    token_sub = ('p.rowid IN (SELECT p2.rowid FROM document_fts f JOIN document_pages p2 '
                 'ON p2.document_id=f.document_id AND p2.page=CAST(f.page AS INTEGER) WHERE document_fts MATCH ?)')
    tier = 'CASE WHEN ' + (token_sub if ascii_term else '0') + ' THEN 0 WHEN (instr(p.text, ?) > 0) THEN 1 ELSE 2 END'
    tier_args = ([json.dumps(term)] if ascii_term else []) + [term]
    clauses = ([token_sub] if ascii_term else []) + ['instr(p.text, ?) > 0', "p.text LIKE ? ESCAPE '\\'"]
    args = ([json.dumps(term)] if ascii_term else []) + [term, '%' + term + '%']
    sql = ('SELECT p.document_id, p.page, p.text, d.group_name, r.kind, ' + tier + ' AS tier '
           'FROM document_pages p JOIN documents d USING(document_id) JOIN resources r USING(resource_id) '
           'WHERE (' + ' OR '.join(clauses) + ") AND d.trust_status='verified_local'")
    return [dict(r) for r in con.execute(sql, tier_args + args)]


def corpus_relevant(con, term):
    """Trusted pages that satisfy the label rule, found with a case-sensitive GLOB prefilter."""
    rows = con.execute("SELECT p.document_id, p.page, p.text FROM document_pages p JOIN documents d USING(document_id) "
                       "WHERE p.text GLOB ? AND d.trust_status='verified_local'", ('*' + term + '*',)).fetchall()
    if CJK.search(term):
        return len(rows)                                    # CJK: any case-sensitive occurrence counts
    pattern = ASCII_TOKEN(term)
    return sum(1 for r in rows if pattern.search(r['text']))


def rank_rows(rows, ordering):
    if ordering == 'old':
        key = lambda r: (kind_rank(r['kind']), r['group_name'] or '', r['document_id'], r['page'])
    elif ordering == 'new':
        key = lambda r: (r['tier'], kind_rank(r['kind']), r['group_name'] or '', r['document_id'], r['page'])
    elif ordering == 'v2':
        # Candidate document-level de-duplication: each document's first page lands before any second page.
        ordinal, seen = {}, {}
        for r in sorted(rows, key=lambda r: (r['document_id'], r['page'])):
            seen[r['document_id']] = seen.get(r['document_id'], 0) + 1
            ordinal[(r['document_id'], r['page'])] = seen[r['document_id']]
        key = lambda r: (r['tier'], ordinal[(r['document_id'], r['page'])], kind_rank(r['kind']),
                         r['group_name'] or '', r['document_id'], r['page'])
    else:
        raise ValueError(ordering)
    return sorted(rows, key=key)


def zero_metrics():
    return {k: 0.0 for k in METRIC_KEYS}, {k: 0 for k in METRIC_KEYS}


def add(sums, counts, metrics):
    for key in METRIC_KEYS:
        if metrics[key] is None:
            continue
        sums[key] += metrics[key]
        counts[key] += 1


def average(sums, counts):
    return {k: (round(sums[k] / counts[k], 4) if counts[k] else None) for k in METRIC_KEYS}


def evaluate(catalog_path):
    con = sqlite3.connect(f'file:{catalog_path}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    per_query, label_artifacts = [], []
    totals = {o: zero_metrics() for o in ORDERS}                      # every query
    subset = {o: zero_metrics() for o in ORDERS}                      # queries with at least one relevant page
    cat_totals = {(n, o): zero_metrics() for n, _ in CATEGORIES for o in ORDERS}
    cat_subset = {(n, o): zero_metrics() for n, _ in CATEGORIES for o in ORDERS}
    verdicts = {'improved': 0, 'unchanged': 0, 'worsened': 0}
    for term in QUERIES:
        category = CAT_OF[term]
        candidates = fetch_candidates(con, term)
        rel_total = corpus_relevant(con, term)
        pattern = None if CJK.search(term) else ASCII_TOKEN(term)
        is_rel = (lambda r: term in r['text']) if pattern is None else (lambda r: bool(pattern.search(r['text'])))
        entry = {'query': term, 'category': category, 'relevant_pages_in_corpus': rel_total,
                 'candidate_pages': len(candidates),
                 'tier_counts': {str(t): sum(1 for r in candidates if r['tier'] == t) for t in (0, 1, 2)},
                 'orders': {}}
        if rel_total == 0:
            label_artifacts.append({'query': term, 'candidate_pages': len(candidates),
                                    'reason': 'no_candidates_in_corpus' if not candidates
                                              else 'term_never_occurs_as_a_standalone_token'})
        firsts = {}
        for order in ORDERS:
            ranked = rank_rows(candidates, order)
            flags = [is_rel(r) for r in ranked]
            ranks = [i for i, flag in enumerate(flags, 1) if flag]
            first = ranks[0] if ranks else None
            metrics = {'first_relevant_rank': first, 'relevant_pages_returned': sum(flags),
                       'noise_only_pages_returned': len(ranked) - sum(flags),
                       'relevant_in_top10': sum(flags[:10]),
                       'noise_only_in_top10': (10 - sum(flags[:10])) if len(ranked) >= 10 else None,
                       'recall_at_10_ceiling': round(min(1.0, 10 / rel_total), 4) if rel_total else None}
            for k in CUTOFFS:
                hit = sum(flags[:k])
                if k in (1, 3, 5, 10):
                    metrics[f'p@{k}'] = round(hit / k, 4)
                metrics[f'r@{k}'] = round(hit / rel_total, 4) if rel_total else None
            metrics['mrr'] = round(1 / first, 4) if first else 0.0
            entry['orders'][order] = metrics
            add(*totals[order], metrics)
            add(*cat_totals[(category, order)], metrics)
            if rel_total:
                add(*subset[order], metrics)
                add(*cat_subset[(category, order)], metrics)
            firsts[order] = first
        old_rank, new_rank = firsts['old'] or NO_RANK, firsts['new'] or NO_RANK
        verdict = 'improved' if new_rank < old_rank else ('worsened' if new_rank > old_rank else 'unchanged')
        verdicts[verdict] += 1
        entry['first_relevant_rank_change'] = {'old': firsts['old'], 'new': firsts['new'], 'verdict': verdict}
        per_query.append(entry)
    corpus = {name: con.execute(sql).fetchone()[0] for name, sql in (
        ('trusted_pages', "SELECT count(*) FROM document_pages p JOIN documents d USING(document_id) WHERE d.trust_status='verified_local'"),
        ('trusted_documents', "SELECT count(*) FROM documents WHERE trust_status='verified_local'"))}
    con.close()
    return {'generated_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'catalogue': catalog_path,
            'label_rule': 'ASCII: case-sensitive standalone token; CJK: case-sensitive occurrence',
            'candidate_rule': 'trusted documents only; FTS MATCH (ASCII terms) OR instr OR LIKE; expand disabled',
            'queries': len(QUERIES), 'corpus': corpus,
            'aggregate_all_queries': {o: average(*totals[o]) for o in ORDERS},
            'aggregate_excluding_label_artifacts': {o: average(*subset[o]) for o in ORDERS},
            'denominator_queries': {o: subset[o][1]['r@10'] for o in ORDERS},
            'by_category_excluding_label_artifacts': {n: {o: average(*cat_subset[(n, o)]) for o in ORDERS}
                                                      for n, _ in CATEGORIES},
            'label_artifacts': label_artifacts,
            'first_relevant_rank': verdicts,
            'per_query': per_query}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--catalog', default=DEFAULT_CATALOG)
    parser.add_argument('--preview', type=int, default=8)
    args = parser.parse_args()
    result = evaluate(args.catalog)
    reports = Path('reports')
    reports.mkdir(exist_ok=True)
    out = reports / f"relevance-eval-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"catalogue={result['catalogue']} queries={result['queries']} corpus={result['corpus']} "
          f"label_artifacts={[a['query'] for a in result['label_artifacts']]}")
    for label, block in (('all queries', 'aggregate_all_queries'),
                         (f"excluding artefacts ({result['denominator_queries']['new']} queries)",
                          'aggregate_excluding_label_artifacts')):
        print(f"  [{label}]")
        for order in ORDERS:
            m = result[block][order]
            print(f"    {order:>3}: P@1={m['p@1']:.3f} P@3={m['p@3']:.3f} P@5={m['p@5']:.3f} P@10={m['p@10']:.3f} "
                  f"MRR={m['mrr']:.3f} R@10={m['r@10']:.3f} R@50={m['r@50']:.3f}")
    for name, _ in CATEGORIES:
        o = result['by_category_excluding_label_artifacts'][name]['old']
        n = result['by_category_excluding_label_artifacts'][name]['new']
        print(f"  {name:<12} P@1 {o['p@1']:.3f}->{n['p@1']:.3f}  MRR {o['mrr']:.3f}->{n['mrr']:.3f}  "
              f"R@10 {o['r@10']:.3f}->{n['r@10']:.3f}")
    print(f"  first-relevant-rank verdicts: {result['first_relevant_rank']}")
    rows = sorted(result['per_query'], key=lambda e: (e['orders']['old']['first_relevant_rank'] or NO_RANK)
                  - (e['orders']['new']['first_relevant_rank'] or NO_RANK), reverse=True)
    print("  largest first-rank gains:")
    for entry in rows[:args.preview]:
        print(f"    {entry['query']:<14} rel={entry['relevant_pages_in_corpus']:<5} cand={entry['candidate_pages']:<5} "
              f"tiers={entry['tier_counts']} first_old={entry['orders']['old']['first_relevant_rank']} "
              f"first_new={entry['orders']['new']['first_relevant_rank']}")
    print(f"  written: {out}")


if __name__ == '__main__':
    main()
