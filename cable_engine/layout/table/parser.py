"""Legacy shim: table parser for PANEL_LAYOUT equipment tables.

Delegates to ``table/parsers/equipment.py``.
"""

from __future__ import annotations

from typing import Optional

from ...ir import Document
from ...ir.entities import BBox
from ..table.model import TableArea
from ..table.parsers.equipment import parse_table_at


__all__ = ['parse_table_at']
