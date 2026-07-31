from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from fu_gm.llm_client import ChatMessage
from fu_gm.llm_utils import extract_json_object
from fu_gm.models import SessionDramaticContract, SessionNPCRole


SESSION_CONTRACT_REACHABILITY_PROMPT = """
你是《最终物语》场次准备的可达性复核器。你不写剧情，不新增世界事实、专有名词、NPC、地点、物品或组织。
你只检查交易型NPC的条件是否真的能由玩家在本场局势中完成，而不是文字看起来完整。

对每名NPC逐项检查：
1. concrete_demand所需的事实、物件、承诺或行动，必须能从输入已有的公开线索、NPC知识、场景入口或现场行动中取得。
2. acceptance_rule必须能被GM客观判断，且至少允许两种合理方法；不得要求唯一台词、唯一技能或玩家知道没有来源的答案。
3. promised_result必须在NPC的authority_scope内，并能在条件满足后立即兑现。
4. public_lead必须是NPC提出条件时能当场告诉玩家的起步方向，明确“去看什么、问谁或做什么”；不能泄露成功答案。
5. fulfillment_routes必须恰好两条，每条都引用输入里已有的线索来源、在场人物、场景入口或现场可执行行动。不能写“自行调查”“想办法”“证明诚意”等空话。
6. 如果原条件不可达，使用现有素材重写demand、acceptance_rule与promised_result；不得靠发明新地点或新证人补洞。

只输出JSON：
{"npcs":[{"name":"原名","reachable":true,"reason":"简短原因","concrete_demand":"...","acceptance_rule":"...","promised_result":"...","public_lead":"...","fulfillment_routes":["路线一","路线二"]}]}
每名输入NPC必须恰好出现一次，name必须逐字保持原名。
""".strip()


