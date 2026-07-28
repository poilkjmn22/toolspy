"""ClassificationStage — early classification of Document IR.

Runs before TopologyStage/LayoutStage so every downstream stage
can dispatch based on a known ``ctx.classification``.

Usage::

    pipeline = Pipeline([
        ClassificationStage(),
        TopologyStage(store),
        LayoutStage(store),
    ])
"""

from __future__ import annotations

from typing import Optional

from ..pipeline import Context, Stage
from .composite import CompositeClassifier


class ClassificationStage(Stage):
    """Classify a Document and set ``ctx.classification``.

    Inputs:  ctx.document (Document IR)
    Outputs: ctx.classification (Classification)
             ctx.document_type (str)
    """

    name = 'classifier'

    def __init__(self, classifier: Optional[CompositeClassifier] = None) -> None:
        self._classifier = classifier or CompositeClassifier()

    def run(self, ctx: Context) -> Context:
        if ctx.document is None:
            ctx.error_msg = 'no document to classify'
            return ctx

        classification = self._classifier.classify(ctx.document)
        ctx.document.classification = classification
        ctx.document_type = classification.primary.value
        ctx.classification = classification
        return ctx


__all__ = ['ClassificationStage']
