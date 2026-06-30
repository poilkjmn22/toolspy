"""cable_engine.stages.graph — extract Node/Edge graph from entities.

Phase 1 of the GraphStage: parse text entities for:
  1. Cable IDs (e.g. 3B-507, ZL-338ZF) — match patterns
  2. Terminal/Device IDs (e.g. X2:1, J701, GD:20) — match terminal patterns
  3. Build Node/Edge pairs and store them in cable.db's
     graph_nodes and graph_edges tables.

The terminal extraction uses heuristics:
  - Patterns like X2:1, J701, GD:20, ZL-338ZF, etc.
  - Anything that appears on the same text line next to a cable ID

Future phases will:
  - Use geometry proximity (DWG coordinates) to find which cable
    goes where
  - Use DWG line endpoints to trace connections
"""

from __future__ import annotations

import re
from typing import Optional

from cable_engine.ir import TextEntity
from cable_engine.pipeline import Context, Stage
from cable_engine.storage import CableStore


# Common terminal/device ID patterns in power-drawing diagrams
_TERMINAL_PATTERNS = [
    # Explicit terminal markers: X followed by digits, optionally colon-digit
    re.compile(r'(?<![A-Z])X\d+[:\-]?\d*', re.IGNORECASE),      # X2:1, X3, X2-34
    # Device/terminal numbers like J701, J943A
    re.compile(r'[JFG][A-Z]?\d{3,}[A-Z]?', re.IGNORECASE),       # J701, J943A, GD:20
    # GD, GD:20 style
    re.compile(r'GD[:\-]?\d+', re.IGNORECASE),                    # GD:20, GD-5
    # T, T2-135 style (T followed by number)
    re.compile(r'(?<![A-Z])T\d{1,3}[:\-]?\w*', re.IGNORECASE),   # T2-135, T011-144a
    # DK, GY, JL, PJ style
    re.compile(r'(?:DK|GY|JL|PJ)[:\-]?\d+[A-Z]?', re.IGNORECASE),  # DK1-130, GY1-145
]

# Cable ID pattern (matches the 4-tier match logic)
_CABLE_PATTERN = re.compile(r'\b[A-Z0-9]+[-\s]?[0-9A-Z]+\b')


class GraphStage(Stage):
    """Extract Node/Edge pairs from document entities and persist to
    cable.db. Identifies:
      - Cables (via the existing match logic)
      - Terminal numbers (via regex patterns)
      - Device IDs (via regex patterns)

    Edges: (cable) --CONNECTS--> (terminal) if they appear on the same
    text line or in close proximity (same page, same layer).
    """

    name = 'graph'

    def __init__(self, store: CableStore):
        self.store = store

    def run(self, ctx: Context) -> Context:
        if ctx.error_msg is not None or ctx.document is None:
            return ctx

        doc = ctx.document
        doc_hash = doc.content_hash

        # Collect all text entities and their content
        all_texts = [e.text for e in doc.entities
                    if isinstance(e, TextEntity) and e.text]

        # Build nodes: cable IDs + terminal IDs
        cables_found: set[str] = set(ctx.matches.keys())
        terminals_found: set[str] = set()

        # Scan all text for terminal patterns
        for text in all_texts:
            for pat in _TERMINAL_PATTERNS:
                for m in pat.finditer(text):
                    terminals_found.add(m.group())

        # Persist nodes
        for cable in cables_found:
            self.store.upsert_match(doc_hash, cable, tier=ctx.matches.get(cable, 'exact'))

        for terminal in terminals_found:
            node_id = f'TERM-{terminal}'
            # Insert into graph_nodes (simple UPSERT via INSERT OR IGNORE)
            self.store._conn.execute(
                'INSERT OR IGNORE INTO graph_nodes (id, page, node_type) VALUES (?, ?, ?)',
                (node_id, 1, 'terminal'),
            )

        # Build edges: if a cable and terminal appear in the same text
        # line, they're connected.
        for text in all_texts:
            line_cables = [c for c in cables_found if c in text]
            line_terminals = [t for t in terminals_found if t in text]
            for c in line_cables:
                for t in line_terminals:
                    src_id = c
                    dst_id = f'TERM-{t}'
                    self.store._conn.execute(
                        """INSERT OR IGNORE INTO relations
                           (src_entity_id, dst_entity_id, relation_type, confidence)
                           VALUES (?, ?, ?, ?)""",
                        (src_id, dst_id, 'CONNECTS', 0.9),
                    )

        self.store.commit()
        return ctx


__all__ = ['GraphStage']
