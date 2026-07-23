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

from typing import TYPE_CHECKING, Optional

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

        # Manufacturer catalog override: if the document contains strong
        # markers (e.g. "厂家图册"), it is a manufacturer catalog regardless
        # of other keyword matches. This bypass prevents multi-type catalogs
        # from being misclassified as circuit_loop / terminal_strip.
        if BusinessType.MANUFACTURER_CATALOG not in composite:
            pass
        elif self._has_manufacturer_marker(doc):
            composite[BusinessType.MANUFACTURER_CATALOG] += 5.0
            composite[BusinessType.CIRCUIT_LOOP] = 0.0
            composite[BusinessType.TERMINAL_STRIP] = 0.0
            composite[BusinessType.PROTECTION_DIAGRAM] = 0.0
            composite[BusinessType.PANEL_POSITION] = 0.0
            # Re-normalise so the boost doesn't overrun
            max_s = max(composite.values()) or 1.0
            normalised = {bt: s / max_s for bt, s in composite.items()}
            ranked = sorted(normalised.items(), key=lambda kv: -kv[1])
            primary_bt, primary_score = ranked[0]
            secondary = [(bt, s) for bt, s in ranked[1:4] if s > 0.05]
            confidence = 1.0
            return Classification(
                primary=primary_bt,
                confidence=round(confidence, 4),
                secondary=secondary,
                signals=signals,
            )

        # If the raw composite sum is too low (every classifier scored
        # < 0.1), the document is essentially empty / unrecognisable —
        # mark it unknown instead of inheriting enum-order tiebreaks.
        total_signal = sum(composite.values())
        if total_signal < 0.10:
            primary_bt = BusinessType.UNKNOWN
            primary_score = 0.0
            confidence = 0.0
            secondary = []

        # Panel position override: if the document has the strong marker
        # "屏位布置图" / "屏位图", PROTECTION_DIAGRAM keyword hits are
        # false positives from cabinet labels. Override the tiebreak.
        if (primary_bt == BusinessType.PROTECTION_DIAGRAM
                and self._has_panel_position_marker(doc)):
            primary_bt = BusinessType.PANEL_POSITION
            primary_score = normalised[primary_bt]
            confidence = min(primary_score * 0.8 + 0.2, 1.0)
            secondary = [(bt, s) for bt, s in ranked if bt != primary_bt][:3]

        return Classification(
            primary=primary_bt,
            confidence=round(confidence, 4),
            secondary=secondary,
            signals=signals,
        )


    @staticmethod
    def _has_manufacturer_marker(doc: 'Document') -> bool:
        """Check if the document text contains manufacturer_catalog strong markers."""
        for e in doc.entities:
            t = getattr(e, 'text', '') or ''
            if '厂家图册' in t or '产品说明书' in t or '安装使用说明书' in t:
                return True
        return False


    @staticmethod
    def _has_panel_position_marker(doc: 'Document') -> bool:
        """Check if the document text contains panel_position strong markers."""
        for e in doc.entities:
            t = getattr(e, 'text', '') or ''
            if '屏位布置图' in t or '屏位图' in t:
                return True
        return False


__all__ = ['CompositeClassifier']