"""Shared tabular serialization v2 for MiniLM and TABERTA.

Wide header sets use column windows; individual headers exceeding the dual
budget use ordered fragments. Both encoders receive the same UTF-8 strings.
Tokenizers count tokens without changing the serialized content.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence

from openpyxl.utils import get_column_letter, range_boundaries

from .tabular_base import (
    LINE_SEPARATOR,
    TOKEN_BUDGET,
    StructuralBudgetError,
    _body_segments,
    _DualBudgetAssembler,
    _headers_for_block,
    _join_fields,
    _mapped_block_type,
    _sorted_cells,
    escape_field,
    nfc,
)

SERIALIZATION_VERSION = "tabular_serialization_v2"
TEXT_VERSION_TAG = "v2"
BOUNDARY_LOOKBACK = 64
KIND_COLUMN_WINDOW = "column_window"
KIND_HEADER_FRAGMENT = "header_fragment"

TokenCounter = Callable[[str], int]


def serialize_block(
    block: dict,
    *,
    count_minilm: TokenCounter,
    count_taberta: TokenCounter,
    token_budget: int = TOKEN_BUDGET,
) -> list[dict]:
    """Serialize one semantic block into one or more shared dual-budget documents."""
    try:
        single = _emit_document(
            block,
            headers=_canonical_headers(block),
            columns=_columns_for_block(block),
            range_value=str(block.get("range") or ""),
            extra_lines=[],
            include_body=True,
            kind=KIND_COLUMN_WINDOW,
            count_minilm=count_minilm,
            count_taberta=count_taberta,
            token_budget=token_budget,
        )
        return [_finalize_document(block, [single], 0, window_ordinal=1, fragment_meta=None)]
    except StructuralBudgetError:
        documents = _serialize_hierarchical(
            block,
            count_minilm=count_minilm,
            count_taberta=count_taberta,
            token_budget=token_budget,
        )
        if not documents:
            raise
        return documents


def reconstruct_canonical_headers(documents: Sequence[dict]) -> list[tuple[int, str]]:
    """Rebuild (column, canonical header) from v2 documents of one source block."""
    by_column: dict[int, list[dict]] = defaultdict(list)
    for document in documents:
        for unit in document.get("header_units") or []:
            by_column[int(unit["column"])].append(unit)
    reconstructed: list[tuple[int, str]] = []
    for column in sorted(by_column):
        units = sorted(by_column[column], key=lambda item: int(item["fragment_ordinal"]))
        ordinals = [int(item["fragment_ordinal"]) for item in units]
        if ordinals != list(range(1, len(units) + 1)):
            raise StructuralBudgetError(f"header fragments for column {column} are not contiguous")
        counts = {int(item["fragment_count"]) for item in units}
        if len(counts) != 1 or next(iter(counts)) != len(units):
            raise StructuralBudgetError(f"header fragment_count mismatch for column {column}")
        pieces = [str(item["fragment_text"]) for item in units]
        starts = [int(item["char_start"]) for item in units]
        ends = [int(item["char_end"]) for item in units]
        if starts[0] != 0:
            raise StructuralBudgetError(f"header reconstruction for column {column} does not start at 0")
        for index in range(len(units) - 1):
            if ends[index] != starts[index + 1]:
                raise StructuralBudgetError(f"header reconstruction gap or overlap in column {column}")
        text = "".join(pieces)
        if ends[-1] != len(text):
            raise StructuralBudgetError(f"header reconstruction length mismatch for column {column}")
        for unit, piece in zip(units, pieces):
            if text[int(unit["char_start"]) : int(unit["char_end"])] != piece:
                raise StructuralBudgetError("fragment_text does not match char span")
        reconstructed.append((column, text))
    return reconstructed


def reconstruct_header_values(documents: Sequence[dict]) -> list[str]:
    return [text for _column, text in reconstruct_canonical_headers(documents)]


def _serialize_hierarchical(
    block: dict,
    *,
    count_minilm: TokenCounter,
    count_taberta: TokenCounter,
    token_budget: int,
) -> list[dict]:
    units = _column_units(
        block,
        count_minilm=count_minilm,
        count_taberta=count_taberta,
        token_budget=token_budget,
    )
    if not units:
        raise StructuralBudgetError(
            "mandatory structural fields exceed the dual token budget and no headers "
            "are available to window or fragment"
        )
    raw: list[dict] = []
    atomic_buffer: list[tuple[int, str]] = []
    mapped_type = _mapped_block_type(block.get("block_type"))
    first_document = True

    def include_body_for(kind: str, fragment_ordinal: int | None) -> bool:
        nonlocal first_document
        attach = False
        if mapped_type == "header_context":
            attach = False
        elif mapped_type == "sheet_overview":
            attach = first_document
        elif kind == KIND_HEADER_FRAGMENT:
            attach = fragment_ordinal == 1
        else:
            attach = True
        first_document = False
        return attach

    def flush_atomic() -> None:
        if not atomic_buffer:
            return
        windows = _greedy_column_windows(
            block,
            list(atomic_buffer),
            count_minilm=count_minilm,
            count_taberta=count_taberta,
            token_budget=token_budget,
        )
        for window in windows:
            columns = [item[0] for item in window]
            headers = [item[1] for item in window]
            raw.append(
                _emit_document(
                    block,
                    headers=headers,
                    columns=columns,
                    range_value=_range_for_columns(block, columns),
                    extra_lines=[],
                    include_body=include_body_for(KIND_COLUMN_WINDOW, None),
                    kind=KIND_COLUMN_WINDOW,
                    count_minilm=count_minilm,
                    count_taberta=count_taberta,
                    token_budget=token_budget,
                )
            )
        atomic_buffer.clear()

    for unit in units:
        if unit["kind"] == KIND_COLUMN_WINDOW:
            atomic_buffer.append((int(unit["column"]), str(unit["text"])))
            continue
        flush_atomic()
        pieces = unit["pieces"]
        column = int(unit["column"])
        letter = str(unit["column_letter"])
        fragment_count = len(pieces)
        for ordinal, piece in enumerate(pieces, start=1):
            extra = [f"hcol={letter}", f"frag={ordinal:04d}"]
            raw.append(
                _emit_document(
                    block,
                    headers=[piece["fragment_text"]],
                    columns=[column],
                    range_value=_range_for_columns(block, [column]),
                    extra_lines=extra,
                    include_body=include_body_for(KIND_HEADER_FRAGMENT, ordinal),
                    kind=KIND_HEADER_FRAGMENT,
                    count_minilm=count_minilm,
                    count_taberta=count_taberta,
                    token_budget=token_budget,
                    header_units=[
                        {
                            "column": column,
                            "column_letter": letter,
                            "fragment_ordinal": ordinal,
                            "fragment_count": fragment_count,
                            "char_start": piece["char_start"],
                            "char_end": piece["char_end"],
                            "fragment_text": piece["fragment_text"],
                        }
                    ],
                )
            )
    flush_atomic()

    window_ordinal = 0
    finalized: list[dict] = []
    for index, payload in enumerate(raw):
        fragment_meta = None
        current_window = None
        if payload["kind"] == KIND_COLUMN_WINDOW:
            window_ordinal += 1
            current_window = window_ordinal
        else:
            fragment_meta = payload["header_units"][0]
        finalized.append(
            _finalize_document(
                block,
                raw,
                index,
                window_ordinal=current_window,
                fragment_meta=fragment_meta,
            )
        )
    return finalized


def _column_units(
    block: dict,
    *,
    count_minilm: TokenCounter,
    count_taberta: TokenCounter,
    token_budget: int,
) -> list[dict]:
    pairs = _columns_and_headers(block)
    units: list[dict] = []
    for column, header in pairs:
        letter = get_column_letter(column)
        range_value = _range_for_columns(block, [column])
        atomic_text = _mandatory_text(
            block,
            range_value=range_value,
            headers=[header],
            extra_lines=[],
        )
        if _fits(atomic_text, count_minilm, count_taberta, token_budget):
            units.append(
                {
                    "kind": KIND_COLUMN_WINDOW,
                    "column": column,
                    "column_letter": letter,
                    "text": header,
                }
            )
            continue
        pieces = _fragment_header(
            block,
            column=column,
            column_letter=letter,
            header=header,
            range_value=range_value,
            count_minilm=count_minilm,
            count_taberta=count_taberta,
            token_budget=token_budget,
        )
        units.append(
            {
                "kind": KIND_HEADER_FRAGMENT,
                "column": column,
                "column_letter": letter,
                "text": header,
                "pieces": pieces,
            }
        )
    return units


def _fragment_header(
    block: dict,
    *,
    column: int,
    column_letter: str,
    header: str,
    range_value: str,
    count_minilm: TokenCounter,
    count_taberta: TokenCounter,
    token_budget: int,
) -> list[dict]:
    if not header:
        raise StructuralBudgetError("cannot fragment an empty header")
    pieces: list[dict] = []
    pos = 0
    ordinal = 1
    while pos < len(header):

        def make_text(piece: str, current_ordinal: int = ordinal) -> str:
            return _mandatory_text(
                block,
                range_value=range_value,
                headers=[piece],
                extra_lines=[f"hcol={column_letter}", f"frag={current_ordinal:04d}"],
            )

        end = _longest_fitting_end(
            header,
            start=pos,
            make_text=make_text,
            count_minilm=count_minilm,
            count_taberta=count_taberta,
            token_budget=token_budget,
        )
        piece_text = header[pos:end]
        if not piece_text:
            raise StructuralBudgetError(
                "header fragment would be empty; single code point exceeds dual token budget"
            )
        pieces.append(
            {
                "column": column,
                "char_start": pos,
                "char_end": end,
                "fragment_text": piece_text,
            }
        )
        pos = end
        ordinal += 1
    reconstructed = "".join(item["fragment_text"] for item in pieces)
    if reconstructed != header:
        raise StructuralBudgetError("header fragmentation is not lossless")
    return pieces


def _longest_fitting_end(
    text: str,
    *,
    start: int,
    make_text: Callable[[str], str],
    count_minilm: TokenCounter,
    count_taberta: TokenCounter,
    token_budget: int,
) -> int:
    remaining = len(text) - start
    lo = 1
    hi = remaining
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = text[start : start + mid]
        if _fits(make_text(candidate), count_minilm, count_taberta, token_budget):
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best == 0:
        minilm = count_minilm(make_text(text[start : start + 1] if remaining else ""))
        taberta = count_taberta(make_text(text[start : start + 1] if remaining else ""))
        raise StructuralBudgetError(
            "mandatory structural fields exceed the dual token budget of "
            f"{token_budget} while fragmenting a header "
            f"(MiniLM={minilm}, TABERTA={taberta})"
        )
    end = start + best
    snapped = _snap_textual_boundary(text, start, end)
    if start < snapped < end and _fits(
        make_text(text[start:snapped]), count_minilm, count_taberta, token_budget
    ):
        return snapped
    return end


def _snap_textual_boundary(text: str, start: int, end: int) -> int:
    if end >= len(text):
        return end
    lookback_from = max(start + 1, end - BOUNDARY_LOOKBACK)
    for index in range(end - 1, lookback_from - 1, -1):
        if text[index].isspace():
            return index + 1
    return end


def _greedy_column_windows(
    block: dict,
    pairs: list[tuple[int, str]],
    *,
    count_minilm: TokenCounter,
    count_taberta: TokenCounter,
    token_budget: int,
) -> list[list[tuple[int, str]]]:
    windows: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []

    def fits(candidate: list[tuple[int, str]]) -> bool:
        columns = [item[0] for item in candidate]
        headers = [item[1] for item in candidate]
        text = _mandatory_text(
            block,
            range_value=_range_for_columns(block, columns),
            headers=headers,
            extra_lines=[],
        )
        return _fits(text, count_minilm, count_taberta, token_budget)

    for pair in pairs:
        candidate = [*current, pair]
        if fits(candidate):
            current = candidate
            continue
        if current:
            windows.append(current)
        current = [pair]
        if not fits(current):
            raise StructuralBudgetError(
                "atomic header no longer fits after range adjustment"
            )
    if current:
        windows.append(current)
    return windows


def _emit_document(
    block: dict,
    *,
    headers: list[str],
    columns: list[int],
    range_value: str,
    extra_lines: list[str],
    include_body: bool,
    kind: str,
    count_minilm: TokenCounter,
    count_taberta: TokenCounter,
    token_budget: int,
    header_units: list[dict] | None = None,
) -> dict:
    mapped_type = _mapped_block_type(block.get("block_type"))
    assembler = _DualBudgetAssembler(
        count_minilm=count_minilm,
        count_taberta=count_taberta,
        token_budget=token_budget,
    )
    structural_lines = [
        f"ver={TEXT_VERSION_TAG}",
        f"type={mapped_type}",
        f"sheet={escape_field(nfc(str(block.get('sheet_name') or '')))}",
        f"range={escape_field(nfc(str(range_value or '')))}",
        *extra_lines,
        f"headers={_join_fields(headers)}",
    ]
    for line in structural_lines:
        if not assembler.try_add(line):
            raise StructuralBudgetError(
                "mandatory structural fields exceed the dual token budget of "
                f"{token_budget} (MiniLM={count_minilm(assembler.preview(line))}, "
                f"TABERTA={count_taberta(assembler.preview(line))})"
            )

    truncated = False
    if include_body:
        sliced = _slice_block(block, columns, range_value)
        for line, replace_prefix in _body_segments(mapped_type, sliced):
            if assembler.try_add(line, replace_prefix=replace_prefix):
                continue
            truncated = True
            break

    text = assembler.text()
    minilm_tokens = count_minilm(text)
    taberta_tokens = count_taberta(text)
    if minilm_tokens > token_budget or taberta_tokens > token_budget:
        raise StructuralBudgetError("serialized text exceeds dual token budget after assembly")

    if header_units is None:
        header_units = [
            {
                "column": column,
                "column_letter": get_column_letter(column),
                "fragment_ordinal": 1,
                "fragment_count": 1,
                "char_start": 0,
                "char_end": len(header),
                "fragment_text": header,
            }
            for column, header in zip(columns, headers)
        ]

    return {
        "serialization_text": text,
        "serialization_version": SERIALIZATION_VERSION,
        "source_block_id": str(block.get("block_id") or ""),
        "truncated": truncated,
        "taberta_token_count": taberta_tokens,
        "minilm_token_count": minilm_tokens,
        "kind": kind,
        "range": range_value,
        "column_start": min(columns) if columns else None,
        "column_end": max(columns) if columns else None,
        "header_units": header_units,
        "includes_body": include_body and not _body_omitted(text, mapped_type),
        "mapped_type": mapped_type,
    }


def _finalize_document(
    block: dict,
    raw: list[dict],
    index: int,
    *,
    window_ordinal: int | None,
    fragment_meta: dict | None,
) -> dict:
    payload = dict(raw[index] if raw else {})
    source_id = str(block.get("block_id") or payload.get("source_block_id") or "")
    if payload.get("kind") == KIND_HEADER_FRAGMENT and fragment_meta is not None:
        letter = str(fragment_meta["column_letter"])
        ordinal = int(fragment_meta["fragment_ordinal"])
        document_id = f"{source_id}__hf_{letter}_{ordinal:04d}"
        payload["source_column"] = int(fragment_meta["column"])
        payload["header_fragment_ordinal"] = ordinal
        payload["header_fragment_count"] = int(fragment_meta["fragment_count"])
        payload["char_start"] = int(fragment_meta["char_start"])
        payload["char_end"] = int(fragment_meta["char_end"])
        payload["fragment_text"] = str(fragment_meta["fragment_text"])
    else:
        document_id = f"{source_id}__cw_{int(window_ordinal or 1):04d}"
        payload["source_column"] = None
        payload["header_fragment_ordinal"] = None
        payload["header_fragment_count"] = None
        payload["char_start"] = None
        payload["char_end"] = None
        payload["fragment_text"] = None
    payload["document_id"] = document_id
    payload["segment_ordinal"] = index + 1
    return payload


def _body_omitted(text: str, mapped_type: str) -> bool:
    if mapped_type == "row_block":
        return "\nrow=" not in f"\n{text}"
    if mapped_type == "column_block":
        return not any(line.startswith("col=") for line in text.split("\n"))
    if mapped_type == "used_range":
        return not any(line.startswith("r") and "=" in line for line in text.split("\n")[5:])
    if mapped_type == "cell_reference_block":
        return "\ncoord=" not in f"\n{text}"
    if mapped_type == "sheet_overview":
        return "\nstats=" not in f"\n{text}"
    return True


def _mandatory_text(
    block: dict,
    *,
    range_value: str,
    headers: list[str],
    extra_lines: list[str],
) -> str:
    mapped = _mapped_block_type(block.get("block_type"))
    lines = [
        f"ver={TEXT_VERSION_TAG}",
        f"type={mapped}",
        f"sheet={escape_field(nfc(str(block.get('sheet_name') or '')))}",
        f"range={escape_field(nfc(str(range_value or '')))}",
        *extra_lines,
        f"headers={_join_fields(headers)}",
    ]
    return LINE_SEPARATOR.join(lines)


def _fits(
    text: str,
    count_minilm: TokenCounter,
    count_taberta: TokenCounter,
    token_budget: int,
) -> bool:
    return count_minilm(text) <= token_budget and count_taberta(text) <= token_budget


def _canonical_headers(block: dict) -> list[str]:
    return [nfc(value) for value in _headers_for_block(block)]


def _columns_for_block(block: dict) -> list[int]:
    header_context = block.get("header_context") or {}
    by_column = header_context.get("headers_by_column")
    if not by_column and "associated_headers" in header_context:
        by_column = header_context.get("associated_headers")
    if not by_column:
        column_header = header_context.get("column_header")
        if column_header:
            by_column = {str(column_header.get("column") or 0): column_header}
    cells = _sorted_cells(block.get("cells") or [])
    if cells:
        return sorted({int(cell["column"]) for cell in cells})
    if by_column:
        return sorted(int(key) for key in by_column)
    return []


def _columns_and_headers(block: dict) -> list[tuple[int, str]]:
    headers = _canonical_headers(block)
    columns = _columns_for_block(block)
    if not headers:
        return []
    if len(columns) != len(headers):
        raise StructuralBudgetError(
            f"column/header length mismatch: {len(columns)} vs {len(headers)}"
        )
    return list(zip(columns, headers))


def _range_for_columns(block: dict, columns: list[int]) -> str:
    if not columns:
        return str(block.get("range") or "")
    column_set = set(columns)
    cells = [
        cell
        for cell in _sorted_cells(block.get("cells") or [])
        if int(cell.get("column") or 0) in column_set
    ]
    min_col = min(columns)
    max_col = max(columns)
    if cells:
        min_row = min(int(cell["row"]) for cell in cells)
        max_row = max(int(cell["row"]) for cell in cells)
    else:
        original = str(block.get("range") or "")
        if original:
            try:
                _c1, min_row, _c2, max_row = range_boundaries(original)
            except ValueError:
                min_row, max_row = 1, 1
        else:
            min_row, max_row = 1, 1
    start = f"{get_column_letter(min_col)}{min_row}"
    end = f"{get_column_letter(max_col)}{max_row}"
    return start if start == end else f"{start}:{end}"


def _slice_block(block: dict, columns: list[int], range_value: str) -> dict:
    column_set = set(columns)
    cells = [
        cell
        for cell in (block.get("cells") or [])
        if int(cell.get("column") or 0) in column_set
    ]
    return {
        **block,
        "cells": cells,
        "range": range_value,
        "header_context": _slice_header_context(block.get("header_context") or {}, column_set),
    }


def _slice_header_context(header_context: dict, columns: set[int]) -> dict:
    sliced = dict(header_context)
    for key in ("headers_by_column", "associated_headers"):
        mapping = header_context.get(key)
        if isinstance(mapping, dict):
            sliced[key] = {
                key_name: value
                for key_name, value in mapping.items()
                if int(key_name) in columns
            }
    column_header = header_context.get("column_header")
    if column_header and int(column_header.get("column") or 0) not in columns:
        sliced["column_header"] = None
    return sliced
