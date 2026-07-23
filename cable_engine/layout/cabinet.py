"""cable_engine.layout.cabinet — PhysicalCabinet for panel-layout drawings.

PhysicalCabinet wraps a CABINET LayoutNode with a SpatialContainer
interface, mirroring how LogicalCabinet (CabinetRecord) exposes its
``container`` property in the graph/cabinet module.

This lets cross-world code (e.g. the viewer, the asset-model builder)
iterate over cabinets from both 回路图 and 屏面布置图 via a single
``SpatialContainer`` reference — without importing from either sub-package.

Usage::

    from cable_engine.layout.cabinet import PhysicalCabinet

    for root in layout_tree.roots:
        if root.node_type == LayoutNodeType.CABINET:
            pc = PhysicalCabinet.from_node(root, doc_hash)
            print(pc.container.name)
            for dev in pc.devices:
                print(f"  {dev.name}")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..core.asset import SpatialContainer
from .types import LayoutNode, LayoutNodeType, LayoutTree


@dataclass
class PhysicalCabinet:
    """A cabinet on a 屏面布置图 (solid outer boundary).

    Unlike the dashed-rectangle ``CabinetRecord`` from graph.cabinet
    (which represents a logical cabinet boundary on a 回路图), this is
    a **physical** cabinet face with devices mounted on it.
    """
    container: SpatialContainer
    node: LayoutNode
    device_nodes: list[LayoutNode] = field(default_factory=list)

    @classmethod
    def from_node(
        cls,
        node: LayoutNode,
        document_hash: str,
    ) -> Optional[PhysicalCabinet]:
        if node.node_type != LayoutNodeType.CABINET:
            return None
        devices: list[LayoutNode] = []
        for child in node.children or []:
            if child.node_type == LayoutNodeType.DEVICE:
                devices.append(child)
            elif child.node_type == LayoutNodeType.PANEL_AREA:
                _collect_devices(child, devices)
        container = SpatialContainer(
            id=node.id,
            document_hash=document_hash,
            bbox=node.bbox,
            name=node.name,
            source='panel_layout',
            layer=node.data.get('layer', ''),
        )
        return cls(container=container, node=node, device_nodes=devices)

    @property
    def id(self) -> str:
        return self.container.id

    @property
    def name(self) -> str:
        return self.container.name

    @property
    def bbox(self):
        return self.container.bbox


def _collect_devices(node: LayoutNode, out: list[LayoutNode]) -> None:
    """Recursively collect DEVICE nodes from a subtree."""
    for child in node.children or []:
        if child.node_type == LayoutNodeType.DEVICE:
            out.append(child)
        else:
            _collect_devices(child, out)


def cabinets_from_tree(
    tree: LayoutTree,
    document_hash: str,
) -> list[PhysicalCabinet]:
    """Extract all PhysicalCabinet instances from a LayoutTree."""
    out: list[PhysicalCabinet] = []
    for root in tree.roots:
        pc = PhysicalCabinet.from_node(root, document_hash)
        if pc is not None:
            out.append(pc)
    return out


__all__ = [
    'PhysicalCabinet',
    'cabinets_from_tree',
]
