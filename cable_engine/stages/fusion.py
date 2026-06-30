"""cable_engine.stages.fusion — multi-source entity fusion.

When the same cable appears in both a PDF (via OCR, confidence ~0.9)
and a DWG (via vector text, confidence 1.0), the FusionStage takes
the higher-confidence result.

Fusion runs at the document level: if two documents have the same
content_hash (same file, different paths), it's the same entity. If
two documents have different hashes but contain the same cable text,
the FusionStage creates a unified record.

Current behavior (Phase 1):
  - The matches table uses (content_hash, cable) as its primary key,
    so different documents with the same cable produce separate rows.
  - The FusionStage takes the max confidence across sources for each
    (content_hash, cable) pair and updates the match tier.
  - Future Phase 2 will fuse across content_hashes (same cable name
    from different documents → one unified record).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from cable_engine.ir import TextEntity
from cable_engine.pipeline import Context, Stage
from cable_engine.storage import CableStore


class FusionStage(Stage):
    """Cross-source entity fusion: merge PDF + DWG entities for the
    same (content_hash, cable) pair, preferring higher confidence."""

    name = 'fusion'

    def __init__(self, store: CableStore):
        self.store = store

    def run(self, ctx: Context) -> Context:
        if ctx.error_msg is not None or ctx.document is None:
            return ctx

        # For each cable matched in this document, check if there's
        # already a match in the store. If so, upgrade the tier if
        # the new one is higher.
        doc = ctx.document
        for cable, tier in ctx.matches.items():
            # Check if this cable already exists for this doc
            if self.store.has_match(doc.content_hash, cable):
                # Already exists — upgrade if current tier is better
                # Tier order: exact > normalized > confusion > levenshtein
                old_row = self.store._conn.execute(
                    'SELECT tier FROM matches WHERE content_hash = ? AND cable = ?',
                    (doc.content_hash, cable),
                ).fetchone()
                old_tier = old_row['tier'] if old_row else ''
                tier_rank = {'exact': 0, 'normalized': 1, 'confusion': 2, 'levenshtein': 3}
                new_rank = tier_rank.get(tier, 99)
                old_rank = tier_rank.get(old_tier, 99)
                if new_rank < old_rank:
                    self.store.upsert_match(doc.content_hash, cable, tier=tier)
        return ctx


__all__ = ['FusionStage']
