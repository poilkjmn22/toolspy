"""cable_match_viewer — web UI to browse a cable_engine cable.db.

Launch with:
    python -m tools cable-match-viewer --db path/to/cable.db [-l 8003]

Or directly:
    python -m tools.cable_match_viewer.server --db path/to/cable.db [-l 8003]

Open the printed URL in a browser; you'll get a 3-pane UI:
  - left   : document list + cable tree
  - middle : document entities / matches / OCR text
  - right  : source document preview via flyfish viewer
"""

from .server import main, main_async, PORT_DEFAULT

__all__ = ['main', 'main_async', 'PORT_DEFAULT']
