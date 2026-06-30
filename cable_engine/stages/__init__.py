"""cable_engine.stages — concrete pipeline stages.

Each stage is one step in the multi-source document pipeline.
Stages are pure transforms: they mutate `ctx` and return it. The
Context (defined in cable_engine.pipeline.stage) carries the
Document, the matches, the error state, etc.

The stages are organized by responsibility:
  - ocr.py:    RasterizeStage (add gauss_otsu variants), OCRStage
  - match.py:  MatchStage (4-tier fuzzy cable ID search)
  - persist.py: PersistStage (write to cable.db), CopyStage (copy PDFs)
  - fusion.py: FusionStage (cross-source entity merging)
  - graph.py:  GraphStage (terminal/device extraction + Node/Edge)
"""

from .fusion import FusionStage
from .graph import GraphStage
from .match import MatchStage
from .ocr import OCRStage, RasterizeStage
from .persist import CopyStage, PersistStage


__all__ = [
    'FusionStage', 'GraphStage',
    'OCRStage', 'RasterizeStage',
    'MatchStage',
    'PersistStage', 'CopyStage',
]
