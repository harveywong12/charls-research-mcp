"""Compact result views with explicit continuation and payload accounting."""
from .query import encoded_size

FIELDS = ('brief', 'standard', 'full')
RETRIEVAL_REVISION = '2026-10-11.research-2'
PAGE_BUDGET = 12000  # JSON envelope bytes, not the SDK's duplicated wire encoding.


def check_fields(fields):
    if fields not in FIELDS:
        raise ValueError('fields must be brief/standard/full')


def finish_page(response, key, requested, offset, total, cursor_at, provenance=None):
    """Trim complete envelopes; cursors advance by actual items, never requested limit.

    Retain at least one item to guarantee progress. A single oversize item is flagged
    rather than silently discarded. Provenance is pruned only for removed items.
    """
    data = response['data']
    data['oversize_item'] = False
    items = data[key]
    removed = False
    def update():
        more = offset + len(items) < total
        response.update(truncated=more, next_cursor=cursor_at(offset + len(items)) if more and items else None)
        data.update(returned_count=len(items), requested_limit=requested, has_more=more,
                    truncation_reason=('byte_budget' if removed else 'limit') if more else None)
        if provenance:
            table, id_key = provenance
            wanted = {item[id_key] for item in items}
            data[table] = {k: v for k, v in data[table].items() if k in wanted}
    update()
    while len(items) > 1 and encoded_size(response) > PAGE_BUDGET:
        items.pop()
        removed = True
        update()
    data['oversize_item'] = encoded_size(response) > PAGE_BUDGET
    return response
