def _block_id(result: dict) -> str:
    value = result.get("metadata", {}).get("block_id")
    if not isinstance(value, str) or not value:
        value = result.get("candidate_id")
    if not isinstance(value, str) or not value:
        raise ValueError("accepted search results require a stable block_id or candidate_id")
    return value


def _selection_key(query_index: int, result: dict) -> tuple:
    rank = result.get("rank")
    if isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0:
        raise ValueError("accepted search results require a positive integer rank")
    return (-result["score"], query_index, rank, _block_id(result))


def _aggregate(accepted: list[tuple[int, dict]]) -> list[dict]:
    best = {}
    for query_index, result in accepted:
        block_id = _block_id(result)
        key = _selection_key(query_index, result)
        if block_id not in best or key < best[block_id][0]:
            best[block_id] = (key, result)
    return [entry[1] for entry in sorted(best.values(), key=lambda entry: entry[0])]
