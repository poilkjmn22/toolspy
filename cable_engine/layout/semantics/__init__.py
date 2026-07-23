"""cable_engine.layout.semantics — Weak-semantic annotation layer.

This package enriches LayoutNodes with *interpreted* (not extracted)
information: device type classification, text association, etc.

The semantics layer is explicitly "weak" — it produces confidence-rated
hints (DeviceAttributes) rather than hard type assignments. Downstream
consumers choose whether to trust them.
"""
