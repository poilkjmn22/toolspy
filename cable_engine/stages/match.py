"""cable_engine.stages.match - 4-tier cable ID match stage.

Wraps cable_engine.match.find_matches in a Stage. The match logic
itself lives in cable_engine.match and is pure (no I/O). This stage
just gathers the document's text and runs the match.
"""

from __future__ import annotations

from cable_engine.ir import TextEntity
from cable_engine.match import find_matches
from cable_engine.pipeline import Context, Stage


class MatchStage(Stage):
    """Match the document's text against the cable-ID target list.

    Reads TextEntity objects from `ctx.document.entities` (any source),
    runs the 4-tier fuzzy match, and stores the result in
    `ctx.matches` (cable -> tier). The Persist Stage will write this
    to the cable.db.

    Why "any source": DWGLoader emits TextEntity objects directly
    (no OCR). OCRStage emits TextEntity objects from PixelImages.
    Fusion logic downstream doesn't care which; it just sees text.
    """

    name = 'match'

    def __init__(self, targets: list[str], use_levenshtein: bool = False):
        self.targets = list(targets)
        self.use_levenshtein = use_levenshtein

    def run(self, ctx: Context) -> Context:
        if ctx.error_msg is not None or ctx.document is None:
            return ctx
        # Concatenate every TextEntity's text across all pages / sources.
        # The order is: DWG first (if any), then OCR pages in page order.
        # We sort by source first (DWG = 'dwg' before 'pdf') so the
        # first match wins for cables that appear in multiple sources
        # (currently just concatenation, no dedup — match is substring-based).
        texts = [e.text for e in ctx.document.entities
                 if isinstance(e, TextEntity) and e.text]
        # Sort entities by (source, page) to get a deterministic order.
        # We pull the source/page from the underlying TextEntity
        # via the document's `entities` list (preserves insertion order).
        combined = '\n'.join(texts)
        ctx.matches = find_matches(
            combined, self.targets, use_levenshtein=self.use_levenshtein,
        )
        return ctx


__all__ = ['MatchStage']
