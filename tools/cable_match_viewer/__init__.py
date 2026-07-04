"""cable_viewer — V5 minimal viewer for cable.db.

Two modules:
  server.py    -- aiohttp server (entry point)
  store.py     -- thin read-only wrapper around CableStore with
                  on-demand graph traversal

API:
  GET /                              -- main page (vanilla HTML)
  GET /api/cables                    -- all cables
  GET /api/cable/{id}                -- cable detail (terminals + loops +
                                         source documents)
  GET /api/document/{hash}           -- document metadata
  GET /api/document/{hash}/file      -- raw file (PDF or DWG, inline)
  GET /api/document/{hash}/entities  -- all graph nodes for the document
                                       (used for the on-canvas preview)
"""

from .server import main

__all__ = ['main']