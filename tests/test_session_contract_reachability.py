from __future__ import annotations

import json

from fu_gm.components.session_contract_reachability import (
    SessionContractReachabilityReviewer,
)
from fu_gm.models import (
    SessionClueRoute,
    SessionDramaticContract,
    SessionNPCRole,
    SessionSceneOpportunity,
)


class ReviewClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls = 0
        self.call_kwargs: list[dict[str, object]] = []

    def create_chat_completion(self, **kwargs) -> str:
        self.calls += 1
        self.call_kwargs.append(dict(kwargs))
        return json.dumps(self.payload, ensure_ascii=False)


def _unreachable_contract() -> SessionDramaticContract:
    return SessionDramaticContract(
        title="迟响的白花铃",
        location="白花碑驿站",
        dramatic_question="守望会是否开放旧路？",
        important_npcs=[
            SessionNPCRole(
                name="白花守望会会长",
                public_role="旧路守门人",
                goal_now="避免追兵进入旧路",
                authority_scope="可以开放旧路并安排巡守带路",
                concrete_demand="说出第一处安全落脚点，并保证巡逻队无法跟入",
                acceptance_rule="落脚点名称正确且巡逻队不会跟入",
                promised_result="立即开放旧路并安排巡守带路",
            )
        ],
        clue_routes=[
            SessionClueRoute(
                approach="查阅记录",
                source="旧路登记簿",
                visible_lead="最近一页有一道被刮去的双线印记",
                success_reveal="双线印记对应闸门内侧的临时避风龛",
                fallback="值守望可以带英雄到闸门现场核对印记",
            )
        ],
        potential_scenes=[
            SessionSceneOpportunity(
                title="闸门核对",
                location="旧路闸门",
                entry_points=["对照登记簿与闸门印记", "请值守望现场作证"],
                npc_names=["白花守望会会长", "值守望"],
            )
        ],
    )


def test_semantic_reviewer_repairs_an_unreachable_gatekeeper_condition() -> None:
    client = ReviewClient(
        {
            "npcs": [
                {
                    "name": "白花守望会会长",
                    "reachable": True,
                    "reason": "登记簿和闸门现场都能提供可核对证据",
                    "concrete_demand": "证明登记簿上的双线印记与闸门内侧同一标志对应，并提出一项阻断追踪的现场安排",
                    "acceptance_rule": "英雄通过对照登记簿或请值守望作证确认印记，并完成封门、抹除标记或同等有效的一项阻断安排",
                    "promised_result": "立即开放旧路并安排巡守带队通过第一处界标",
                    "public_lead": "先去看旧路登记簿末页的双线印记；值守望也能带你们到闸门核对",
                    "fulfillment_routes": [
                        "查阅旧路登记簿并到闸门对照双线印记",
                        "请值守望现场作证，再处理追踪标记",
                    ],
                }
            ]
        }
    )
    reviewer = SessionContractReachabilityReviewer(client=client, model="fake")

    deadline = 123456.0
    reviewed = reviewer.review(
        _unreachable_contract(),
        world_context={},
        deadline=deadline,
    )

    role = reviewed.important_npcs[0]
    assert client.calls == 1
    assert reviewer.last_status == "reviewed_by_llm"
    assert "双线印记" in role.concrete_demand
    assert "登记簿" in role.public_lead
    assert len(role.fulfillment_routes) == 2
    assert all("安全落脚点" not in route for route in role.fulfillment_routes)
    request = client.call_kwargs[0]
    assert request["thinking_enabled"] is False
    assert request["max_tokens"] == 2400
    assert request["deadline"] == deadline
    assert request["operation"] == "session_contract_reachability_review"
    assert request["max_recovery_retries"] == 1
    assert request["retry_without_response_format_on_empty"] is True


def test_no_model_fallback_still_gives_players_two_prepared_starting_routes() -> None:
    reviewer = SessionContractReachabilityReviewer(client=None, model="")

    reviewed = reviewer.review(_unreachable_contract(), world_context={})

    role = reviewed.important_npcs[0]
    assert reviewer.last_status == "fallback_no_model"
    assert role.public_lead
    assert len(role.fulfillment_routes) == 2
    assert any("登记簿" in route for route in role.fulfillment_routes)
