"""cable_engine.classifier.keyword — text/ATTRIB-keyword classifier.

Walks every TextEntity and AttributeEntity in the document, scoring
each BusinessType by the count of matching keywords (normalised by
total text count, with diminishing returns so 1 hit != 100%).

Keyword lists were built from sampling the 527 unclassified DWG files
in the shengli dataset. They are deliberately permissive — false
positives are preferable to false negatives at this stage because
the downstream analyzer will silently produce zero records anyway.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import BaseClassifier, BusinessType, Classification

if TYPE_CHECKING:
    from ..ir import Document


# (business_type, [keyword patterns])
# Matching is substring + case-insensitive for Chinese, case-sensitive
# for ATTRIB tags (which are ASCII).
_KEYWORD_TABLE: dict[BusinessType, list[str]] = {
    BusinessType.CIRCUIT_LOOP: [
        '回路图', '回路',
        '原理图',
        '接线图',
        # ATTRIB tags emitted by DWG standard cable symbol library:
        'WireSerial', 'WireDescription', 'LoopCode', 'WIRENO',
    ],
    BusinessType.TERMINAL_STRIP: [
        '端子排图',
        '端子功能',
        '端子接线',
        # dwgread's EED on LINE entities carries the cable id and an
        # "EXTEND"/"EED" sentinel — but those are entity-level, not
        # text-level; we only score on visible text here.
    ],
    BusinessType.CABLE_SCHEDULE: [
        '电缆清册', '电缆联系', '电缆敷设', '电缆走向',
        '接线表', '接线清单',
        '清册',
    ],
    BusinessType.PROTECTION_DIAGRAM: [
        '保护', '测控', '信号回路',
        '重合闸', '失灵', '差动', '距离', '零序',
        '监控信号',
    ],
    BusinessType.PANEL_LAYOUT: [
        '屏面布置', '屏柜',
        '正面', '背面', '前门', '后门',  # panel face labels
    ],
    BusinessType.PANEL_POSITION: [
        '屏位布置', '屏位图',
        '屏位编号', '屏位表',
    ],
    BusinessType.MONITORING_SYSTEM: [
        '状态监测', '在线监测', '监测系统',
        '通风控制', 'SF6', '密度监测', '密度在线',
        '局放', '温度监测',
    ],
    BusinessType.MANUFACTURER_CATALOG: [
        '厂家图册', '厂家资料', '产品说明书',
        '安装使用说明书', '技术资料', '产品样本',
        '产品目录', '使用手册',
    ],
    BusinessType.UNKNOWN: [
        # Markers of low-value drawings: cover pages, TOCs, design notes.
        '目录', '封面', '卷册说明', '总说明',
        '编制说明', '设计说明', '工艺执行',
        '材料清册', '设备材料',
    ],
}


# ATTRIB tags that strongly imply a specific business type — counted
# with weight 2x since they are structurally meaningful.
_ATTRIB_TAG_HINTS: dict[BusinessType, set[str]] = {
    BusinessType.CIRCUIT_LOOP: {
        'WireSerial', 'WireDescription', 'LoopCode',
        'WIRENO', 'InOut',
    },
    BusinessType.TERMINAL_STRIP: {
        'NO', 'EQUNAME', 'TERMINAL',
    },
}


# "Type-name" keywords that, when present in ANY text, are near-perfect
# indicators of the business type. Weighted 5x so they decisively beat
# ambiguous substrings like "回路" (which also appears in protection
# titles).
_STRONG_MARKERS: dict[BusinessType, list[str]] = {
    BusinessType.CIRCUIT_LOOP: ['回路图', '原理图'],
    BusinessType.TERMINAL_STRIP: ['端子排图', '端子功能图'],
    BusinessType.CABLE_SCHEDULE: ['电缆清册', '电缆联系图'],
    BusinessType.PROTECTION_DIAGRAM: ['保护原理图'],
    BusinessType.PANEL_LAYOUT: ['屏面布置图'],
    BusinessType.PANEL_POSITION: ['屏位布置图', '屏位图'],
    BusinessType.MONITORING_SYSTEM: ['状态监测系统', '通风控制系统'],
    BusinessType.MANUFACTURER_CATALOG: ['厂家图册', '产品说明书', '安装使用说明书'],
    BusinessType.UNKNOWN: ['目录', '卷册说明'],
}


class KeywordClassifier(BaseClassifier):
    name = 'keyword'

    def score(self, doc: 'Document') -> dict[BusinessType, float]:
        # Collect all texts and ATTRIB tags from the document.
        texts: list[tuple[str, str]] = []  # (text, tag_or_empty)
        for e in doc.entities:
            t = getattr(e, 'text', '') or ''
            tag = getattr(e, 'tag', '') or ''
            if t.strip() or tag.strip():
                texts.append((t, tag))

        if not texts:
            return {bt: 0.0 for bt in BusinessType}

        counts: dict[BusinessType, float] = {bt: 0.0 for bt in BusinessType}
        for text, tag in texts:
            text_lower = text.lower()
            # Strong-marker pass (5x weight): exact type-name keywords
            for bt, strong_kws in _STRONG_MARKERS.items():
                for skw in strong_kws:
                    if skw in text:
                        counts[bt] += 5.0
                        break  # one strong hit per type per text
            for bt, kws in _KEYWORD_TABLE.items():
                weight = 1.0
                if tag and tag in _ATTRIB_TAG_HINTS.get(bt, set()):
                    weight = 2.0
                for kw in kws:
                    # For Chinese keywords use substring match (case-insensitive
                    # is a no-op for Chinese).
                    if kw.isascii():
                        if kw.lower() in text_lower:
                            counts[bt] += weight
                            break  # one hit per type per text is enough
                    else:
                        if kw in text:
                            counts[bt] += weight
                            break

        # Normalise: diminishing returns via log1p; cap at 1.0.
        import math
        result: dict[BusinessType, float] = {}
        for bt in BusinessType:
            raw = counts[bt]
            if raw == 0:
                result[bt] = 0.0
                continue
            # log1p gives 0.69 at 1 hit, 1.10 at 2 hits, 1.39 at 3, ...
            # divide by log1p(5) ≈ 1.79 to give 5+ hits ≈ 1.0.
            score = math.log1p(raw) / math.log1p(5)
            result[bt] = min(score, 1.0)
        return result


__all__ = ['KeywordClassifier']
