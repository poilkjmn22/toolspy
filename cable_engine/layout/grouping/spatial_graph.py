"""DeviceSpatialGraph — lightweight spatial index for DEVICE nodes."""

from __future__ import annotations

from typing import Optional

from ...ir.entities import BBox
from ..model import LayoutNode, LayoutNodeType


class DeviceSpatialGraph:
    """Lightweight spatial index for device centroids.

    Builds a grid-based index grouping devices into cells of *cell_size*.
    Acceptable for <5000 devices; no external dependency.

    Usage::

        graph = DeviceSpatialGraph(devices)
        for row in graph.grid_rows():
            ...
    """

    def __init__(
        self,
        devices: list[LayoutNode],
        cell_size: float = 20.0,
    ) -> None:
        self._devices = list(devices)
        self._cell_size = cell_size
        self._grid: dict[tuple[int, int], list[LayoutNode]] = {}
        self._device_map: dict[str, LayoutNode] = {}

        for d in devices:
            cx = int(d.bbox.x + d.bbox.w / 2)
            cy = int(d.bbox.y + d.bbox.h / 2)
            gc = (cx // int(cell_size), cy // int(cell_size))
            self._grid.setdefault(gc, []).append(d)
            self._device_map[d.id] = d

    @property
    def devices(self) -> list[LayoutNode]:
        return self._devices

    def get(self, device_id: str) -> Optional[LayoutNode]:
        return self._device_map.get(device_id)

    def neighbors_in_cell(self, device: LayoutNode) -> list[LayoutNode]:
        """Return all devices in the same grid cell."""
        cx = int(device.bbox.x + device.bbox.w / 2)
        cy = int(device.bbox.y + device.bbox.h / 2)
        gc = (cx // int(self._cell_size), cy // int(self._cell_size))
        return self._grid.get(gc, [])

    def center(self, device: LayoutNode) -> tuple[float, float]:
        return (device.bbox.x + device.bbox.w / 2,
                device.bbox.y + device.bbox.h / 2)

    def grid_rows(self) -> list[list[LayoutNode]]:
        """Group devices by y-band (horizontal stripe)."""
        rows: dict[int, list[LayoutNode]] = {}
        for d in self._devices:
            band = int((d.bbox.y + d.bbox.h / 2) // self._cell_size)
            rows.setdefault(band, []).append(d)
        return list(rows.values())

    def grid_columns(self) -> list[list[LayoutNode]]:
        """Group devices by x-band (vertical stripe)."""
        cols: dict[int, list[LayoutNode]] = {}
        for d in self._devices:
            band = int((d.bbox.x + d.bbox.w / 2) // self._cell_size)
            cols.setdefault(band, []).append(d)
        return list(cols.values())

    def bbox(self) -> BBox:
        if not self._devices:
            return BBox(0, 0, 0, 0)
        xs = [d.bbox.x for d in self._devices]
        ys = [d.bbox.y for d in self._devices]
        xe = [d.bbox.x + d.bbox.w for d in self._devices]
        ye = [d.bbox.y + d.bbox.h for d in self._devices]
        return BBox(min(xs), min(ys), max(xe) - min(xs), max(ye) - min(ys))
