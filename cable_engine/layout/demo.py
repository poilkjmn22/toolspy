"""Usage: python -m cable_engine.layout.demo <dwg_file>"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from cable_engine.loaders import get_loader_for
from cable_engine.layout import build_layout_tree


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    path = Path(sys.argv[1])
    loader = get_loader_for(path)
    if loader is None:
        print(f'No loader for {path.suffix}')
        sys.exit(1)

    print(f'Loading {path}...', flush=True)
    doc = loader.load(path)
    if doc is None:
        print('Failed to load')
        sys.exit(1)

    print(f'  entities: {len(doc.entities)}')
    print(f'  pages: {len(doc.pages)}')
    print(f'  document_type: {doc.document_type}')

    # Count entity types
    from cable_engine.ir import (
        ArcGeometry, BlockRef, CircleGeometry, GeometryEntity,
        LineEntity, LineGeometry, TextEntity,
    )
    counts = {}
    for e in doc.entities:
        cls_name = type(e).__name__
        counts[cls_name] = counts.get(cls_name, 0) + 1
    print(f'  entity types: {json.dumps(counts, indent=2)}')

    # Check polyline point counts
    poly_counts = {}
    for e in doc.entities:
        if isinstance(e, LineGeometry):
            n = len(list(e.points or []))
            poly_counts[n] = poly_counts.get(n, 0) + 1
    print(f'  polyline point counts: {json.dumps(poly_counts, indent=2)}')

    print()
    print('Building LayoutTree...', flush=True)
    tree = build_layout_tree(doc)
    print(tree.dump())


if __name__ == '__main__':
    main()
