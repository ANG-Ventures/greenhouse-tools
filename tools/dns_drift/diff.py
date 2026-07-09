from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class DriftItem:
    klass: str
    name: str
    sub_reason: str | None = None
    detail: dict[str, object] | None = None


SEVERITY = {
    "MISSING": 0,
    "MISPOINTED": 0,
    "ORPHAN": 1,
    "ambiguous": 1,
    "unknown_source": 1,
    "unknown_shape": 1,
}


def _state_by_id(frontdoor_state: Mapping[str, Mapping[str, object]]) -> dict[str, tuple[str, Mapping[str, object]]]:
    by_id: dict[str, tuple[str, Mapping[str, object]]] = {}
    for ip, state in frontdoor_state.items():
        fd_id = str(state.get("id", ""))
        if fd_id:
            by_id[fd_id] = (ip, state)
    return by_id


def _wildcard_matches(wildcard: str, expected_names: Sequence[str]) -> bool:
    if not wildcard.startswith("*."):
        return False
    suffix = wildcard[1:]
    return any(name.endswith(suffix) for name in expected_names)


def _wildcard_matches_name(wildcard: str, name: str) -> bool:
    return wildcard.startswith("*.") and name.endswith(wildcard[1:])


def _live_row_for_name(name: str, live: Mapping[str, tuple[str, bool]]) -> tuple[str, bool] | None:
    exact = live.get(name)
    if exact is not None:
        return exact
    for live_name, row in live.items():
        if _wildcard_matches_name(live_name, name):
            return row
    return None


def reconcile(
    expected: Mapping[str, Sequence[str]],
    live: Mapping[str, tuple[str, bool]],
    frontdoor_state: Mapping[str, Mapping[str, object]],
    *,
    mispoint_confirmed: set[str] | frozenset[str],
) -> list[DriftItem]:
    """Pure DNS drift differ.

    expected is name -> one or more declaring frontdoor IDs. live is name ->
    (answer_ip, enabled). frontdoor_state is answer_ip -> per-frontdoor status.
    """
    items: list[DriftItem] = []
    by_id = _state_by_id(frontdoor_state)

    for name in sorted(expected, key=str.casefold):
        owners = list(expected[name])
        if len(owners) >= 2:
            items.append(DriftItem("ambiguous", name, detail={"frontdoors": owners}))
            continue

        if not owners:
            continue
        owner = owners[0]
        live_row = _live_row_for_name(name, live)
        if live_row is None:
            items.append(DriftItem("MISSING", name, "absent", {"frontdoor": owner}))
            continue
        answer_ip, enabled = live_row
        if not enabled:
            items.append(DriftItem("MISSING", name, "disabled", {"frontdoor": owner, "answer_ip": answer_ip}))
            continue
        owner_ip = by_id.get(owner, (None, {}))[0]
        if owner in mispoint_confirmed and owner_ip is not None and answer_ip != owner_ip:
            items.append(
                DriftItem(
                    "MISPOINTED",
                    name,
                    detail={"frontdoor": owner, "expected_ip": owner_ip, "answer_ip": answer_ip},
                )
            )

    expected_names = set(expected)
    for name in sorted(live, key=str.casefold):
        if name in expected_names:
            continue
        answer_ip, enabled = live[name]
        if not enabled:
            continue
        if name.startswith("*."):
            if not _wildcard_matches(name, tuple(expected_names)):
                items.append(DriftItem("unknown_shape", name, "wildcard_unmatched", {"answer_ip": answer_ip}))
            continue
        state = frontdoor_state.get(answer_ip)
        if state is None:
            items.append(DriftItem("unknown_source", name, "out_of_model", {"answer_ip": answer_ip}))
            continue
        if bool(state.get("deferred")) or not bool(state.get("readable")):
            items.append(
                DriftItem(
                    "unknown_source",
                    name,
                    "deferred_source",
                    {"answer_ip": answer_ip, "frontdoor": state.get("id")},
                )
            )
            continue
        items.append(DriftItem("ORPHAN", name, detail={"answer_ip": answer_ip, "frontdoor": state.get("id")}))

    return sorted(items, key=lambda item: (SEVERITY.get(item.klass, 9), item.klass, item.sub_reason or "", item.name.casefold()))


def exit_code_for(items: Sequence[DriftItem], *, cant_measure: bool = False) -> int:
    if cant_measure:
        return 3
    code = 0
    for item in items:
        if item.klass == "unknown_source" and item.sub_reason == "deferred_source":
            continue
        if item.klass in {"MISSING", "MISPOINTED"}:
            return 2
        if item.klass in {"ORPHAN", "ambiguous", "unknown_shape", "unknown_source"}:
            code = max(code, 1)
    return code
