"""cable_engine.classifier.composite — weighted ensemble of sub-classifiers.

Combines KeywordClassifier + GeometryClassifier + LayoutClassifier
into a single Classification result.

Default weights were chosen from observed signal quality:
  - KeywordClassifier : 0.55 (most discriminative; we sampled 527 docs)
  - GeometryClassifier: 0.30 (structural)
  - LayoutClassifier  : 0.15 (helps when text is ambiguous)

Each sub-classifier's score is a dict[BusinessType, float]. The
composite score is the weighted sum, then re-normalised to [0, 1].

The primary type is the argmax. The runner-ups become `secondary`
(sorted by score, top 2). Confidence is the margin between primary
and second-best (a heuristic for "how sure are we").
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import BaseClassifier, BusinessType, Classification
from .geometry import GeometryClassifier
from .keyword import KeywordClassifier
from .layout import LayoutClassifier

if TYPE_CHECKING:
    from ..ir import Document


# Tunable weights. Sum doesn't need to equal 1 — the composite
# re-normalises by max(score) anyway.
_WEIGHTS = {
    'keyword': 0.55,
    'geometry': 0.30,
    'layout': 0.15,
}


class CompositeClassifier(BaseClassifier):
    name = 'composite'

    def __init__(self) -> None:
        self._sub = [
            (KeywordClassifier(), _WEIGHTS['keyword']),
            (GeometryClassifier(), _WEIGHTS['geometry']),
            (LayoutClassifier(), _WEIGHTS['layout']),
        ]

    def score(self, doc: 'Document') -> dict[BusinessType, float]:
        composite: dict[BusinessType, float] = {bt: 0.0 for bt in BusinessType}
        for clf, w in self._sub:
            for bt, s in clf.score(doc).items():
                composite[bt] += w * s
        return composite

    def classify(self, doc: 'Document') -> Classification:
        # Collect raw signals (for explainability)
        signals: dict[str, dict[str, float]] = {}
        composite: dict[BusinessType, float] = {bt: 0.0 for bt in BusinessType}
        for clf, w in self._sub:
            sub_scores = clf.score(doc)
            signals[clf.name] = {bt.value: s for bt, s in sub_scores.items()}
            for bt, s in sub_scores.items():
                composite[bt] += w * s

        # Normalise: divide by max score (so primary ≈ 1.0).
        max_s = max(composite.values()) or 1.0
        normalised = {bt: s / max_s for bt, s in composite.items()}

        ranked = sorted(normalised.items(), key=lambda kv: -kv[1])
        primary_bt, primary_score = ranked[0]
        secondary = [(bt, s) for bt, s in ranked[1:4] if s > 0.05]

        # Confidence: weighted margin between top-1 and top-2
        if len(ranked) >= 2 and ranked[0][1] > 0:
            margin = ranked[0][1] - ranked[1][1]
            confidence = min(primary_score * 0.7 + margin * 0.3, 1.0)
        else:
            confidence = primary_score

        # If the raw composite sum is too low (every classifier scored
        # < 0.1), the document is essentially empty / unrecognisable —
        # mark it unknown instead of inheriting enum-order tiebreaks.
        total_signal = sum(composite.values())
        if total_signal < 0.10:
            primary_bt = BusinessType.UNKNOWN
            primary_score = 0.0
            confidence = 0.0
            secondary = []

        return Classification(
            primary=primary_bt,
            confidence=round(confidence, 4),
            secondary=secondary,
            signals=signals,
        )


__all__ = ['CompositeClassifier']