class SessionContractReachabilityReviewer:
    """Make every prepared NPC bargain discoverable and executable at the table."""

    def __init__(self, *, client: Any | None, model: str) -> None:
        self.client = client
        self.model = str(model or "").strip()
        self.last_error = ""
        self.last_status = "not_run"
        self.last_call_count = 0

    def review(
        self,
        contract: SessionDramaticContract,
        *,
        world_context: dict[str, object],
    ) -> SessionDramaticContract:
        bargaining = [
            role
            for role in contract.important_npcs
            if all((role.concrete_demand, role.acceptance_rule, role.promised_result))
        ]
        if not bargaining:
            self.last_error = ""
            self.last_status = "not_needed"
            self.last_call_count = 0
            return contract
        if self.client is None or not self.model:
            self.last_error = ""
            self.last_status = "fallback_no_model"
            self.last_call_count = 0
            return self._with_fallback_routes(contract)

        request = {
            "session": {
                "title": contract.title,
                "location": contract.location,
                "dramatic_question": contract.dramatic_question,
                "opening_disruption": contract.opening_disruption,
                "closure_requirement": contract.closure_requirement,
            },
            "npcs": [
                {
                    "name": role.name,
                    "public_role": role.public_role,
                    "goal_now": role.goal_now,
                    "authority_scope": role.authority_scope,
                    "concrete_demand": role.concrete_demand,
                    "acceptance_rule": role.acceptance_rule,
                    "promised_result": role.promised_result,
                    "public_lead": role.public_lead,
                    "fulfillment_routes": list(role.fulfillment_routes),
                }
                for role in bargaining
            ],
            "prepared_clues": [
                {
                    "approach": clue.approach,
                    "source": clue.source,
                    "visible_lead": clue.visible_lead,
                    "success_reveal": clue.success_reveal,
                    "fallback": clue.fallback,
                }
                for clue in contract.clue_routes
            ],
            "prepared_scenes": [
                {
                    "title": scene.title,
                    "location": scene.location,
                    "situation": scene.situation,
                    "entry_points": list(scene.entry_points),
                    "npc_names": list(scene.npc_names),
                }
                for scene in contract.potential_scenes
            ],
            "world_facts": {
                "setting_summary": str(world_context.get("setting_summary") or "")[:1200],
                "active_chapter_package": world_context.get("active_chapter_package") or {},
            },
        }
        try:
            self.last_call_count = 1
            raw = self.client.create_chat_completion(
                model=self.model,
                messages=[
                    ChatMessage(role="system", content=SESSION_CONTRACT_REACHABILITY_PROMPT),
                    ChatMessage(role="user", content=json.dumps(request, ensure_ascii=False)),
                ],
                temperature=0.1,
                response_format={"type": "json_object"},
                operation="session_contract_reachability_review",
            )
            payload = extract_json_object(raw)
            replacements = self._parse(payload, bargaining)
            self.last_error = ""
            self.last_status = "reviewed_by_llm"
            return self._apply(contract, replacements)
        except Exception as exc:
            self.last_error = str(exc)
            self.last_status = "fallback_after_review_failure"
            return self._with_fallback_routes(contract)

    @classmethod
    def _parse(
        cls,
        payload: dict[str, object],
        bargaining: list[SessionNPCRole],
    ) -> dict[str, SessionNPCRole]:
        raw_npcs = payload.get("npcs")
        if not isinstance(raw_npcs, list):
            raise ValueError("reachability review missing npcs array")
        expected = {role.name: role for role in bargaining}
        parsed: dict[str, SessionNPCRole] = {}
        for raw in raw_npcs:
            if not isinstance(raw, dict):
                raise ValueError("reachability npc must be an object")
            name = cls._clean(raw.get("name"), limit=80)
            if name not in expected or name in parsed:
                raise ValueError("reachability review returned an unknown or repeated npc")
            routes = cls._clean_list(raw.get("fulfillment_routes"), limit=2, item_limit=260)
            demand = cls._clean(raw.get("concrete_demand"), limit=260)
            acceptance = cls._clean(raw.get("acceptance_rule"), limit=300)
            promised = cls._clean(raw.get("promised_result"), limit=260)
            lead = cls._clean(raw.get("public_lead"), limit=260)
            if not all((demand, acceptance, promised, lead)) or len(routes) != 2:
                raise ValueError("reachability review returned an incomplete playable contract")
            parsed[name] = replace(
                expected[name],
                concrete_demand=demand,
                acceptance_rule=acceptance,
                promised_result=promised,
                public_lead=lead,
                fulfillment_routes=routes,
            )
        if set(parsed) != set(expected):
            raise ValueError("reachability review did not cover every bargaining npc")
        return parsed

    @staticmethod
    def _apply(
        contract: SessionDramaticContract,
        replacements: dict[str, SessionNPCRole],
    ) -> SessionDramaticContract:
        return replace(
            contract,
            important_npcs=[replacements.get(role.name, role) for role in contract.important_npcs],
        )

    @classmethod
    def _with_fallback_routes(
        cls,
        contract: SessionDramaticContract,
    ) -> SessionDramaticContract:
        candidates: list[str] = []
        for clue in contract.clue_routes:
            source = cls._clean(clue.source, limit=120)
            lead = cls._clean(clue.visible_lead, limit=180)
            if source and lead:
                candidates.append(f"查看{source}呈现的线索：{lead}")
        for scene in contract.potential_scenes:
            for entry in scene.entry_points:
                clean = cls._clean(entry, limit=200)
                if clean:
                    candidates.append(f"在{scene.location or scene.title}尝试：{clean}")
        candidates = list(dict.fromkeys(candidates))
        repaired: list[SessionNPCRole] = []
        for role in contract.important_npcs:
            if not all((role.concrete_demand, role.acceptance_rule, role.promised_result)):
                repaired.append(role)
                continue
            routes = list(dict.fromkeys(cls._clean_list(role.fulfillment_routes, limit=2, item_limit=260)))
            routes.extend(item for item in candidates if item not in routes)
            routes = routes[:2]
            if len(routes) < 2:
                routes.extend(
                    item
                    for item in (
                        f"当面向{role.name}提出一项可验证的现场安排",
                        f"请{role.name}指出其权限内可以当场核对的标准",
                    )
                    if item not in routes
                )
                routes = routes[:2]
            lead = cls._clean(role.public_lead, limit=260) or (
                f"可以先从这里着手：{routes[0]}" if routes else ""
            )
            repaired.append(
                replace(
                    role,
                    public_lead=lead,
                    fulfillment_routes=routes,
                )
            )
        return replace(contract, important_npcs=repaired)

    @staticmethod
    def _clean(value: object, *, limit: int) -> str:
        return " ".join(str(value or "").split()).strip()[:limit]

    @classmethod
    def _clean_list(
        cls,
        value: object,
        *,
        limit: int,
        item_limit: int,
    ) -> list[str]:
        if not isinstance(value, (list, tuple)):
            return []
        return list(
            dict.fromkeys(
                clean
                for item in value
                if (clean := cls._clean(item, limit=item_limit))
            )
        )[:limit]
