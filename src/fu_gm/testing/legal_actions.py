from __future__ import annotations

from dataclasses import asdict
import re
from typing import Any

from fu_gm.http_server import FUGMHttpService
from fu_gm.components.scene_access_boundary import SceneAccessBoundary
from fu_gm.components.portable_device_rules import portable_device_tiers
from fu_gm.components.spell_parameter_manager import ATTRIBUTE_LABELS, DAMAGE_TYPE_LABELS, STATUS_LABELS
from fu_gm.skill_library import get_skill_reference, skill_implementation_coverage
from fu_gm.spellbook import get_spell_definition
from fu_gm.testing.replay_models import LegalActionContext, ReplayScenario, ReplayStep


class LegalActionLayer:
    """Build a constrained action menu for synthetic players.

    This layer intentionally talks in Final Fabula terms rather than raw Python
    action names. The player simulator may phrase the action naturally, but it
    should not invent unsupported mechanics outside this menu.
    """

    def build(
        self,
        service: FUGMHttpService,
        scenario: ReplayScenario,
        step: ReplayStep,
        *,
        public_context: str = "",
    ) -> LegalActionContext:
        runtime = service.runtimes.get(scenario.campaign_id)
        if runtime is None:
            return LegalActionContext(
                stage_goal=step.stage_goal,
                legal_actions=["新建战役", "进入第零章"],
                notes=["当前还没有运行时，只能进行战役初始化或第零章开场。"],
            )

        app = runtime.app
        pcs = [character for character in app.character_manager.all() if "pc" in character.traits]
        enemies = [
            character
            for character in app.character_manager.all()
            if "enemy" in character.traits or "villain" in character.traits
        ]
        current_actor = app.conflict_manager.state.current_actor() or ""
        current_scene = app.scene_manager.current_scene
        frame = app.scene_frame_manager.current_frame
        known_npcs: list[str] = []
        established_scene_facts: list[str] = []
        immediate_scene_consequence = ""
        if frame is not None:
            if frame.last_npc_speaker:
                known_npcs.append(frame.last_npc_speaker)
            generic_npc_roles = {"知情者", "受压者", "对立者", "守门者"}
            for entry in frame.npc_functions:
                label = str(entry or "").split("：", 1)[0].strip()
                if label and label not in generic_npc_roles:
                    known_npcs.append(label)
            established_scene_facts = list(
                dict.fromkeys(
                    item
                    for item in [
                        frame.current_pressure,
                        *frame.committed_consequences[-6:],
                        *frame.public_facts[-6:],
                        *frame.revealed_clues[-4:],
                    ]
                    if str(item or "").strip()
                )
            )
            if frame.committed_consequences:
                latest_consequence = str(frame.committed_consequences[-1] or "").strip()
                pressure = str(frame.current_pressure or "").strip()
                if latest_consequence and pressure and (
                    latest_consequence.rstrip("。") in pressure
                    or pressure.rstrip("。") in latest_consequence
                ):
                    immediate_scene_consequence = latest_consequence
        pc_names = [character.name for character in pcs]
        present_pcs = [
            name
            for name in pc_names
            if current_scene is not None and name in current_scene.participants
        ]
        present_npcs: list[str] = []
        if current_scene is not None:
            for raw_name in current_scene.participants:
                name = str(raw_name or "").strip()
                if not name or name in pc_names:
                    continue
                canonical = app.world_state.resolve_npc_name(name) or name
                if canonical not in present_npcs:
                    present_npcs.append(canonical)
            for persona in app.world_state.npc_personas.values():
                if (
                    current_scene.location
                    and persona.current_location == current_scene.location
                    and persona.name not in present_npcs
                ):
                    present_npcs.append(persona.name)
        known_npcs.extend(present_npcs)

        context = LegalActionContext(
            stage_goal=step.stage_goal,
            scene_name=str(getattr(app.scene_manager.current_scene, "name", "") or ""),
            scene_location=str(getattr(app.scene_manager.current_scene, "location", "") or ""),
            current_actor=current_actor,
            conflict_active=app.conflict_manager.state.active,
            known_pcs=pc_names,
            pc_resources={
                character.name: {
                    "hp": int(character.hp),
                    "max_hp": int(character.max_hp),
                    "mp": int(character.mp),
                    "max_mp": int(character.max_mp),
                    "statuses": [
                        self._status_label(getattr(status, "value", status))
                        for status in character.statuses
                    ],
                }
                for character in pcs
            },
            known_enemies=[character.name for character in enemies],
            known_npcs=list(dict.fromkeys(known_npcs)),
            present_npcs=list(dict.fromkeys(present_npcs)),
            present_pcs=list(dict.fromkeys(present_pcs)),
            presence_authoritative=current_scene is not None,
            actor_locations={
                name: app.scene_manager.location_of(name)
                for name in pc_names
            },
            story_items=[
                {
                    "item_id": item.item_id,
                    "name": item.name,
                    "description": item.description,
                    "holder": item.holder,
                    "location": item.location,
                    "status": item.status.value,
                }
                for item in app.world_state.story_items.values()
            ],
            visible_scene_elements=list(frame.visible_elements if frame is not None else []),
            established_scene_facts=established_scene_facts,
            immediate_scene_consequence=immediate_scene_consequence,
            blocked_routes=SceneAccessBoundary().blocked_routes(
                frame=frame,
                scene=app.scene_manager.current_scene,
            ),
            active_clocks=[
                app.clock_manager.format_clock(clock)
                for clock in app.clock_manager.all()
                if int(clock.current or 0) < int(clock.max_segments or 0)
            ],
            open_npc_conditions=[
                {
                    "condition_id": str(item.get("condition_id") or ""),
                    "npc": str(item.get("npc") or ""),
                    "condition": str(item.get("condition") or ""),
                    "promised_result": str(item.get("promised_result") or ""),
                }
                for item in (frame.open_conditions if frame is not None else [])
                if str(item.get("status") or "open") == "open"
                and str(item.get("condition") or "").strip()
            ],
            settled_npc_exchanges=[
                {
                    "npc": str(item.get("npc") or ""),
                    "outcome": str(item.get("outcome") or ""),
                    "settled_terms": str(item.get("settled_terms") or ""),
                    "player_performance": str(item.get("player_performance") or "pending"),
                }
                for item in (frame.settled_exchanges[-6:] if frame is not None else [])
                if str(item.get("npc") or "").strip()
                and str(item.get("settled_terms") or "").strip()
            ],
            pending_decisions=app.interceptor.decision_window_manager.public_summary(),
        )
        if str(public_context or "").strip():
            self._restrict_to_public_context(context, public_context)

        actor = step.actor or self._speaker_character_guess(step.speaker, pcs)
        answerable_decisions = [
            decision
            for decision in context.pending_decisions
            if self._requires_player_response(decision)
            and (
                not decision.get("allowed_speakers")
                or step.speaker in decision.get("allowed_speakers", [])
                or actor in decision.get("allowed_speakers", [])
            )
        ]
        if answerable_decisions:
            context.pending_decisions = answerable_decisions
            context.legal_actions.append("回应待决窗口")
            context.notes.append("先回答属于当前玩家/角色的待决窗口；不要替其他玩家作答。")
        else:
            context.pending_decisions = []

        if step.kind.startswith("session_zero"):
            context.legal_actions = [
                "贡献世界事实",
                "提出地区或事件",
                "提出反派种子",
                "声明界限与帷幕",
                "创建或补全角色",
            ]
            context.notes.append("第零章允许共创世界，不需要消耗物语点。")
            return context

        if context.conflict_active:
            if not current_actor:
                context.legal_actions = ["等待 GM 明确当前行动者"]
            elif actor and actor != current_actor:
                context.legal_actions = ["回合外等待", "给当前行动者建议", "声明预备想法但不结算"]
                context.notes.append(f"当前行动者是 {current_actor}，{actor} 不能结算消耗回合的行动。")
            elif actor and self._is_enemy_actor(actor, enemies):
                context.legal_actions = ["等待敌方行动"]
                context.notes.append("玩家不能替敌人行动。")
            else:
                context.legal_actions = [
                    "攻击",
                    "防御",
                    "妨碍",
                    "调查",
                    "推进目标命刻",
                    "压制威胁命刻",
                    "使用库存道具",
                    "施放已掌握法术",
                ]
                actor_sheet = self._character_by_name(actor, pcs)
                if actor_sheet is not None:
                    can_pay_with_hp = bool(
                        int(dict(actor_sheet.skills or {}).get("生命秘法", 0) or 0)
                        or "生命秘法" in list(actor_sheet.abilities or [])
                    )
                    context.legal_spell_rules = self._spell_rules(
                        list(actor_sheet.spells),
                        current_mp=int(actor_sheet.mp),
                        can_pay_with_hp=can_pay_with_hp,
                    )
                    context.legal_spells = [
                        str(rule["name"])
                        for rule in context.legal_spell_rules
                        if bool(rule.get("affordable"))
                    ]
                    context.legal_spell_rules = [
                        rule
                        for rule in context.legal_spell_rules
                        if bool(rule.get("affordable"))
                    ]
                    if not context.legal_spells:
                        context.legal_actions = [
                            action
                            for action in context.legal_actions
                            if action != "施放已掌握法术"
                        ]
                    context.legal_skills = list(actor_sheet.skills)
                    context.legal_skill_rules = self._skill_rules(actor_sheet)
                self._append_pending_exchange_action(context)
            return context

        context.legal_actions = [
            "普通叙事行动",
            "调查",
            "社交交涉",
            "推进目标命刻",
            "计划或推进仪式",
            "启动或推进工程",
            "消耗物语点引入事实",
            "请求休息或幕间",
        ]
        actor_sheet = self._character_by_name(actor, pcs)
        if actor_sheet is not None:
            context.legal_spells = list(actor_sheet.spells)
            context.legal_spell_rules = self._spell_rules(context.legal_spells)
            context.legal_skills = list(actor_sheet.skills)
            context.legal_skill_rules = self._skill_rules(actor_sheet)
        self._append_pending_exchange_action(context)
        return context

    @classmethod
    def _restrict_to_public_context(
        cls,
        context: LegalActionContext,
        public_context: str,
    ) -> None:
        """Remove runtime-only facts from a synthetic player's action menu.

        The scene frame is a GM workspace, not a transcript.  It may contain a
        prepared NPC, an authoritative clue awaiting expression, or a bargain
        whose prose has not reached the group yet.  Long-run players receive
        the public dialogue separately, so this layer may retain only entities
        and constraints visibly named there.
        """

        public = " ".join(str(public_context or "").split())
        if not public:
            return

        def visible(value: object) -> bool:
            text = " ".join(str(value or "").split()).strip()
            return bool(text and text in public)

        def visible_scene_item(value: object) -> bool:
            text = " ".join(str(value or "").split()).strip()
            if visible(text):
                return True
            label = text.split("：", 1)[0].strip()
            return bool(len(label) >= 2 and label in public)

        context.known_enemies = [name for name in context.known_enemies if visible(name)]
        context.known_npcs = [name for name in context.known_npcs if visible(name)]
        context.present_npcs = [name for name in context.present_npcs if visible(name)]
        context.story_items = [
            item
            for item in context.story_items
            if visible(item.get("name"))
        ]
        context.visible_scene_elements = [
            item for item in context.visible_scene_elements if visible_scene_item(item)
        ]
        context.established_scene_facts = [
            item for item in context.established_scene_facts if visible(item)
        ]
        if not visible(context.immediate_scene_consequence):
            context.immediate_scene_consequence = ""
        context.blocked_routes = [route for route in context.blocked_routes if visible(route)]
        context.active_clocks = [
            clock
            for clock in context.active_clocks
            if any(visible(name) for name in cls._clock_names(clock))
        ]
        context.open_npc_conditions = [
            item
            for item in context.open_npc_conditions
            if visible(item.get("npc"))
            and (
                visible(item.get("condition"))
                or visible(item.get("promised_result"))
            )
        ]
        context.settled_npc_exchanges = [
            item
            for item in context.settled_npc_exchanges
            if visible(item.get("npc")) and visible(item.get("settled_terms"))
        ]

    @staticmethod
    def _clock_names(value: object) -> list[str]:
        text = str(value or "")
        names = re.findall(r"【([^】]+)】", text)
        return names or [text]

    @staticmethod
    def _append_pending_exchange_action(context: LegalActionContext) -> None:
        if any(
            str(item.get("outcome") or "").strip().lower() == "accepted"
            and str(item.get("player_performance") or "pending").strip().lower()
            != "complete"
            for item in context.settled_npc_exchanges
        ):
            context.legal_actions.append("履行已接受的NPC交换")
            context.notes.append(
                "存在NPC已接受、但玩家尚未实际履行的条款；可以直接履行、明确拒绝或承担违约后果，不能把谈妥当成交付完成。"
            )

    @staticmethod
    def _requires_player_response(decision: dict[str, object]) -> bool:
        return bool(decision.get("blocking")) or str(decision.get("kind") or "") in {
            "zero_hp",
            "critical_opportunity",
            "opportunity_parameter",
            "spell_parameter",
            "skill_judgement",
        }

    def as_prompt_block(self, context: LegalActionContext) -> str:
        data = asdict(context)
        lines = [
            "当前合法行动上下文：",
            f"- 测试目标：{data['stage_goal'] or '未指定'}",
            f"- 当前场景：{data['scene_name'] or '未命名'}",
            f"- 当前地点：{data['scene_location'] or '未记录'}",
            f"- 冲突中：{data['conflict_active']}",
            f"- 当前行动者：{data['current_actor'] or '无'}",
            f"- 已知 PC：{'、'.join(data['known_pcs']) or '无'}",
            "- 队伍公开资源："
            + (
                "；".join(
                    f"{name} HP {values.get('hp', 0)}/{values.get('max_hp', 0)}，"
                    f"MP {values.get('mp', 0)}/{values.get('max_mp', 0)}，"
                    f"异常状态 {'、'.join(values.get('statuses') or []) or '无'}"
                    for name, values in data["pc_resources"].items()
                )
                or "无"
            ),
            f"- 已知敌人：{'、'.join(data['known_enemies']) or '无'}",
            f"- 当前场景已公开 NPC：{'、'.join(data['known_npcs']) or '无'}",
            f"- 当前确实在场、可直接对话的 NPC：{'、'.join(data['present_npcs']) or '无'}",
            f"- 当前镜头中的 PC：{'、'.join(data['present_pcs']) or '无'}",
            "- PC最后确认位置："
            + (
                "；".join(
                    f"{name}={location or '未记录'}"
                    for name, location in data["actor_locations"].items()
                )
                or "无"
            ),
            "- 已公开剧情物件的权威状态（持有与放置必须以此为准）："
            + (
                "；".join(
                    f"{item.get('name')}：状态={item.get('status') or '未知'}，"
                    f"持有者={item.get('holder') or '无'}，位置={item.get('location') or '未记录'}"
                    for item in data["story_items"]
                )
                or "无"
            ),
            f"- 当前确实在场、可以直接接触的物件或景象：{'；'.join(data['visible_scene_elements']) or '无'}",
            f"- 已经发生且不可倒退的现场事实：{'；'.join(data['established_scene_facts']) or '无'}",
            f"- 刚刚兑现、必须立刻回应的现场后果：{data['immediate_scene_consequence'] or '无'}",
            f"- 尚未开放、不能穿过的道路或入口：{'、'.join(data['blocked_routes']) or '无'}",
            f"- 命刻：{'；'.join(data['active_clocks']) or '无'}",
            "- NPC已经公开、尚未满足的有限条件（承诺结果尚未发生，不能说成已经放行、开门、交出或告知）："
            + (
                "；".join(
                    f"{item.get('npc')}要求【{item.get('condition')}】，完成后才会【{item.get('promised_result') or '兑现已公开帮助'}】"
                    for item in data["open_npc_conditions"]
                )
                or "无"
            ),
            "- NPC已接受、但玩家尚未实际履行的交换（不得重谈；可履行、拒绝或承担违约后果）："
            + (
                "；".join(
                    f"{item.get('npc')}接受【{item.get('settled_terms')}】"
                    for item in data["settled_npc_exchanges"]
                    if item.get("outcome") == "accepted"
                    and item.get("player_performance") != "complete"
                )
                or "无"
            ),
            "- 已完成或已拒绝的NPC交换（不得重复确认、重新交付或换词再问）："
            + (
                "；".join(
                    f"{item.get('npc')}已{('接受并完成' if item.get('outcome') == 'accepted' else '拒绝')}"
                    f"【{item.get('settled_terms')}】"
                    for item in data["settled_npc_exchanges"]
                    if item.get("outcome") == "rejected"
                    or item.get("player_performance") == "complete"
                )
                or "无"
            ),
            f"- 可选动作：{'、'.join(data['legal_actions']) or '无'}",
        ]
        if context.legal_spells:
            lines.append(f"- 当前角色已掌握法术：{'、'.join(context.legal_spells)}")
        if context.legal_spell_rules:
            lines.append("- 已掌握法术的硬规则（只能声明这些效果）：")
            for rule in context.legal_spell_rules:
                choices: list[str] = []
                if rule.get("selectable_damage_types"):
                    choices.append("元素=" + "、".join(rule["selectable_damage_types"]))
                if rule.get("selectable_statuses"):
                    choices.append("异常=" + "、".join(rule["selectable_statuses"]))
                if rule.get("selectable_attributes"):
                    choices.append("属性=" + "、".join(rule["selectable_attributes"]))
                lines.append(
                    f"  * 【{rule['name']}】目标={rule['target_label']}；"
                    f"{'需要施法检定' if rule['requires_check'] else '不进行施法检定'}；"
                    f"{rule['description']}"
                    + (f"；必须选择：{'；'.join(choices)}" if choices else "")
                )
        if context.legal_skills:
            lines.append(f"- 当前角色已掌握技能：{'、'.join(context.legal_skills)}")
        if context.legal_skill_rules:
            lines.append("- 已掌握技能的硬规则（技能名不授权额外功能）：")
            for rule in context.legal_skill_rules:
                lines.append(f"  * 【{rule['name']}】{rule['description']}")
        if context.pending_decisions:
            lines.append("- 当前玩家可回答的待决窗口：")
            for decision in context.pending_decisions:
                options = "、".join(
                    str(option.get("effect") or option.get("label") or option.get("trait") or option.get("target") or option)
                    for option in decision.get("options", [])
                )
                lines.append(
                    f"  * {decision.get('kind')}: {decision.get('prompt') or '请选择'}"
                    + (f"；合法选项：{options}" if options else "")
                )
        if context.notes:
            lines.append("- 注意：" + "；".join(context.notes))
        return "\n".join(lines)

    @staticmethod
    def _skill_rules(character: Any) -> list[dict[str, Any]]:
        rules: list[dict[str, Any]] = []
        for raw_name in character.skills:
            reference = get_skill_reference(str(raw_name))
            if reference is None:
                continue
            coverage = skill_implementation_coverage(reference.name)
            description = reference.summary
            can_declare_as_action = not (
                coverage is not None
                and coverage.category == "passive_hard"
            )
            if not can_declare_as_action:
                description += "（被动触发，不能单独声明为一次行动。）"
            if reference.name == "便携装置":
                tiers = portable_device_tiers(
                    character.skill_options.get("便携装置", [])
                )
                features: list[str] = []
                if tiers.get("炼金装置", 0) >= 1:
                    features.append("炼金装置：消耗物资点调制规则药剂")
                infusion_tier = tiers.get("注魔装置", 0)
                if infusion_tier >= 1:
                    features.append("注魔装置：命中时消耗2物资点附加已解锁注魔")
                magitech_tier = tiers.get("魔导装置", 0)
                if magitech_tier >= 1:
                    features.append(
                        "基础魔导装置仅有魔导覆写：冲突中消耗10精神值，指定一个无心智、"
                        "正受异常状态影响的构装体或元素敌人"
                    )
                if magitech_tier >= 2:
                    features.append("进阶魔导装置另有魔法加农炮")
                if magitech_tier >= 3:
                    features.append("顶级魔导装置另有法球")
                description += " 当前已解锁：" + ("；".join(features) or "尚未选择装置类型")
                description += "。它不提供通用扫描、探测或校准能力；普通工具只能作为调查手段的叙事外观。"
            rules.append(
                {
                    "name": reference.name,
                    "description": description,
                    "can_declare_as_action": can_declare_as_action,
                    "coverage_category": (
                        str(coverage.category)
                        if coverage is not None
                        else "unknown"
                    ),
                }
            )
        return rules

    @staticmethod
    def _status_label(status: object) -> str:
        return {
            "slow": "迟缓",
            "dazed": "眩晕",
            "weakened": "虚弱",
            "shaken": "动摇",
            "enraged": "激怒",
            "poisoned": "中毒",
        }.get(str(status or ""), str(status or ""))

    def _speaker_character_guess(self, speaker: str, pcs: list[Any]) -> str:
        if not pcs:
            return ""
        if len(pcs) == 1:
            return pcs[0].name
        for character in pcs:
            if character.name and character.name in speaker:
                return character.name
        return ""

    def _character_by_name(self, name: str, characters: list[Any]) -> Any | None:
        for character in characters:
            if character.name == name:
                return character
        return None

    def _is_enemy_actor(self, actor: str, enemies: list[Any]) -> bool:
        return any(character.name == actor for character in enemies)

    @staticmethod
    def _spell_rules(
        spell_names: list[str],
        *,
        current_mp: int | None = None,
        can_pay_with_hp: bool = False,
    ) -> list[dict[str, Any]]:
        target_labels = {
            "self": "自身",
            "one_ally": "一个盟友",
            "one_enemy": "一个敌人",
            "one_creature": "一个生物",
            "up_to_three_creatures": "一至三个生物",
        }
        rules: list[dict[str, Any]] = []
        for spell_name in spell_names:
            try:
                definition = get_spell_definition(spell_name)
            except ValueError:
                continue
            target_limit = (
                3
                if definition.target.value == "up_to_three_creatures"
                else 1
            )
            if can_pay_with_hp or current_mp is None:
                max_affordable_targets = target_limit
            elif definition.mp_cost_per_target and int(definition.mp_cost or 0) > 0:
                max_affordable_targets = min(
                    target_limit,
                    max(0, int(current_mp) // int(definition.mp_cost)),
                )
            else:
                max_affordable_targets = (
                    target_limit
                    if int(current_mp) >= int(definition.mp_cost or 0)
                    else 0
                )
            rules.append(
                {
                    "name": definition.name,
                    "mp_cost": int(definition.mp_cost or 0),
                    "mp_cost_per_target": bool(definition.mp_cost_per_target),
                    "max_affordable_targets": max_affordable_targets,
                    "affordable": max_affordable_targets > 0,
                    "can_pay_with_hp": bool(can_pay_with_hp),
                    "target": definition.target.value,
                    "target_label": target_labels.get(definition.target.value, definition.target.value),
                    "effect_type": definition.effect_type.value,
                    "requires_check": bool(definition.requires_check),
                    "description": definition.description,
                    "selectable_damage_types": [
                        DAMAGE_TYPE_LABELS.get(value, value)
                        for value in definition.selectable_damage_types
                    ],
                    "selectable_statuses": [
                        STATUS_LABELS.get(value.value, value.value)
                        for value in definition.selectable_statuses
                    ],
                    "selectable_attributes": [
                        ATTRIBUTE_LABELS.get(value, value)
                        for value in definition.selectable_attributes
                    ],
                }
            )
        return rules
