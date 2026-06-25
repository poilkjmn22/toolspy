"""cable_match_viewer — web UI to browse cable_match state.json + cache.db.

Launch with:
    python -m tools cable-match-viewer --state .../state.json --cache .../cache.db [-l 8003]

Open the printed URL in a browser; you'll get a 3-pane UI:
  - left  : cable tree (matched first, natural-sorted)
  - middle: PDFs under the selected cable
  - right : PDF.js preview + OCR text + cable-highlighted matches

For details see README in this directory and the docstrings in
viewer.py / server.py.
"""

from .server import main, main_async, PORT_DEFAULT

__all__ = ['main', 'main_async', 'PORT_DEFAULT']