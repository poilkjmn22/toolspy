"""TableRow ↔ DeviceCandidate matching.

Matches a parsed TableRow to a DeviceCandidate by comparing the
``name`` column value with the candidate's name. When matched, the
table's business metadata (model, description) is injected into the
candidate's ``features['table_info']``.
"""

from __future__ import annotations

from ..candidate import DeviceCandidate
from .model import TableArea


def match_to_devices(table: TableArea,
                     candidates: list[DeviceCandidate],
                     ) -> int:
    """Match table rows to device candidates by name column.

    For each data row in *table*, if a candidate's ``name`` matches the
    cell text in the ``name`` column, the row's metadata is stored in
    ``candidate.features['table_info']``.

    Returns the number of matches.
    """
    if table.name_column_index < 0:
        return 0

    match_count = 0
    for row in table.data_rows:
        name_text = row.cell_text(table.name_column_index)
        if not name_text:
            continue

        # Find candidate whose name starts with or equals the table name
        for cand in candidates:
            cand_name = cand.name or ''
            if cand_name == name_text or cand_name.startswith(name_text + ' '):
                info: dict[str, str] = {}
                if table.model_column_index >= 0:
                    info['model'] = row.cell_text(table.model_column_index)
                if table.desc_column_index >= 0:
                    info['description'] = row.cell_text(table.desc_column_index)
                if table.qty_column_index >= 0:
                    info['qty'] = row.cell_text(table.qty_column_index)
                if info:
                    cand.features['table_info'] = info
                    match_count += 1
                break

    return match_count


__all__ = ['match_to_devices']
