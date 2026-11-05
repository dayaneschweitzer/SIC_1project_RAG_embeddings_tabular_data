"""Deterministic instruction patterns for the shared modification parser.

Patterns match natural-language templates. They must not read benchmark family
IDs, oracle/golden fields, or expected cells.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from .parsed_intent import (
    COPY_SHEET,
    INSERT_COLUMN,
    INSERT_ROW,
    PARSE_PARSED,
    REPLACE_VALUES,
    SORT_RANGE,
    UNMERGE_RANGE,
    WRITE_FORMULA,
    WRITE_VALUE,
    ParsedModificationIntent,
)

CELL = r"\$?[A-Za-z]{1,3}\$?[1-9]\d{0,6}"
RANGE = rf"{CELL}:{CELL}"
COLUMN = r"[A-Za-z]{1,3}"
SHEET_ON = r'On sheet "(?P<sheet>.+?)",\s+'


def _upper_ref(value: str) -> str:
    return str(value).strip().upper()


def _intent(**kwargs: Any) -> ParsedModificationIntent:
    destination = kwargs.get("destination")
    if destination and not kwargs.get("target_coordinate"):
        kwargs["target_coordinate"] = destination
    literal = kwargs.get("literal_value")
    if literal is not None and kwargs.get("new_value") is None:
        kwargs["new_value"] = literal
    kwargs.setdefault("parse_status", PARSE_PARSED)
    kwargs.setdefault("confidence", "high")
    kwargs.setdefault("warnings", [])
    return ParsedModificationIntent(**kwargs)


def _t1(match: re.Match[str], request: str, normalized: str) -> ParsedModificationIntent:
    return _intent(
        request=request,
        normalized_request=normalized,
        operation=REPLACE_VALUES,
        target_sheet=match.group("sheet"),
        source_range=_upper_ref(match.group("range")),
        replacement_old=match.group("old"),
        replacement_new=match.group("new"),
        target_value=match.group("old"),
        new_value=match.group("new"),
    )


def _t2(match: re.Match[str], request: str, normalized: str) -> ParsedModificationIntent:
    src1 = _upper_ref(match.group("src1"))
    src2 = _upper_ref(match.group("src2"))
    dest = _upper_ref(match.group("dest"))
    return _intent(
        request=request,
        normalized_request=normalized,
        operation=WRITE_VALUE,
        expression="concatenate",
        separator=" ",
        target_sheet=match.group("sheet"),
        source_cells=[src1, src2],
        destination=dest,
    )


def _t3_token(match: re.Match[str], request: str, normalized: str) -> ParsedModificationIntent:
    return _intent(
        request=request,
        normalized_request=normalized,
        operation=WRITE_VALUE,
        expression="extract_token",
        target_sheet=match.group("sheet"),
        source_cells=[_upper_ref(match.group("src"))],
        destination=_upper_ref(match.group("dest")),
    )


def _t3_prefix(match: re.Match[str], request: str, normalized: str) -> ParsedModificationIntent:
    return _intent(
        request=request,
        normalized_request=normalized,
        operation=WRITE_VALUE,
        expression="extract_prefix",
        extract_length=int(match.group("n")),
        target_sheet=match.group("sheet"),
        source_cells=[_upper_ref(match.group("src"))],
        destination=_upper_ref(match.group("dest")),
    )


def _t4(match: re.Match[str], request: str, normalized: str) -> ParsedModificationIntent:
    return _intent(
        request=request,
        normalized_request=normalized,
        operation=WRITE_VALUE,
        expression="lookup",
        target_sheet=match.group("sheet"),
        source_cells=[_upper_ref(match.group("src"))],
        destination=_upper_ref(match.group("dest")),
    )


def _n1(match: re.Match[str], request: str, normalized: str) -> ParsedModificationIntent:
    src1 = _upper_ref(match.group("src1"))
    src2 = _upper_ref(match.group("src2"))
    dest = _upper_ref(match.group("dest"))
    return _intent(
        request=request,
        normalized_request=normalized,
        operation=WRITE_VALUE,
        expression="add",
        target_sheet=match.group("sheet"),
        source_cells=[src1, src2],
        destination=dest,
    )


def _n2(match: re.Match[str], request: str, normalized: str) -> ParsedModificationIntent:
    source_range = _upper_ref(match.group("range"))
    dest = _upper_ref(match.group("dest"))
    return _intent(
        request=request,
        normalized_request=normalized,
        operation=WRITE_FORMULA,
        expression="sum",
        target_sheet=match.group("sheet"),
        source_range=source_range,
        destination=dest,
        formula=f"=SUM({source_range})",
    )


def _n3(match: re.Match[str], request: str, normalized: str) -> ParsedModificationIntent:
    source_range = _upper_ref(match.group("range"))
    dest = _upper_ref(match.group("dest"))
    return _intent(
        request=request,
        normalized_request=normalized,
        operation=WRITE_FORMULA,
        expression="average",
        target_sheet=match.group("sheet"),
        source_range=source_range,
        destination=dest,
        formula=f"=AVERAGE({source_range})",
    )


def _n4(match: re.Match[str], request: str, normalized: str) -> ParsedModificationIntent:
    src1 = _upper_ref(match.group("src1"))
    src2 = _upper_ref(match.group("src2"))
    dest = _upper_ref(match.group("dest"))
    return _intent(
        request=request,
        normalized_request=normalized,
        operation=WRITE_FORMULA,
        expression="divide",
        target_sheet=match.group("sheet"),
        source_cells=[src1, src2],
        destination=dest,
        formula=f"={src1}/{src2}",
    )


def _n5(match: re.Match[str], request: str, normalized: str) -> ParsedModificationIntent:
    src1 = _upper_ref(match.group("src1"))
    src2 = _upper_ref(match.group("src2"))
    dest = _upper_ref(match.group("dest"))
    return _intent(
        request=request,
        normalized_request=normalized,
        operation=WRITE_FORMULA,
        expression="subtract",
        target_sheet=match.group("sheet"),
        source_cells=[src1, src2],
        destination=dest,
        formula=f"={src1}-{src2}",
    )


def _n6(match: re.Match[str], request: str, normalized: str) -> ParsedModificationIntent:
    src1 = _upper_ref(match.group("src1"))
    src2 = _upper_ref(match.group("src2"))
    dest = _upper_ref(match.group("dest"))
    return _intent(
        request=request,
        normalized_request=normalized,
        operation=WRITE_FORMULA,
        expression="add",
        target_sheet=match.group("sheet"),
        source_cells=[src1, src2],
        destination=dest,
        formula=f"={src1}+{src2}",
    )


def _s1(match: re.Match[str], request: str, normalized: str) -> ParsedModificationIntent:
    return _intent(
        request=request,
        normalized_request=normalized,
        operation=INSERT_ROW,
        target_sheet=match.group("sheet"),
        insertion_axis="row",
        insertion_position=int(match.group("row")),
    )


def _s2(match: re.Match[str], request: str, normalized: str) -> ParsedModificationIntent:
    return _intent(
        request=request,
        normalized_request=normalized,
        operation=INSERT_COLUMN,
        target_sheet=match.group("sheet"),
        insertion_axis="column",
        insertion_position=_upper_ref(match.group("col")),
    )


def _s3(match: re.Match[str], request: str, normalized: str) -> ParsedModificationIntent:
    sort_range = _upper_ref(match.group("range"))
    return _intent(
        request=request,
        normalized_request=normalized,
        operation=SORT_RANGE,
        target_sheet=match.group("sheet"),
        sort_range=sort_range,
        source_range=sort_range,
        sort_key="first_column",
        sort_order="ascending",
    )


def _s4(match: re.Match[str], request: str, normalized: str) -> ParsedModificationIntent:
    merged = _upper_ref(match.group("range"))
    return _intent(
        request=request,
        normalized_request=normalized,
        operation=UNMERGE_RANGE,
        target_sheet=match.group("sheet"),
        source_range=merged,
        target_coordinate=None,
        destination=None,
    )


def _s5(match: re.Match[str], request: str, normalized: str) -> ParsedModificationIntent:
    caption = match.group("caption").strip().strip("'\"")
    dest = _upper_ref(match.group("dest"))
    return _intent(
        request=request,
        normalized_request=normalized,
        operation=WRITE_VALUE,
        expression="layout_caption",
        target_sheet=match.group("sheet"),
        destination=dest,
        literal_value=caption,
        new_value=caption,
    )


def _s6(match: re.Match[str], request: str, normalized: str) -> ParsedModificationIntent:
    return _intent(
        request=request,
        normalized_request=normalized,
        operation=COPY_SHEET,
        target_sheet=match.group("sheet"),
        new_sheet_name=match.group("new_sheet"),
    )


Builder = Callable[[re.Match[str], str, str], ParsedModificationIntent]

CATALOG: list[tuple[re.Pattern[str], Builder]] = [
    (
        re.compile(
            rf'^{SHEET_ON}replace every occurrence of "(?P<old>.*?)" in (?P<range>{RANGE}) with "(?P<new>.*?)"\.$',
            re.IGNORECASE,
        ),
        _t1,
    ),
    (
        re.compile(
            rf"^{SHEET_ON}concatenate (?P<src1>{CELL}) and (?P<src2>{CELL}) with a space "
            rf"and write the result in (?P<dest>{CELL})\.$",
            re.IGNORECASE,
        ),
        _t2,
    ),
    (
        re.compile(
            rf"^{SHEET_ON}extract the first (?P<n>\d+) characters from (?P<src>{CELL}) "
            rf"and write them in (?P<dest>{CELL})\.$",
            re.IGNORECASE,
        ),
        _t3_prefix,
    ),
    (
        re.compile(
            rf"^{SHEET_ON}extract the first token from (?P<src>{CELL}) "
            rf"and write it in (?P<dest>{CELL})\.$",
            re.IGNORECASE,
        ),
        _t3_token,
    ),
    (
        re.compile(
            rf"^{SHEET_ON}look up the key in (?P<src>{CELL}) and write the matching "
            rf"text value into (?P<dest>{CELL})\.$",
            re.IGNORECASE,
        ),
        _t4,
    ),
    (
        re.compile(
            rf"^{SHEET_ON}insert a formula that adds (?P<src1>{CELL}) and (?P<src2>{CELL}) "
            rf"in (?P<dest>{CELL})\.$",
            re.IGNORECASE,
        ),
        _n6,
    ),
    (
        re.compile(
            rf"^{SHEET_ON}add (?P<src1>{CELL}) and (?P<src2>{CELL}) and store the numeric "
            rf"result in (?P<dest>{CELL})\.$",
            re.IGNORECASE,
        ),
        _n1,
    ),
    (
        re.compile(
            rf"^{SHEET_ON}write a SUM formula for (?P<range>{RANGE}) in (?P<dest>{CELL})\.$",
            re.IGNORECASE,
        ),
        _n2,
    ),
    (
        re.compile(
            rf"^{SHEET_ON}write an AVERAGE formula for (?P<range>{RANGE}) in (?P<dest>{CELL})\.$",
            re.IGNORECASE,
        ),
        _n3,
    ),
    (
        re.compile(
            rf"^{SHEET_ON}write a formula for (?P<src1>{CELL})/(?P<src2>{CELL}) in (?P<dest>{CELL})\.$",
            re.IGNORECASE,
        ),
        _n4,
    ),
    (
        re.compile(
            rf"^{SHEET_ON}write a formula for (?P<src1>{CELL})-(?P<src2>{CELL}) in (?P<dest>{CELL})\.$",
            re.IGNORECASE,
        ),
        _n5,
    ),
    (
        re.compile(
            rf"^{SHEET_ON}insert one blank row at row (?P<row>\d+)\.$",
            re.IGNORECASE,
        ),
        _s1,
    ),
    (
        re.compile(
            rf"^{SHEET_ON}insert one blank column at column (?P<col>{COLUMN})\.$",
            re.IGNORECASE,
        ),
        _s2,
    ),
    (
        re.compile(
            rf"^{SHEET_ON}sort range (?P<range>{RANGE}) by the first column ascending\.$",
            re.IGNORECASE,
        ),
        _s3,
    ),
    (
        re.compile(
            rf"^{SHEET_ON}unmerge the range (?P<range>{RANGE})\.$",
            re.IGNORECASE,
        ),
        _s4,
    ),
    (
        re.compile(
            rf"^{SHEET_ON}write the layout caption (?P<caption>\"[^\"]+\"|\S+) in (?P<dest>{CELL})\.$",
            re.IGNORECASE,
        ),
        _s5,
    ),
    (
        re.compile(
            r'^Create a copy of sheet "(?P<sheet>.+?)" named "(?P<new_sheet>.+?)"\.$',
            re.IGNORECASE,
        ),
        _s6,
    ),
]


def match_catalog(request: str, normalized: str) -> list[ParsedModificationIntent]:
    matches: list[ParsedModificationIntent] = []
    for pattern, builder in CATALOG:
        found = pattern.fullmatch(normalized)
        if found is None:
            continue
        matches.append(builder(found, request, normalized))
    return matches
