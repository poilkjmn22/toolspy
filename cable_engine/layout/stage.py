"""cable_engine.layout.stage — LayoutStage plugging into the pipeline.

Adds spatial-containment analysis (LayoutTree) to the processing
pipeline. Runs AFTER TopologyStage so classification is known.
Only processes documents classified as panel_layout.

Usage:
    from cable_engine.layout.stage import LayoutStage
    pipeline.stages.append(LayoutStage(store))
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Optional

from ..pipeline import Context, Stage
from ..classifier import BusinessType
from .cabinet import PhysicalCabinet, cabinets_from_tree
from .detector import build_layout_tree

if TYPE_CHECKING:
    from ..storage.sqlite import CableStore


class LayoutStage(Stage):
    """Build a LayoutTree for panel_layout documents and persist it.

    Inputs:  ctx.document (Document IR), ctx.classification
    Outputs: store.panel_layout (if panel_layout)

    Only runs for documents classified as panel_layout (屏面布置图).
    Other document types are skipped entirely.
    """

    name = 'layout_builder'

    def __init__(self, store: Optional['CableStore'] = None) -> None:
        self._store = store

    def run(self, ctx: Context) -> Context:
        # Only process panel_layout documents
        if ctx.classification is None or ctx.classification.primary != BusinessType.PANEL_LAYOUT:
            return ctx

        doc = ctx.document
        if doc is None:
            ctx.error_msg = 'no document to build layout tree from'
            return ctx

        try:
            tree = build_layout_tree(doc)
            ctx.layout_tree = tree

            # Build PhysicalCabinet wrappers for cross-world access
            cabinets = cabinets_from_tree(tree, doc.content_hash)
            ctx.physical_cabinets = cabinets

            if tree.roots and self._store is not None:
                tree_json = json.dumps(_layout_tree_to_dict(tree))
                self._store.upsert_panel_layout(
                    doc.content_hash, tree_json,
                )
        except Exception as exc:
            ctx.error_msg = f'LayoutStage failed: {exc}'

        return ctx


def _layout_tree_to_dict(tree) -> dict:
    return {
        'roots': [_node_to_dict(r) for r in tree.roots],
    }


def _node_to_dict(node) -> dict:
    result = {
        'id': node.id,
        'type': node.node_type.value,
        'name': node.name,
        'bbox': {
            'x': node.bbox.x,
            'y': node.bbox.y,
            'w': node.bbox.w,
            'h': node.bbox.h,
        },
        'children': [_node_to_dict(c) for c in node.children],
        'data': node.data or {},
    }
    if node.group_type is not None:
        result['group_type'] = node.group_type.value
    return result
