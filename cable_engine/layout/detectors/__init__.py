"""cable_engine.layout.detectors — Spatial detector modules.

Each module detects one spatial element family:

  cabinet.py — detect_cabinets, _merge_cabinet_candidates, _find_cabinet_name_at
  device.py  — detect_devices, open-rect + BlockRef + merge
  area.py    — panel area detection, cabinet interior analysis
"""
