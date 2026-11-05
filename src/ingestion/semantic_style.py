from __future__ import annotations

from typing import Any

from openpyxl.styles.colors import Color
from openpyxl.styles.fills import GradientFill, PatternFill


SEMANTIC_STYLE_FIELDS = (
    "font",
    "fill",
    "border",
    "alignment",
    "number_format",
    "protection",
    "quotePrefix",
    "pivotButton",
)


def extract_cell_semantic_style(cell) -> dict:
    """Resolve comparable style fields from an openpyxl cell."""
    style_array = cell._style if getattr(cell, "has_style", False) else None
    return {
        "font": _font(getattr(cell, "font", None)),
        "fill": _fill(getattr(cell, "fill", None)),
        "border": _border(getattr(cell, "border", None)),
        "alignment": _alignment(getattr(cell, "alignment", None)),
        "protection": _protection(getattr(cell, "protection", None)),
        "quotePrefix": bool(getattr(style_array, "quotePrefix", False)) if style_array is not None else False,
        "pivotButton": bool(getattr(style_array, "pivotButton", False)) if style_array is not None else False,
    }


def semantic_style_signature(cell: dict | None) -> dict:
    """Comparable style signature from a snapshot cell dict. Does not use style_id."""
    cell = cell or {}
    return {
        "font": cell.get("font"),
        "fill": cell.get("fill"),
        "border": cell.get("border"),
        "alignment": cell.get("alignment"),
        "number_format": cell.get("number_format"),
        "protection": cell.get("protection"),
        "quotePrefix": bool(cell.get("quotePrefix")),
        "pivotButton": bool(cell.get("pivotButton")),
    }


def semantic_styles_equivalent(left: dict | None, right: dict | None) -> bool:
    return semantic_style_signature(left) == semantic_style_signature(right)


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _color(value: Any) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, Color):
        return {"type": "raw", "rgb": _as_str(value), "indexed": None, "theme": None, "tint": 0.0, "auto": None}

    color_type = _as_str(getattr(value, "type", None))
    payload = {
        "type": color_type,
        "rgb": None,
        "indexed": None,
        "theme": None,
        "tint": 0.0,
        "auto": None,
    }
    tint = _as_float(getattr(value, "tint", None))
    payload["tint"] = 0.0 if tint in (None, 0.0) else tint

    if color_type == "rgb":
        rgb = getattr(value, "rgb", None)
        payload["rgb"] = None if rgb is None else str(rgb)
    elif color_type == "theme":
        payload["theme"] = _as_int(getattr(value, "theme", None))
    elif color_type == "indexed":
        payload["indexed"] = _as_int(getattr(value, "indexed", None))
    elif color_type == "auto":
        payload["auto"] = bool(getattr(value, "auto", False))
    else:
        rgb = getattr(value, "rgb", None)
        if rgb is not None and not isinstance(rgb, str):
            try:
                rgb = str(rgb)
            except (TypeError, ValueError):
                rgb = None
        if isinstance(rgb, str) and "Values must be of type" not in rgb:
            payload["rgb"] = rgb
        payload["theme"] = _as_int(getattr(value, "theme", None))
        payload["indexed"] = _as_int(getattr(value, "indexed", None))
        auto = getattr(value, "auto", None)
        payload["auto"] = None if auto is None else bool(auto)
    return payload


def _font(font) -> dict | None:
    if font is None:
        return None
    return {
        "name": _as_str(font.name),
        "sz": _as_float(font.sz),
        "b": bool(font.b),
        "i": bool(font.i),
        "u": _as_str(font.u) if font.u is not None else None,
        "strike": bool(font.strike) if font.strike is not None else False,
        "outline": bool(font.outline) if font.outline is not None else False,
        "shadow": bool(font.shadow) if font.shadow is not None else False,
        "condense": bool(font.condense) if font.condense is not None else False,
        "extend": bool(font.extend) if font.extend is not None else False,
        "charset": _as_int(font.charset),
        "family": _as_float(font.family),
        "scheme": _as_str(font.scheme),
        "vertAlign": _as_str(font.vertAlign),
        "color": _color(font.color),
    }


def _fill(fill) -> dict | None:
    if fill is None:
        return None
    if isinstance(fill, GradientFill):
        return {
            "kind": "gradient",
            "type": _as_str(fill.type),
            "degree": _as_float(fill.degree),
            "left": _as_float(fill.left),
            "right": _as_float(fill.right),
            "top": _as_float(fill.top),
            "bottom": _as_float(fill.bottom),
            "stop": [_color(getattr(stop, "color", None)) for stop in (fill.stop or [])],
        }
    if isinstance(fill, PatternFill) or hasattr(fill, "patternType"):
        return {
            "kind": "pattern",
            "patternType": _as_str(getattr(fill, "patternType", None)),
            "fgColor": _color(getattr(fill, "fgColor", None)),
            "bgColor": _color(getattr(fill, "bgColor", None)),
        }
    return {"kind": "other", "raw": str(fill)}


def _side(side) -> dict | None:
    if side is None:
        return None
    return {
        "style": _as_str(side.style),
        "color": _color(side.color),
    }


def _border(border) -> dict | None:
    if border is None:
        return None
    return {
        "outline": bool(border.outline) if border.outline is not None else True,
        "diagonalUp": bool(border.diagonalUp),
        "diagonalDown": bool(border.diagonalDown),
        "left": _side(border.left),
        "right": _side(border.right),
        "top": _side(border.top),
        "bottom": _side(border.bottom),
        "diagonal": _side(border.diagonal),
    }


def _alignment(alignment) -> dict | None:
    if alignment is None:
        return None
    return {
        "horizontal": _as_str(alignment.horizontal),
        "vertical": _as_str(alignment.vertical),
        "textRotation": _as_int(alignment.textRotation) or 0,
        "wrapText": alignment.wrapText,
        "shrinkToFit": alignment.shrinkToFit,
        "indent": _as_float(alignment.indent) or 0.0,
        "relativeIndent": _as_float(alignment.relativeIndent) or 0.0,
        "justifyLastLine": alignment.justifyLastLine,
        "readingOrder": _as_float(alignment.readingOrder) or 0.0,
    }


def _protection(protection) -> dict | None:
    if protection is None:
        return None
    return {
        "locked": bool(protection.locked) if protection.locked is not None else True,
        "hidden": bool(protection.hidden) if protection.hidden is not None else False,
    }
