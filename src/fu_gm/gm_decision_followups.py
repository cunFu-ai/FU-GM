from __future__ import annotations

from copy import deepcopy


def add_gm_opportunity_followups(
    *,
    pending_decisions: list[dict[str, object]],
    required_tools: list[str],
    required_calls: list[dict[str, object]],
) -> bool:
    """要求本事务内处理所有由GM操控的阻塞机会。"""

    existing_window_ids = {
        str(dict(call.get("arguments") or {}).get("window_id") or "").strip()
        for call in required_calls
        if str(call.get("tool_name") or "").strip() == "resolve_gm_opportunity"
    }
    added = False
    for pending in pending_decisions:
        if (
            str(pending.get("kind") or "").strip()
            not in {"critical_opportunity", "fumble_opportunity"}
            or str(pending.get("owner") or "").strip() != "__gm__"
            or not bool(pending.get("blocking"))
        ):
            continue
        window_id = str(pending.get("window_id") or "").strip()
        if not window_id or window_id in existing_window_ids:
            continue
        required_calls.append(
            {
                "tool_name": "resolve_gm_opportunity",
                "arguments": {"window_id": window_id},
                "authority_reason": (
                    "本次检定产生了由GM操控的机会；"
                    "必须从窗口合法选项中决定效果或立即放弃并提交，不能遗留到下一条消息。"
                ),
                "window": deepcopy(pending),
            }
        )
        existing_window_ids.add(window_id)
        added = True
    if added and "resolve_gm_opportunity" not in required_tools:
        required_tools.append("resolve_gm_opportunity")
    return added


# 保留旧名称，供第三方集成在迁移期间继续导入。
add_gm_fumble_followups = add_gm_opportunity_followups


def required_followup_mode(
    required_calls: list[dict[str, object]],
    *,
    independent_obligation_added: bool = False,
) -> str:
    """Return all-mode when separate obligations cannot substitute for each other."""

    return (
        "all"
        if independent_obligation_added or len(required_calls) > 1
        else "any"
    )
