"""cable_engine — single-binary + single-SQLite cable-match engine.

Stage 1 refactor: extract the cable_match.py monolith into a small
package with clear layers (ir / pipeline / storage). This __init__ is
intentionally empty — the real CLI is `cable_engine/cli.py`.
"""
