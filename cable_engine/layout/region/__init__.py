"""cable_engine.layout.region — Region layer between CABINET and GROUP.

Regions represent functional areas within a cabinet, detected via:
  - Text labels (仪表区, 端子排区, 设备区, etc.)
  - Group proximity aggregation (adjacent groups → same region)

Integration::

    from cable_engine.layout.region import detect_regions
    regions = detect_regions(cab, doc)
    # → list[LayoutNode] with type=REGION
    # Children are re-parented under the correct region.
"""

from .detector import detect_regions

__all__ = ['detect_regions']
