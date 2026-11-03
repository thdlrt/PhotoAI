"""Read-only resolution of the active creative choice, shared by export and edits."""
from collections.abc import Mapping
from typing import Any


def selection_for_group(plan: Mapping[str, Any], group_id: str | int) -> Mapping[str, Any]:
    creative = plan.get("creative_style") or {}
    if creative.get("scope") == "global":
        selected = creative.get("global_selection")
    else:
        selected = (creative.get("groups") or {}).get(str(group_id))
    return selected if isinstance(selected, Mapping) else {}


def active_selections(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    creative = plan.get("creative_style") or {}
    if creative.get("scope") == "global":
        return [selection_for_group(plan, "global")]
    # Missing items supports historical callers that supplied only creative
    # state; an explicitly empty item list must not resurrect removed groups.
    group_ids = dict.fromkeys(str(item.get("group_id", -1)) for item in plan.get("items", []))
    if "items" not in plan:
        group_ids = dict.fromkeys((creative.get("groups") or {}).keys())
    return [selection_for_group(plan, group_id) for group_id in group_ids]


def selection_is_confirmed(selection: Mapping[str, Any]) -> bool:
    return selection.get("status") in {"confirmed", "skipped"} and (
        bool(selection.get("manual_override")) or selection.get("recommendation_status") == "complete"
    )


def scope_is_confirmed(plan: Mapping[str, Any]) -> bool:
    selections = active_selections(plan)
    return bool(plan.get("items")) and bool(selections) and all(map(selection_is_confirmed, selections))
