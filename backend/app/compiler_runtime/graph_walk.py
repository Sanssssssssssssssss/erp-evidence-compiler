from __future__ import annotations

from collections.abc import Callable, Iterable


def reachable_ids(
    root_id: str,
    children_for: Callable[[str], Iterable[str]],
) -> set[str]:
    """Return node ids reachable from one root using caller-owned edge semantics."""

    reachable: set[str] = set()
    pending = [root_id]
    while pending:
        node_id = pending.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        pending.extend(children_for(node_id))
    return reachable


__all__ = ["reachable_ids"]
