"""cable_engine.stages.persist - write the run to cable.db + copy PDFs.

The PersistStage is the only thing that talks to the storage layer.
It writes:
  - the document's content_hash + path + source_type (documents table)
  - the OCR entities (entities table)
  - the matches (matches table)
  - scan_state updates (scan_state key/value table)

The CopyStage only runs for PDF. It copies the source PDF into
<output_root>/<cable>/<file> for each matched cable (the original
cable_match.py copied this way to make browsing the result easy).
DWG files are skipped (no need to copy a vector file alongside its
matches; the entities are already in cable.db).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from cable_engine.ir import Document, DocumentType
from cable_engine.pipeline import Context, Stage
from cable_engine.storage import CableStore


class PersistStage(Stage):
    """Write everything to cable.db. Runs for every document type."""

    name = 'persist'

    def __init__(self, store: CableStore, input_root: Path,
                 no_state: bool = False):
        self.store = store
        self.input_root = Path(input_root)
        self.no_state = no_state

    def run(self, ctx: Context) -> Context:
        if ctx.error_msg is not None or ctx.document is None:
            return ctx
        doc = ctx.document
        try:
            rel_path = str(Path(doc.document_path).relative_to(self.input_root))
        except ValueError:
            rel_path = str(doc.document_path)
        # Document row
        self.store.upsert_document(
            content_hash=doc.content_hash,
            pdf_rel_path=rel_path,
            pdf_size=0,
            pdf_mtime=0.0,
            source_type=doc.document_type.value,
        )
        # All entities from the document (text runs, lines, etc.)
        from cable_engine.ir import TextEntity
        for e in doc.entities:
            if isinstance(e, TextEntity):
                self.store.upsert_entity(
                    content_hash=doc.content_hash,
                    page=e.page,
                    source_type=e.source,
                    entity_type='text',
                    text=e.text,
                    confidence=e.confidence,
                    layer=e.layer or '',
                    raw_handle=e.id,
                )
            else:
                # line / polyline / symbol — store minimal fields
                entity_short = (
                    e.__class__.__name__.replace('Entity', '').lower()
                )
                self.store.upsert_entity(
                    content_hash=doc.content_hash,
                    page=e.page,
                    source_type=e.source,
                    entity_type=entity_short,
                    text=None,
                    confidence=e.confidence,
                    layer=e.layer or '',
                    raw_handle=e.id,
                )
        # Matches
        for cable, tier in ctx.matches.items():
            self.store.upsert_match(
                content_hash=doc.content_hash, cable=cable, tier=tier,
            )
        # scan_state: processed list
        if not self.no_state:
            self.store.append_state_list('processed', rel_path)
            # Per-tier counts
            counts = self.store.get_state('match_type_counts', {}) or {}
            for cable, tier in ctx.matches.items():
                counts[tier] = counts.get(tier, 0) + 1
            self.store.set_state('match_type_counts', counts)
        self.store.commit()
        return ctx


class CopyStage(Stage):
    """Copy the source file into <output_root>/<cable>/<file> for each
    matched cable. PDFs only — DWG files don't need a copy because
    their entities are already in cable.db; the original DWG stays
    in place.
    """

    name = 'copy'

    def __init__(self, output_root: Path, input_root: Path):
        self.output_root = Path(output_root)
        self.input_root = Path(input_root)

    def run(self, ctx: Context) -> Context:
        if ctx.error_msg is not None or ctx.document is None:
            return ctx
        if ctx.document.document_type != DocumentType.PDF:
            return ctx  # skip non-PDF
        if not ctx.matches:
            return ctx
        try:
            rel = ctx.document.document_path.relative_to(self.input_root)
        except ValueError:
            rel = Path(ctx.document.document_path.name)
        for cable in ctx.matches:
            dest_dir = self.output_root / cable
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / ctx.document.document_path.name
            n = 1
            while dest.exists():
                dest = dest_dir / f'{ctx.document.document_path.stem}_{n}{ctx.document.document_path.suffix}'
                n += 1
            try:
                shutil.copy2(ctx.document.document_path, dest)
            except OSError:
                pass  # Non-fatal: keep matches in cable.db even if copy fails
        return ctx


__all__ = ['PersistStage', 'CopyStage']
