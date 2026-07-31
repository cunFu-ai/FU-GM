from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from fu_gm.llm_client import ChatMessage
from fu_gm.llm_utils import extract_json_object
from fu_gm.npc_identity import stable_npc_identity_label
from fu_gm.components.session_identity_guard import (
    SessionIdentityAssessment,
    SessionIdentityGuard,
)
from fu_gm.components.session_contract_reachability import (
    SessionContractReachabilityReviewer,
)
from fu_gm.components.npc_role_profiles import (
    DEFAULT_AUTHORITY_SCOPE,
    enrich_role_record,
    local_role_profile,
)
from fu_gm.models import (
    SessionClueRoute,
    SessionDramaticContract,
    SessionNPCRole,
    SessionSceneOpportunity,
)


class SessionPrepConcretizer:
    """Turn a generic session brief into concrete, reusable situation prep.

    The planner still owns pacing and continuity.  This component only gives
    its movable pieces names, physical form and decision rules once per table
    session.  It never chooses what the heroes do or fixes a scene order.
    """

    _GENERIC_MARKERS = (
        "选定一件",
        "首次出镜",
        "固定其感官",
        "标志画面",
        "可被触碰或改变",
        "本次必须更换物件",
        "某个npc",
        "某个 NPC",
        "现场关键人物",
        "现场阻力",
        "合适对象",
        "具体事物",
        "当前局面",
        "可附着",
        "根据玩家",
    )
    _GENERIC_NPC_NAMES = {
        "",
        "现场人物",
        "现场关键人物",
        "关键人物",
        "守门人",
        "对立方",
        "世界威胁",
        "世界奥秘",
        "现场阻力",
        "未知敌人",
        "财团代理人",
        "某人",
        "NPC",
    }
    _FACTION_SUFFIXES = (
        "财团",
        "司教团",
        "教团",
        "守望会",
        "行会",
        "王室",
        "帝国",
        "王国",
        "公国",
        "联盟",
        "军团",
        "教会",
        "协会",
        "公司",
    )
    _NPC_ROLE_TERMS = (
        "旅人",
        "会长",
        "监察官",
        "守门人",
        "巡守长",
        "钟匠",
        "掌柜",
        "祭司",
        "书记官",
        "队长",
        "领主",
        "商人",
        "学者",
        "士兵",
        "守卫",
        "村长",
        "司教",
        "船长",
        "向导",
        "证人",
        "俘虏",
        "伤员",
        "使者",
        "女王",
        "公主",
        "王子",
        "骑士",
        "神官",
        "工匠",
    )
    _DEFAULT_AUTHORITY_SCOPE = DEFAULT_AUTHORITY_SCOPE

    def __init__(self, *, client: Any | None, model: str) -> None:
        self.client = client
        self.model = str(model or "").strip()
        self.last_error = ""
        self.last_gatekeeper_repair_error = ""
        self.last_gatekeeper_repair_status = "not_run"
        self.last_gatekeeper_repair_attempts = 0
        self.identity_guard = SessionIdentityGuard()
        self.reachability_reviewer = SessionContractReachabilityReviewer(
            client=client,
            model=model,
        )
        self.last_identity_assessment = SessionIdentityAssessment()

    def concretize(
        self,
        contract: SessionDramaticContract,
        *,
        world_context: dict[str, object],
        recent_contracts: list[SessionDramaticContract] | None = None,
    ) -> SessionDramaticContract:
        self.last_gatekeeper_repair_error = ""
        self.last_gatekeeper_repair_status = "not_needed"
        self.last_gatekeeper_repair_attempts = 0
        if not contract.title:
            return contract
        if self.client is None or not self.model:
            repaired = self._repair_missing_gatekeeper_contracts(
                contract,
                world_context=world_context,
            )
            return self.reachability_reviewer.review(
                repaired,
                world_context=world_context,
            )
        request = {
            "session_brief": self._contract_packet(contract),
            "world_context": world_context,
            "required_chapter_npcs": self._required_chapter_npc_labels(
                world_context.get("active_chapter_package")
            ),
            "recent_session_identities": [
                {
                    "title": item.title,
                    "location": item.location,
                    "signature_image": item.signature_image,
                    "memory_anchor": item.memory_anchor,
                    "ending_echo": item.ending_echo,
                }
                for item in list(recent_contracts or [])[-3:]
            ],
        }
        try:
            raw = self.client.create_chat_completion(
                model=self.model,
                messages=[
                    ChatMessage(role="system", content=self._system_prompt()),
                    ChatMessage(role="user", content=json.dumps(request, ensure_ascii=False)),
                ],
                temperature=0.45,
                response_format={"type": "json_object"},
            )
            payload = extract_json_object(raw)
            concrete = self._merge(contract, payload, world_context=world_context)
            concrete = self._repair_missing_gatekeeper_contracts(
                concrete,
                world_context=world_context,
            )
            assessment = self.identity_guard.assess(
                concrete,
                list(recent_contracts or []),
            )
            if not assessment.distinct:
                repair_request = dict(request)
                repair_request["identity_repair"] = assessment.repair_instruction
                try:
                    repaired_raw = self.client.create_chat_completion(
                        model=self.model,
                        messages=[
                            ChatMessage(role="system", content=self._system_prompt()),
                            ChatMessage(
                                role="user",
                                content=json.dumps(repair_request, ensure_ascii=False),
                            ),
                        ],
                        temperature=0.55,
                        response_format={"type": "json_object"},
                    )
                    repaired_payload = extract_json_object(repaired_raw)
                    repaired = self._merge(
                        contract,
                        repaired_payload,
                        world_context=world_context,
                    )
                    # Identity repair is allowed to reshape the episode, but
                    # it must not erase a gatekeeper's playable bargain.  The
                    # first repair pass ran against the previous draft; review
                    # the replacement draft again before accepting it.
                    repaired = self._repair_missing_gatekeeper_contracts(
                        repaired,
                        world_context=world_context,
                    )
                    repaired_assessment = self.identity_guard.assess(
                        repaired,
                        list(recent_contracts or []),
                    )
                    if (
                        repaired_assessment.distinct
                        or repaired_assessment.similarity < assessment.similarity
                    ):
                        concrete = repaired
                        assessment = repaired_assessment
                except Exception:
                    # The first concrete brief remains usable. Identity repair
                    # is quality assurance, never a reason to block play.
                    pass
            # Keep this final pass even when no identity rewrite occurred.
            # Model drafts can refer to a role by a shortened public title, so
            # a structurally required gate may only become detectable after
            # merge/normalization has supplied the NPC's authority and stance.
            concrete = self._repair_missing_gatekeeper_contracts(
                concrete,
                world_context=world_context,
            )
            self.last_identity_assessment = assessment
            self.last_error = ""
            return self.reachability_reviewer.review(
                concrete,
                world_context=world_context,
            )
        except Exception as exc:  # Session prep failure must never block play.
            self.last_error = str(exc)
            # A large creative-prep request can fail while the much smaller
            # gatekeeper repair remains available. More importantly, a failed
            # optional enhancement must never submit a known route controller
            # with an empty bargain and leave the dialogue model to improvise
            # a different condition on every turn.
            repaired = self._repair_missing_gatekeeper_contracts(
                contract,
                world_context=world_context,
            )
            return self.reachability_reviewer.review(
                repaired,
                world_context=world_context,
            )

    @staticmethod
    def _system_prompt() -> str:
        return (
            "你是《最终物语》单场局势准备器。输入含世界事实与一个可修改的后台场次简报。"
            "只输出JSON，不写解释。你不写固定剧情，也不决定玩家行动；你要把抽象占位词变成GM可反复调用的具体素材。"
            "本场按约四小时准备3到5个可换序或可丢弃的场景机会，围绕同一个可在本场得到局部答案的问题。"
            "必须输出：title、dramatic_question、opening_disruption、signature_image、opposition_goal、dilemma、"
            "reversal_evidence、irreversible_change、closure_requirement、ending_echo、memory_anchor、fantastic_details、"
            "escalation_ladder、possible_payoffs、npcs、clues、scenes。"
            "signature_image必须是首次出镜即可直接描述的一个具体画面，含明确物件及感官细节；不能写准备指令。"
            "opening_disruption必须是开场前已经发生或正在现场发生的具体变化，不是任务摘要。"
            "npcs为1到3名对象，每名含name、public_role、goal_now、leverage、authority_scope、concrete_demand、"
            "acceptance_rule、promised_result、public_lead、fulfillment_routes、refusal_move、voice_cue、private_secret、if_helped、if_blocked。"
            "至少一名应有独特姓名，不能只叫守门人、代理人或关键人物；required_chapter_npcs中的人物必须列入npcs。"
            "所有NPC都必须写清goal_now与authority_scope。只有把帮助作为交换的守门人、交易者或谈判对象才填写"
            "concrete_demand、acceptance_rule、promised_result，而且这三项必须成套出现；证人、受害者、同行者或普通"
            "现场人物可以将三项全部留空。concrete_demand要说清楚究竟要哪件东西、哪个承诺或哪项行动；"
            "acceptance_rule是判断条件是否已经满足的客观标准；authority_scope必须区分NPC能决定与不能决定的事项；"
            "promised_result是条件满足后NPC凭自身权限立即兑现的唯一具体结果，不能偷换成玩家刚问但NPC无权决定的事；"
            "refusal_move是受阻后会实际采取的新动作，不能只是继续犹豫。"
            "若opening_disruption已经说明某名掌握通行、开放或放行权的NPC不愿轻易合作，且本场问题要求争取该权限，"
            "该NPC就是本场守门人：concrete_demand、acceptance_rule、promised_result不得留空。要求必须能在本场通过"
            "不同方法满足，不能强迫唯一解；兑现结果必须是该NPC权限内可以当场做到的事。"
            "交易型NPC还必须给出public_lead和恰好两条fulfillment_routes。public_lead是NPC提出条件时会公开告诉玩家的"
            "起步方向；两条路线必须分别绑定本场已有线索来源、人物、场景入口或现场可执行行动，不能要求玩家知道没有来源的答案。"
            "clues恰好3条，分别适合物证、人物、记录/魔法等不同方法；每条含approach、source、visible_lead、"
            "success_reveal、fallback。线索必须具体且共同支持reversal_evidence，失败仍给代价或替代方向。"
            "scenes为3到5项，每项含scene_role、title、location、situation、purpose、pressure、entry_points、"
            "possible_changes、npc_names；只准备局势，不规定先后或唯一解。"
            "escalation_ladder写3项已经可以实际发生的不同升级，不能连续三次只是逼近、警告或等待。"
            "irreversible_change写本场高潮可能改变的局部事实，不是预设英雄成功；ending_echo写结局后同一标志物"
            "可能如何回应选择。memory_anchor必须明确为一个画面、一个两难选择和一个可追踪后果。"
            "不得新增与世界事实冲突的国家、角色经历或终极真相。未公开暗线可以移动，但已公开事实不可更改。"
            "若world_context.active_chapter_package非空，它是玩家已确认的本场权威骨架：必须保留其synopsis、"
            "intro_prompt、conclusion_prompt、scenes的核心问题与required_elements。可以把场景变得具体、换序、合并或"
            "因玩家选择舍弃可选段落，但不得另起一套无关主线，也不得把章节目标偷换成别的交易或任务。"
            "session_brief.location是本场已经确定的物理地点，不得改名。scenes中的location只能写该地点内部的"
            "房间、街区、道路或邻近子区域；不得把allowed_locations中的另一个远方地点直接搬进同一场，更不得使用"
            "forbidden_backstage_locations。新NPC可以命名，但不得顺手创造新的国家、城市、森林、岛屿或组织。"
            "如果输入包含identity_repair，说明初稿与近期场次过于相似；必须服从其中的差异化要求，并至少改变两个"
            "可游玩轴，不能只换专有名词。"
            "避免抽象词：那件东西、某种担保、当前目标、合适对象、现场阻力、发生变化、继续推进。"
        )

    @staticmethod
    def _gatekeeper_repair_prompt() -> str:
        return (
            "你是《最终物语》场次准备中的守门条件补全器。只输出JSON，不写解释。"
            "输入列出的NPC已经被本场开局确定为掌握眼前通行、开放、放行或带路权限且不愿轻易合作的守门人，"
            "但他的可玩条件缺失。你不得改名、增加NPC、创造新势力、揭露秘密或改变已公开事实。"
            "为每名NPC填写一组三项相互闭合的内容：concrete_demand是NPC此刻真正需要英雄解决的有限顾虑；"
            "acceptance_rule是GM能客观判断已经满足的标准，并应允许至少两种合理做法达成，不能指定唯一台词或唯一技能；"
            "promised_result是满足后NPC凭现有权限立即兑现的一个可见结果。三项都必须具体，不能写再考虑、继续审查、"
            "视情况而定、证明诚意或某种担保。不得要求英雄交出核心任务物、永久失去角色自主权或接受与现场无关的任务。"
            "输出形状：{\"npcs\":[{\"name\":\"原名\",\"concrete_demand\":\"...\","
            "\"acceptance_rule\":\"...\",\"promised_result\":\"...\"}]}。"
        )

    def _repair_missing_gatekeeper_contracts(
        self,
        contract: SessionDramaticContract,
        *,
        world_context: dict[str, object],
    ) -> SessionDramaticContract:
        missing = self._missing_gatekeeper_contracts(contract)
        if not missing:
            if self.last_gatekeeper_repair_attempts == 0:
                self.last_gatekeeper_repair_status = "not_needed"
            return contract
        if self.client is None or not self.model:
            self.last_gatekeeper_repair_status = "fallback_no_model"
            return self._fallback_gatekeeper_contracts(contract, missing)
        request = {
            "session": {
                "title": contract.title,
                "location": contract.location,
                "dramatic_question": contract.dramatic_question,
                "opening_disruption": contract.opening_disruption,
                "closure_requirement": contract.closure_requirement,
                "strong_start": [
                    {
                        "title": scene.title,
                        "situation": scene.situation,
                        "purpose": scene.purpose,
                        "possible_changes": list(scene.possible_changes),
                    }
                    for scene in contract.potential_scenes
                    if scene.scene_role in {"strong_start", "social_or_investigation"}
                ][:2],
            },
            "world_facts": {
                "setting_summary": str(world_context.get("setting_summary") or "")[:1000],
                "active_chapter_package": world_context.get("active_chapter_package") or {},
            },
            "npcs": [
                {
                    "name": npc.name,
                    "public_role": npc.public_role,
                    "goal_now": npc.goal_now,
                    "leverage": npc.leverage,
                    "authority_scope": npc.authority_scope,
                    "refusal_move": npc.refusal_move or npc.if_blocked,
                }
                for npc in missing
            ],
        }
        errors: list[str] = []
        for attempt in range(2):
            self.last_gatekeeper_repair_attempts += 1
            try:
                raw = self.client.create_chat_completion(
                    model=self.model,
                    messages=[
                        ChatMessage(role="system", content=self._gatekeeper_repair_prompt()),
                        ChatMessage(role="user", content=json.dumps(request, ensure_ascii=False)),
                    ],
                    temperature=0.2 if attempt == 0 else 0.1,
                    response_format={"type": "json_object"},
                )
                payload = extract_json_object(raw)
                repairs = payload.get("npcs")
                if not isinstance(repairs, list):
                    errors.append("响应缺少npcs数组")
                    continue
                by_name: dict[str, tuple[str, str, str]] = {}
                for raw_npc in repairs:
                    if not isinstance(raw_npc, dict):
                        continue
                    name = self._clean(raw_npc.get("name"), limit=48)
                    matched = self._match_gatekeeper_repair_name(name, missing)
                    if matched is None:
                        continue
                    demand = self._concrete_text(raw_npc.get("concrete_demand"), limit=220)
                    acceptance = self._concrete_text(raw_npc.get("acceptance_rule"), limit=220)
                    promised = self._concrete_text(raw_npc.get("promised_result"), limit=220)
                    if all((demand, acceptance, promised)):
                        by_name[matched.name] = (demand, acceptance, promised)
                if not by_name:
                    errors.append("响应中没有可绑定的完整守门条件")
                    continue
                repaired_npcs = [
                    replace(
                        npc,
                        concrete_demand=by_name[npc.name][0],
                        acceptance_rule=by_name[npc.name][1],
                        promised_result=by_name[npc.name][2],
                    )
                    if npc.name in by_name
                    else npc
                    for npc in contract.important_npcs
                ]
                repaired = replace(contract, important_npcs=repaired_npcs)
                still_missing = self._missing_gatekeeper_contracts(repaired)
                if not still_missing:
                    self.last_gatekeeper_repair_error = ""
                    self.last_gatekeeper_repair_status = "repaired_by_llm"
                    return repaired
                errors.append("响应只补全了部分守门条件")
                missing = still_missing
                request["npcs"] = [
                    {
                        "name": npc.name,
                        "public_role": npc.public_role,
                        "goal_now": npc.goal_now,
                        "leverage": npc.leverage,
                        "authority_scope": npc.authority_scope,
                        "refusal_move": npc.refusal_move or npc.if_blocked,
                    }
                    for npc in missing
                ]
                contract = repaired
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        self.last_gatekeeper_repair_error = "；".join(errors)[-800:]
        self.last_gatekeeper_repair_status = "fallback_after_llm_failure"
        return self._fallback_gatekeeper_contracts(contract, missing)

    @classmethod
    def _match_gatekeeper_repair_name(
        cls,
        returned_name: str,
        missing: list[SessionNPCRole],
    ) -> SessionNPCRole | None:
        """Bind natural shortened titles without guessing between two NPCs."""

        clean_name = str(returned_name or "").strip()
        if not clean_name:
            return missing[0] if len(missing) == 1 else None
        exact = [npc for npc in missing if clean_name == str(npc.name or "").strip()]
        if len(exact) == 1:
            return exact[0]
        candidates = [
            npc
            for npc in missing
            if any(
                cls._role_label_matches_text(label, clean_name)
                or cls._role_label_matches_text(clean_name, label)
                for label in (npc.name, npc.public_role)
                if label
            )
        ]
        if len(candidates) == 1:
            return candidates[0]
        return None

    @classmethod
    def _fallback_gatekeeper_contracts(
        cls,
        contract: SessionDramaticContract,
        missing: list[SessionNPCRole],
    ) -> SessionDramaticContract:
        """Keep a confirmed route gate playable when the semantic repair fails.

        This fallback is deliberately narrow: it only uses an authority scope
        already saying that the NPC controls access. It never creates a new
        faction, secret, item, or side quest.
        """

        replacements: dict[str, tuple[str, str, str]] = {}
        scene_text = " ".join(
            (
                str(contract.dramatic_question or ""),
                str(contract.opening_disruption or ""),
                str(contract.closure_requirement or ""),
            )
        )
        for npc in missing:
            authority = str(npc.authority_scope or "")
            if not re.search(r"(?:开放|放行|通行|借路|开门|开闸|带路|许可)", authority):
                continue
            combined = " ".join(
                (
                    scene_text,
                    str(npc.goal_now or ""),
                    str(npc.refusal_move or ""),
                    authority,
                )
            )
            route = next(
                (
                    label
                    for label in ("旧路", "侧门", "闸门", "关口", "入口", "路线")
                    if label in combined
                ),
                "当前通路",
            )
            escorted = bool(re.search(r"(?:旅人|护送|送往|送去|被护送)", combined))
            if escorted:
                demand = f"说明被护送者要经由{route}前往的明确去向，并解决沿途由谁承担护送责任的顾虑"
                acceptance = (
                    "目的地已经明确，且至少一人承担沿途护送责任，或队伍提出了同等可信、"
                    "能避免把追兵带入通路的安全安排"
                )
            else:
                demand = f"说明经由{route}前往的明确地点，并解决通行会把风险带入此处的顾虑"
                acceptance = (
                    "目的地已经明确，且至少一人承担通行期间的安全责任，或队伍提出了同等可信的风险控制办法"
                )
            if re.search(r"(?:带路|巡守|向导)", authority):
                promised = f"立即临时开放{route}并安排权限内的人员带队通过第一处安全界标"
            else:
                promised = f"立即临时开放{route}并放行队伍"
            replacements[npc.name] = (demand, acceptance, promised)

        if not replacements:
            return contract
        return replace(
            contract,
            important_npcs=[
                replace(
                    npc,
                    concrete_demand=replacements[npc.name][0],
                    acceptance_rule=replacements[npc.name][1],
                    promised_result=replacements[npc.name][2],
                )
                if npc.name in replacements
                else npc
                for npc in contract.important_npcs
            ],
        )

    @classmethod
    def _missing_gatekeeper_contracts(
        cls,
        contract: SessionDramaticContract,
    ) -> list[SessionNPCRole]:
        opening = " ".join(
            str(item or "")
            for item in (
                contract.opening_disruption,
                contract.dramatic_question,
                contract.closure_requirement,
                *(
                    scene.situation
                    for scene in contract.potential_scenes
                    if scene.scene_role in {"strong_start", "social_or_investigation"}
                ),
            )
        )
        # Do not manufacture a bargain merely because somebody owns a door.
        # A repair is warranted when either the opening explicitly withholds
        # access, or the structured dramatic/closure question says gaining
        # that access is one of this session's unresolved objectives.
        reluctant_gate = bool(re.search(
            r"(?:不愿|不肯|拒绝|不准|不让|不轻易|暂不|尚未).{0,24}"
            r"(?:开放|放行|通行|借路|开门|开闸|带路)|"
            r"(?:开放|放行|通行|借路|开门|开闸|带路).{0,24}"
            r"(?:条件|要求|交换|担保)",
            opening,
        ))
        structured_gate_goal = bool(re.search(
            r"(?:能否|是否|争取|获得|取得|需要|必须|要求).{0,36}"
            r"(?:旧路|路线|通行|开放|放行|开门|开闸|带路)|"
            r"(?:旧路|路线|通行|开放|放行|开门|开闸|带路).{0,36}"
            r"(?:能否|是否|争取|获得|取得|条件|许可)",
            " ".join((contract.dramatic_question, contract.closure_requirement)),
        ))
        if not (reluctant_gate or structured_gate_goal):
            return []
        result: list[SessionNPCRole] = []
        for npc in contract.important_npcs:
            if all((npc.concrete_demand, npc.acceptance_rule, npc.promised_result)):
                continue
            identity = " ".join((str(npc.name or ""), str(npc.public_role or "")))
            stance = " ".join(
                (
                    str(npc.goal_now or ""),
                    str(npc.refusal_move or ""),
                    str(npc.if_blocked or ""),
                )
            )
            identity_mentioned = any(
                cls._role_label_matches_text(label, opening)
                for label in (npc.name, npc.public_role)
                if label
            )
            structured_gatekeeper = bool(
                structured_gate_goal
                and re.search(
                    r"(?:决定|控制|掌握|负责|维持|警戒|要求).{0,28}"
                    r"(?:开放|放行|通行|旧路|路线|去向|责任|条件)|"
                    r"(?:开放|放行|通行|旧路|路线).{0,28}"
                    r"(?:决定|控制|掌握|负责|警戒|要求)",
                    stance,
                )
            )
            if not (identity_mentioned or structured_gatekeeper):
                continue
            authority = str(npc.authority_scope or "")
            if not re.search(r"(?:开放|放行|通行|借路|开门|开闸|带路|许可)", authority):
                continue
            if re.search(r"(?:旅人|受害者|伤员|证人|俘虏)", identity) and not re.search(
                r"(?:会长|守门|巡守|队长|领主|官|长)", identity
            ):
                continue
            result.append(npc)
        return result

    @staticmethod
    def _role_label_matches_text(label: str, text: str) -> bool:
        """Match a full NPC title against a natural shortened table reference."""

        clean_label = re.sub(r"\s+", "", str(label or ""))
        clean_text = re.sub(r"\s+", "", str(text or ""))
        if not clean_label or not clean_text:
            return False
        if clean_label in clean_text:
            return True
        # Public prose routinely drops a place/faction prefix: e.g.
        # “白花守望会会长” becomes “守望会会长”.  Require at least four
        # characters so generic titles such as “会长” do not bind randomly.
        maximum = min(len(clean_label), 12)
        return any(
            clean_label[-size:] in clean_text
            for size in range(maximum, 3, -1)
        )

    @staticmethod
    def _contract_packet(contract: SessionDramaticContract) -> dict[str, object]:
        return {
            "session_number": contract.session_number,
            "title": contract.title,
            "location": contract.location,
            "dramatic_question": contract.dramatic_question,
            "opening_disruption": contract.opening_disruption,
            "signature_image": contract.signature_image,
            "spotlight_hero": contract.spotlight_hero,
            "focus_thread": contract.focus_thread,
            "opposition_goal": contract.opposition_goal,
            "dilemma": contract.dilemma,
            "reversal": contract.reversal,
            "climax_type": contract.climax_type,
            "closure_requirement": contract.closure_requirement,
            "situation_facts": contract.situation_facts,
            "flexible_secrets": contract.flexible_secrets,
            "existing_npcs": [item.name for item in contract.important_npcs],
            "existing_clue_approaches": [item.approach for item in contract.clue_routes],
            "existing_scene_roles": [item.scene_role for item in contract.potential_scenes],
        }

    def _merge(
        self,
        contract: SessionDramaticContract,
        payload: dict[str, object],
        *,
        world_context: dict[str, object],
    ) -> SessionDramaticContract:
        chapter = (
            dict(world_context.get("active_chapter_package") or {})
            if isinstance(world_context.get("active_chapter_package"), dict)
            else {}
        )
        forbidden = {
            str(item).strip()
            for item in world_context.get("forbidden_backstage_locations", [])
            if str(item).strip()
        }
        signature = self._safe_world_text(
            payload.get("signature_image"), limit=320, forbidden=forbidden
        )
        opening = self._safe_world_text(
            payload.get("opening_disruption"), limit=360, forbidden=forbidden
        )
        reversal = self._safe_world_text(
            payload.get("reversal_evidence"), limit=320, forbidden=forbidden
        )
        if not reversal:
            reversal = contract.reversal
        npcs = self._normalize_npcs(payload.get("npcs"), forbidden=forbidden)
        npcs = self._ensure_required_chapter_npcs(
            npcs,
            chapter=chapter,
            contract=contract,
        )
        prepared_npcs = npcs or [
            item
            for item in contract.important_npcs
            if not self._is_generic_npc_name(item.name)
        ]
        clues = self._normalize_clues(
            payload.get("clues"), contract, conclusion=reversal, forbidden=forbidden
        )
        scenes = self._normalize_scenes(
            payload.get("scenes"), contract, prepared_npcs, clues, forbidden=forbidden
        )
        if chapter:
            scenes = self._anchor_chapter_scenes(
                scenes,
                contract=contract,
                chapter=chapter,
                npcs=prepared_npcs,
                clues=clues or contract.clue_routes,
            )
        else:
            scenes = self._ensure_session_scene_structure(scenes, contract)
        chapter_title = self._clean(chapter.get("chapter_title"), limit=120)
        chapter_intro = self._safe_world_text(
            chapter.get("intro_prompt"), limit=360, forbidden=forbidden
        )
        chapter_conclusion = self._clean(chapter.get("conclusion_prompt"), limit=320)
        chapter_has_adversary = bool(self._clean_list(chapter.get("adversary_notes"), limit=3, item_limit=260))
        # A partially generic response is worse than the stable base brief.
        # Keep each section independently replaceable, but only adopt lists
        # when they contain enough playable material.
        return replace(
            contract,
            title=chapter_title or self._clean(payload.get("title"), limit=120) or contract.title,
            dramatic_question=(
                contract.dramatic_question
                if chapter
                else self._clean(payload.get("dramatic_question"), limit=260)
                or contract.dramatic_question
            ),
            opening_disruption=chapter_intro or opening or contract.opening_disruption,
            signature_image=signature or contract.signature_image,
            opposition_goal=(
                contract.opposition_goal
                if chapter_has_adversary
                else self._clean(payload.get("opposition_goal"), limit=260)
                or contract.opposition_goal
            ),
            dilemma=self._clean(payload.get("dilemma"), limit=300) or contract.dilemma,
            reversal=reversal,
            closure_requirement=chapter_conclusion
            or self._clean(payload.get("closure_requirement"), limit=320)
            or contract.closure_requirement,
            fantastic_details=self._clean_list(payload.get("fantastic_details"), limit=5, item_limit=240)
            or contract.fantastic_details,
            escalation_ladder=self._clean_list(payload.get("escalation_ladder"), limit=4, item_limit=240)
            or contract.escalation_ladder,
            possible_payoffs=self._clean_list(payload.get("possible_payoffs"), limit=4, item_limit=240)
            or contract.possible_payoffs,
            important_npcs=prepared_npcs,
            clue_routes=clues if len(clues) >= 3 else contract.clue_routes,
            potential_scenes=scenes if len(scenes) >= 3 else contract.potential_scenes,
            irreversible_change=self._clean(payload.get("irreversible_change"), limit=320)
            or contract.irreversible_change,
            ending_echo=self._clean(payload.get("ending_echo"), limit=320)
            or contract.ending_echo,
            memory_anchor=self._clean(payload.get("memory_anchor"), limit=360)
            or contract.memory_anchor,
        )

    @staticmethod
    def _ensure_session_scene_structure(
        candidates: list[SessionSceneOpportunity],
        contract: SessionDramaticContract,
    ) -> list[SessionSceneOpportunity]:
        """Keep model prep movable while guaranteeing a playable evening.

        This does not impose a plot order.  It only ensures that the prepared
        set contains a strong start, at least one development route, a climax
        situation and an aftermath, each with a distinguishable camera.  The
        GM may still move, combine or discard opportunities during play.
        """

        required_groups = (
            ("strong_start",),
            ("social_or_investigation", "alternate_approach"),
            ("climax_candidate",),
            ("aftermath",),
        )
        result = list(candidates[:5])
        fallbacks = list(contract.potential_scenes or [])
        for group in required_groups:
            if any(item.scene_role in group for item in result):
                continue
            fallback = next(
                (item for item in fallbacks if item.scene_role in group),
                None,
            )
            if fallback is None:
                role = group[0]
                labels = {
                    "strong_start": ("被打断的当下", "让正在发生的麻烦先改变现场，再交出行动权"),
                    "social_or_investigation": ("人物与证据", "让人物目标或一条可验证证据成为可互动局面"),
                    "climax_candidate": ("本场决断点", "让核心问题获得答案或不可逆改变"),
                    "aftermath": ("选择之后", "兑现局部结果、记录代价并让角色回应"),
                }
                label, purpose = labels[role]
                fallback = SessionSceneOpportunity(
                    scene_key=f"s{contract.session_number:02d}-{role}",
                    scene_role=role,
                    title=label,
                    location=contract.location,
                    situation=(
                        "同一标志物、人物或地点因英雄刚才的选择呈现出新的状态"
                        if role == "aftermath"
                        else contract.opening_disruption or contract.dramatic_question
                    ),
                    purpose=purpose,
                    pressure=(
                        "不得用新谜团覆盖刚刚取得的结果"
                        if role == "aftermath"
                        else contract.opposition_goal
                    ),
                    entry_points=["观察现场反应", "与相关人物交涉", "决定下一步"],
                    possible_changes=["人物态度改变", "地点或关系状态被记录"],
                    optional=role not in {"strong_start", "climax_candidate", "aftermath"},
                )
            result.append(fallback)

        role_order = {
            "strong_start": 0,
            "social_or_investigation": 1,
            "alternate_approach": 2,
            "climax_candidate": 3,
            "aftermath": 4,
        }
        result.sort(key=lambda item: role_order.get(item.scene_role, 2))
        if len(result) > 5:
            middle = [
                item
                for item in result
                if item.scene_role in {"social_or_investigation", "alternate_approach"}
            ][:2]
            result = [
                *[item for item in result if item.scene_role == "strong_start"][:1],
                *middle,
                *[item for item in result if item.scene_role == "climax_candidate"][:1],
                *[item for item in result if item.scene_role == "aftermath"][:1],
            ]

        fallback_by_role = {
            item.scene_role: item for item in fallbacks if item.location
        }
        used_locations: set[str] = set()
        repaired: list[SessionSceneOpportunity] = []
        suffix_by_role = {
            "strong_start": "入口",
            "social_or_investigation": "会面处",
            "alternate_approach": "侧路",
            "climax_candidate": "冲突中心",
            "aftermath": "事后落脚处",
        }
        for index, item in enumerate(result, start=1):
            location = str(item.location or contract.location).strip()
            if location in used_locations:
                fallback = fallback_by_role.get(item.scene_role)
                fallback_location = str(getattr(fallback, "location", "") or "").strip()
                if fallback_location and fallback_location not in used_locations:
                    location = fallback_location
                else:
                    root = str(contract.location or location or "当前地点").split("·", 1)[0]
                    suffix = suffix_by_role.get(item.scene_role, f"场景{index}")
                    location = f"{root}·{suffix}"
            used_locations.add(location)
            repaired.append(
                replace(
                    item,
                    scene_key=f"s{contract.session_number:02d}-{item.scene_role}-{index}",
                    location=location,
                    optional=item.scene_role
                    not in {"strong_start", "climax_candidate", "aftermath"},
                )
            )
        return repaired

    def _anchor_chapter_scenes(
        self,
        candidates: list[SessionSceneOpportunity],
        *,
        contract: SessionDramaticContract,
        chapter: dict[str, object],
        npcs: list[SessionNPCRole],
        clues: list[SessionClueRoute],
    ) -> list[SessionSceneOpportunity]:
        """Keep every required chapter scene even when concretization drifts."""

        raw_scenes = chapter.get("scenes")
        if not isinstance(raw_scenes, list) or not raw_scenes:
            return candidates
        chapter_scenes = raw_scenes[:5]
        anchored: list[SessionSceneOpportunity] = []
        for index, raw in enumerate(chapter_scenes):
            if not isinstance(raw, dict):
                continue
            title = self._clean(raw.get("title"), limit=100) or f"章节场景{index + 1}"
            required = self._clean_list(raw.get("required_elements"), limit=6, item_limit=160)
            purpose = self._clean(raw.get("purpose"), limit=220)
            candidate = candidates[index] if index < len(candidates) else None
            if candidate is None or not self._scene_matches_chapter(
                candidate,
                title=title,
                purpose=purpose,
                required=required,
            ):
                candidate = self._chapter_scene_fallback(
                    contract,
                    raw,
                    index=index,
                    npcs=npcs,
                    clues=clues,
                )
            else:
                situation = str(candidate.situation or "").strip()
                missing = [item for item in required if item not in situation]
                if missing:
                    situation = (situation.rstrip("；。") + "；必须出现：" + "、".join(missing)).strip("；")
                candidate = replace(
                    candidate,
                    scene_key=f"s{contract.session_number:02d}-chapter-{index + 1}",
                    title=title,
                    location=self._clean(raw.get("location"), limit=160)
                    or candidate.location,
                    purpose=purpose or candidate.purpose,
                    situation=situation,
                    pressure=self._clean(raw.get("when_to_use"), limit=220) or candidate.pressure,
                    possible_changes=self._chapter_possible_changes(raw)
                    or candidate.possible_changes,
                    optional=True,
                )
            required_elements, required_npc_names = self._required_scene_cast(
                required,
                npcs,
            )
            chapter_role = self._chapter_scene_role(
                raw.get("scene_type"),
                index=index,
                count=len(chapter_scenes),
            )
            candidate = replace(
                candidate,
                scene_role=chapter_role,
                required_elements=required_elements,
                required_npc_names=required_npc_names,
                optional=chapter_role
                not in {"strong_start", "climax_candidate", "aftermath"},
            )
            anchored.append(candidate)
        if anchored and not any(item.scene_role == "aftermath" for item in anchored):
            aftermath = next(
                (item for item in candidates if item.scene_role == "aftermath"),
                None,
            ) or next(
                (
                    item
                    for item in contract.potential_scenes
                    if item.scene_role == "aftermath"
                ),
                None,
            )
            if aftermath is None:
                climax = next(
                    (
                        item
                        for item in reversed(anchored)
                        if item.scene_role == "climax_candidate"
                    ),
                    anchored[-1],
                )
                aftermath = SessionSceneOpportunity(
                    scene_key=f"s{contract.session_number:02d}-chapter-aftermath",
                    scene_role="aftermath",
                    title=f"{climax.title}之后",
                    location=self._chapter_aftermath_location(
                        contract.location,
                        climax.title,
                    ),
                    situation=f"【{climax.title}】的结果已经落地，现场因英雄的选择呈现新状态。",
                    purpose="兑现本场结果与代价，让角色回应后收束，不开启新任务。",
                    pressure="只在高潮产生明确结果后使用。",
                    entry_points=["处理伤者与资源", "确认人物态度", "决定离场方式"],
                    possible_changes=["地点或关系状态被记录", "长期后果进入下一场"],
                    npc_names=[item.name for item in npcs[:2]],
                    optional=False,
                )
            anchored.append(
                replace(
                    aftermath,
                    scene_key=f"s{contract.session_number:02d}-chapter-aftermath",
                    scene_role="aftermath",
                    optional=False,
                )
            )
        return anchored or candidates

    @staticmethod
    def _scene_matches_chapter(
        scene: SessionSceneOpportunity,
        *,
        title: str,
        purpose: str,
        required: list[str],
    ) -> bool:
        text = " ".join((scene.title, scene.situation, scene.purpose))
        if title and title in text:
            return True
        if any(item and item in text for item in required):
            return True
        compact_purpose = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", purpose)
        compact_text = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text)
        return bool(compact_purpose and compact_purpose[:8] in compact_text)

    def _chapter_scene_fallback(
        self,
        contract: SessionDramaticContract,
        raw: dict[str, object],
        *,
        index: int,
        npcs: list[SessionNPCRole],
        clues: list[SessionClueRoute],
    ) -> SessionSceneOpportunity:
        title = self._clean(raw.get("title"), limit=100) or f"章节场景{index + 1}"
        required = self._clean_list(raw.get("required_elements"), limit=6, item_limit=160)
        purpose = self._clean(raw.get("purpose"), limit=220)
        situation = purpose
        if required:
            situation = (situation.rstrip("；。") + "；必须出现：" + "、".join(required)).strip("；")
        role = self._chapter_scene_role(
            raw.get("scene_type"),
            index=index,
            count=max(index + 1, 2),
        )
        return SessionSceneOpportunity(
            scene_key=f"s{contract.session_number:02d}-chapter-{index + 1}",
            scene_role=role,
            title=title,
            location=self._clean(raw.get("location"), limit=160) or contract.location,
            situation=situation or title,
            purpose=purpose,
            pressure=self._clean(raw.get("when_to_use"), limit=220),
            entry_points=self._clean_list(raw.get("optional_elements"), limit=4, item_limit=120)
            or required[:4],
            possible_changes=self._chapter_possible_changes(raw),
            clue_route_ids=[item.route_id for item in clues[:3]],
            npc_names=[item.name for item in npcs[:3]],
            required_elements=self._required_scene_cast(required, npcs)[0],
            required_npc_names=self._required_scene_cast(required, npcs)[1],
            optional=index != 0,
        )

    @staticmethod
    def _chapter_scene_role(raw_type: object, *, index: int, count: int) -> str:
        if index == 0:
            return "strong_start"
        scene_type = str(raw_type or "").strip().lower()
        if scene_type in {"aftermath", "epilogue", "interlude", "rest", "余波", "尾声"}:
            return "aftermath"
        if scene_type in {
            "climax",
            "boss",
            "finale",
            "conflict",
            "combat",
            "高潮",
            "首领",
        }:
            return "climax_candidate"
        if scene_type in {
            "social",
            "social_conflict",
            "dialogue",
            "negotiation",
            "社交",
            "交涉",
        }:
            return "social_or_investigation"
        if scene_type in {
            "investigation",
            "exploration",
            "ritual",
            "dungeon",
            "调查",
            "探索",
            "仪式",
        }:
            return "alternate_approach"
        if index == count - 1:
            return "climax_candidate"
        return "alternate_approach"

    @staticmethod
    def _chapter_aftermath_location(location: str, climax_title: str) -> str:
        base = str(location or "").strip()
        title = str(climax_title or "").strip()
        if any(token in title for token in ("旧路", "闸门", "门", "关口")):
            suffix = "旧路出口外"
        elif any(token in title for token in ("船", "港", "海", "码头")):
            suffix = "靠岸处"
        elif any(token in title for token in ("遗迹", "迷宫", "塔", "矿井", "洞窟")):
            suffix = "出口"
        else:
            suffix = "事后落脚处"
        return f"{base}·{suffix}" if base else suffix

    @classmethod
    def _required_scene_cast(
        cls,
        required: list[str],
        npcs: list[SessionNPCRole],
    ) -> tuple[list[str], list[str]]:
        """Separate visible props from required prepared NPC appearances."""

        visible: list[str] = []
        required_names: list[str] = []
        for raw in required:
            item = str(raw or "").strip()
            if not item:
                continue
            matched = next(
                (
                    npc
                    for npc in npcs
                    if cls._npc_matches_required_element(item, npc)
                ),
                None,
            )
            if matched is None:
                if item not in visible:
                    visible.append(item)
                continue
            name = str(matched.name or "").strip()
            if name and name not in required_names:
                required_names.append(name)
        return visible, required_names

    @staticmethod
    def _npc_matches_required_element(required: str, npc: SessionNPCRole) -> bool:
        item = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(required or ""))
        labels = [
            re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(value or ""))
            for value in (npc.name, npc.public_role)
        ]
        if any(label and (label in item or item in label) for label in labels):
            return True
        role_terms = (
            "会长",
            "监察官",
            "守门人",
            "巡守长",
            "钟匠",
            "掌柜",
            "祭司",
            "书记官",
            "旅人",
            "队长",
            "领主",
        )
        return any(term in item and any(term in label for label in labels) for term in role_terms)

    def _chapter_possible_changes(self, raw: dict[str, object]) -> list[str]:
        return [
            text
            for text in (
                self._clean(raw.get("success_condition"), limit=180),
                self._clean(raw.get("exit_condition"), limit=180),
            )
            if text
        ]

    def _normalize_npcs(
        self,
        raw: object,
        *,
        forbidden: set[str],
    ) -> list[SessionNPCRole]:
        if not isinstance(raw, list):
            return []
        result: list[SessionNPCRole] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = stable_npc_identity_label(item.get("name"))
            if self._is_generic_npc_name(name) or name in seen:
                continue
            if self._mentions_forbidden(name, forbidden):
                continue
            goal = self._clean(item.get("goal_now"), limit=220)
            demand = self._concrete_text(item.get("concrete_demand"), limit=220)
            acceptance = self._concrete_text(item.get("acceptance_rule"), limit=220)
            promised_result = self._concrete_text(item.get("promised_result"), limit=220)
            public_lead = self._concrete_text(item.get("public_lead"), limit=240)
            fulfillment_routes = self._clean_list(
                item.get("fulfillment_routes"),
                limit=2,
                item_limit=240,
            )
            if not goal:
                continue
            if not all((demand, acceptance, promised_result)):
                demand = ""
                acceptance = ""
                promised_result = ""
                public_lead = ""
                fulfillment_routes = []
            seen.add(name)
            result.append(
                SessionNPCRole(
                    name=name,
                    public_role=self._clean(item.get("public_role"), limit=120) or "现场人物",
                    goal_now=goal,
                    leverage=self._clean(item.get("leverage"), limit=220),
                    authority_scope=self._concrete_text(item.get("authority_scope"), limit=220)
                    or self._DEFAULT_AUTHORITY_SCOPE,
                    concrete_demand=demand,
                    acceptance_rule=acceptance,
                    promised_result=promised_result,
                    public_lead=public_lead,
                    fulfillment_routes=fulfillment_routes,
                    refusal_move=self._concrete_text(item.get("refusal_move"), limit=220),
                    voice_cue=self._clean(item.get("voice_cue"), limit=180),
                    private_secret=self._clean(item.get("private_secret"), limit=260),
                    if_helped=self._clean(item.get("if_helped"), limit=220),
                    if_blocked=self._clean(item.get("if_blocked"), limit=220),
                )
            )
            if len(result) >= 3:
                break
        return result

    @classmethod
    def _required_chapter_npc_labels(cls, raw_chapter: object) -> list[str]:
        if not isinstance(raw_chapter, dict):
            return []
        result: list[str] = []
        scenes = raw_chapter.get("scenes")
        if not isinstance(scenes, list):
            return result
        for scene in scenes:
            if not isinstance(scene, dict):
                continue
            candidates: list[tuple[object, bool]] = []
            for key in ("required_npc_names", "required_elements"):
                values = scene.get(key)
                if isinstance(values, list):
                    candidates.extend((value, key == "required_npc_names") for value in values)
            for raw, explicitly_npc in candidates:
                label = " ".join(str(raw or "").split()).strip()
                if not label or label in result:
                    continue
                if explicitly_npc or any(term in label for term in cls._NPC_ROLE_TERMS):
                    result.append(label[:80])
        return result[:4]

    @classmethod
    def _ensure_required_chapter_npcs(
        cls,
        npcs: list[SessionNPCRole],
        *,
        chapter: dict[str, object],
        contract: SessionDramaticContract,
    ) -> list[SessionNPCRole]:
        required = cls._required_chapter_npc_labels(chapter)
        if not required:
            return npcs

        prepared = list(npcs)
        for label in required:
            context = cls._chapter_npc_context(
                label,
                chapter=chapter,
                contract=contract,
            )
            prepared_index = next(
                (
                    index
                    for index, npc in enumerate(prepared)
                    if cls._npc_matches_required_element(label, npc)
                ),
                -1,
            )
            if prepared_index >= 0:
                prepared[prepared_index] = cls._enrich_required_npc(
                    prepared[prepared_index],
                    label=label,
                    context=context,
                )
                continue
            inherited = next(
                (
                    npc
                    for npc in contract.important_npcs
                    if cls._npc_matches_required_element(label, npc)
                    and not cls._is_generic_npc_name(str(npc.name or "").strip())
                ),
                None,
            )
            if inherited is not None:
                prepared.append(
                    cls._enrich_required_npc(
                        inherited,
                        label=label,
                        context=context,
                    )
                )
                continue
            profile = local_role_profile(label, context=context)
            prepared.append(
                SessionNPCRole(
                    name=label,
                    public_role=label,
                    goal_now=profile.get("goal_now", "面对当前局势，并依照自身处境作出选择"),
                    leverage=profile.get("leverage", ""),
                    authority_scope=profile.get("authority_scope", cls._DEFAULT_AUTHORITY_SCOPE),
                    refusal_move=profile.get("refusal_move", ""),
                    voice_cue=profile.get("voice_cue", ""),
                    if_helped=(
                        "在自身权限范围内立即兑现已经答应的帮助"
                        if profile
                        else ""
                    ),
                    if_blocked=profile.get("refusal_move", ""),
                )
            )

        required_roles = [
            npc
            for npc in prepared
            if any(cls._npc_matches_required_element(label, npc) for label in required)
        ]
        optional_roles = [npc for npc in prepared if npc not in required_roles]
        ordered: list[SessionNPCRole] = []
        for npc in (*required_roles, *optional_roles):
            if any(existing.name == npc.name for existing in ordered):
                continue
            ordered.append(npc)
        return ordered[:3]

    @classmethod
    def _enrich_required_npc(
        cls,
        npc: SessionNPCRole,
        *,
        label: str,
        context: str,
    ) -> SessionNPCRole:
        values = enrich_role_record(vars(npc), target=label, context=context)
        return replace(
            npc,
            goal_now=values.get("goal_now", npc.goal_now),
            leverage=values.get("leverage", npc.leverage),
            authority_scope=values.get("authority_scope", npc.authority_scope),
            refusal_move=values.get("refusal_move", npc.refusal_move),
            voice_cue=values.get("voice_cue", npc.voice_cue),
            if_helped=values.get("if_helped", npc.if_helped),
            if_blocked=values.get("if_blocked", npc.if_blocked),
        )

    @classmethod
    def _chapter_npc_context(
        cls,
        label: str,
        *,
        chapter: dict[str, object],
        contract: SessionDramaticContract,
    ) -> str:
        parts = [
            contract.location,
            contract.dramatic_question,
            contract.opening_disruption,
            contract.closure_requirement,
            str(chapter.get("synopsis") or ""),
            str(chapter.get("intro_prompt") or ""),
            str(chapter.get("conclusion_prompt") or ""),
        ]
        for scene in chapter.get("scenes") or []:
            if not isinstance(scene, dict):
                continue
            required = [
                str(item or "")
                for key in ("required_npc_names", "required_elements")
                for item in (scene.get(key) or [])
                if isinstance(scene.get(key), list)
            ]
            if required and not any(
                cls._npc_matches_required_element(
                    item,
                    SessionNPCRole(name=label, public_role=label),
                )
                for item in required
            ):
                continue
            parts.extend(
                str(scene.get(key) or "")
                for key in (
                    "title",
                    "location",
                    "purpose",
                    "success_condition",
                    "exit_condition",
                )
            )
        return "；".join(" ".join(str(item or "").split()) for item in parts if str(item or "").strip())

    def _normalize_clues(
        self,
        raw: object,
        contract: SessionDramaticContract,
        *,
        conclusion: str,
        forbidden: set[str],
    ) -> list[SessionClueRoute]:
        if not isinstance(raw, list):
            return []
        result: list[SessionClueRoute] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            visible = self._concrete_text(item.get("visible_lead"), limit=260)
            reveal = self._concrete_text(item.get("success_reveal"), limit=320)
            source = self._concrete_text(item.get("source"), limit=180)
            if self._is_generic_npc_name(source):
                source = ""
            if not visible or not reveal or not source:
                continue
            if any(
                self._mentions_forbidden(text, forbidden)
                for text in (visible, reveal, source)
            ):
                continue
            route_id = (
                contract.clue_routes[index].route_id
                if index < len(contract.clue_routes)
                else f"s{contract.session_number:02d}-route-{index + 1}"
            )
            result.append(
                SessionClueRoute(
                    route_id=route_id,
                    conclusion=conclusion,
                    approach=self._clean(item.get("approach"), limit=100) or "调查",
                    source=source,
                    visible_lead=visible,
                    success_reveal=reveal,
                    fallback=self._clean(item.get("fallback"), limit=260),
                )
            )
            if len(result) >= 3:
                break
        return result

    def _normalize_scenes(
        self,
        raw: object,
        contract: SessionDramaticContract,
        npcs: list[SessionNPCRole],
        clues: list[SessionClueRoute],
        *,
        forbidden: set[str],
    ) -> list[SessionSceneOpportunity]:
        if not isinstance(raw, list):
            return []
        result: list[SessionSceneOpportunity] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            situation = self._concrete_text(item.get("situation"), limit=320)
            title = self._clean(item.get("title"), limit=100)
            if not situation or not title:
                continue
            if any(
                self._mentions_forbidden(text, forbidden)
                for text in (situation, title)
            ):
                continue
            role = self._clean(item.get("scene_role"), limit=48) or (
                contract.potential_scenes[index].scene_role
                if index < len(contract.potential_scenes)
                else "situation"
            )
            requested_npcs = [
                name
                for name in self._clean_list(
                    item.get("npc_names"), limit=3, item_limit=48
                )
                if not self._is_generic_npc_name(name)
                and any(name == npc.name for npc in npcs)
            ]
            result.append(
                SessionSceneOpportunity(
                    scene_key=f"s{contract.session_number:02d}-{role}-{index + 1}",
                    scene_role=role,
                    title=title,
                    location=self._anchored_scene_location(
                        item.get("location"),
                        contract.location,
                        forbidden=forbidden,
                    ),
                    situation=situation,
                    purpose=self._clean(item.get("purpose"), limit=220),
                    pressure=self._clean(item.get("pressure"), limit=220),
                    entry_points=self._clean_list(item.get("entry_points"), limit=4, item_limit=120),
                    possible_changes=self._clean_list(item.get("possible_changes"), limit=4, item_limit=160),
                    clue_route_ids=[route.route_id for route in (clues or contract.clue_routes)[:3]],
                    npc_names=requested_npcs or [npc.name for npc in npcs[:2]],
                    required_elements=self._clean_list(
                        item.get("required_elements"), limit=6, item_limit=160
                    ),
                    required_npc_names=self._clean_list(
                        item.get("required_npc_names"), limit=3, item_limit=48
                    ),
                    optional=role not in {"strong_start", "climax_candidate", "aftermath"},
                )
            )
            if len(result) >= 5:
                break
        return result

    def _safe_world_text(
        self,
        value: object,
        *,
        limit: int,
        forbidden: set[str],
    ) -> str:
        text = self._concrete_text(value, limit=limit)
        if self._mentions_forbidden(text, forbidden):
            return ""
        return text

    def _anchored_scene_location(
        self,
        value: object,
        root: str,
        *,
        forbidden: set[str],
    ) -> str:
        location = self._clean(value, limit=100)
        root = self._clean(root, limit=100)
        if not location or self._mentions_forbidden(location, forbidden):
            return root
        if location == root or root in location:
            return location
        return f"{root}·{location}" if root else location

    @staticmethod
    def _mentions_forbidden(text: str, forbidden: set[str]) -> bool:
        clean = str(text or "")
        return any(name and name in clean for name in forbidden)

    def _concrete_text(self, value: object, *, limit: int) -> str:
        text = self._clean(value, limit=limit)
        lower = text.lower()
        if not text or any(marker.lower() in lower for marker in self._GENERIC_MARKERS):
            return ""
        return text

    @classmethod
    def _is_generic_npc_name(cls, value: object) -> bool:
        name = " ".join(str(value or "").split()).strip()
        if not name or name in cls._GENERIC_NPC_NAMES:
            return True
        return name.endswith(cls._FACTION_SUFFIXES)

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
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for item in value:
            clean = cls._clean(item, limit=item_limit)
            if clean and clean not in result:
                result.append(clean)
            if len(result) >= limit:
                break
        return result
