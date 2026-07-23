"""cable_engine.core.asset — Spatial container primitives.

A "spatial container" is anything that occupies physical space in a
drawing: a cabinet, a panel area, a room, or a device bounding box.

SpatialContainer is the shared base type between the two "worlds":

  LogicalCabinet (graph/topology — dashed-rectangle cabinet on 回路图)
  PhysicalCabinet (layout — solid-rectangle cabinet on 屏面布置图)

Both inherit or compose SpatialContainer so that code that only needs
(bbox, name, source) can treat them uniformly without importing from
either sub-package.

Note on "source" values:
  'dwg_dashed_rect'  — CabinetRegionAnalyzer (回路图 dashed boundary)
  'panel_layout'     — LayoutStage (屏面布置图 solid boundary)
  Future: 'bim_import', 'manual_definition', etc.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..ir.entities import BBox


@dataclass(frozen=True)
class SpatialContainer:
    """Base spatial container — anything with a position and name.

    This is intentionally minimal. Sub-packages add their own fields
    (e.g. CabinetRecord adds text_label, location, etc.) and expose
    a ``container`` property for cross-world interoperability.
    """
    id: str
    document_hash: str
    bbox: BBox
    name: str = ''
    source: str = ''
    layer: str = ''


__all__ = ['SpatialContainer']
