from __future__ import annotations

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.models import Affinity, Character, EncounterDesign, EncounterDifficulty, EnemyRank, EscalationStage, StatusEffect
from fu_gm.npc_design_library import (
    ATTRIBUTE_SPREADS,
    BATTLE_DESIGN_PRINCIPLES,
    BATTLE_MECHANIC_RULES,
    DAMAGE_TYPES,
    LEVEL_RELATIONSHIP_NOTES,
    NPCDesignDraft,
    RESOURCE_PRESSURE_NOTES,
    normalize_affinity,
    normalize_damage_type,
    normalize_species,
    normalize_status,
    npc_skill_rule,
)


class EncounterManager:
    """根据 GM 章节准则设计战斗遭遇，并把精英/悍将倍率写回冲突状态。"""

    def __init__(self, character_manager: CharacterManager, conflict_manager: ConflictManager) -> None:
        self.character_manager = character_manager
        self.conflict_manager = conflict_manager

    def design_encounter(
        self,
        party: list[Character] | list[str],
        *,
        difficulty: EncounterDifficulty | str = EncounterDifficulty.NORMAL,
        boss: bool = False,
        champion_value: int | None = None,
    ) -> EncounterDesign:
        party_characters = self._resolve_party(party)
        pc_count = max(1, len(party_characters))
        party_level = max((character.level for character in party_characters), default=5)
        difficulty = EncounterDifficulty.BOSS if boss else EncounterDifficulty(difficulty)
        soldier_equivalent = self.soldier_equivalent(pc_count, difficulty)
        average_hp = sum(character.max_hp for character in party_characters) // pc_count
        expected_enemy_damage = max(1, average_hp // 3)
        expected_soldier_hp = expected_enemy_damage * 3
        suggested_range = (
            f"{party_level} 到 {party_level + 5} 级较合适；"
            f"{party_level + 10} 级以内算大威胁，超过 {party_level + 10} 级通常过强。"
        )
        enemy_mix = self._enemy_mix(pc_count, soldier_equivalent, difficulty, champion_value)
        transparency_notes = [
            "敌人进入危机状态、相性被触发或发生变化时，必须告诉玩家。",
            "若敌人正在蓄力强力攻击，应在轮开始时明确预示，让玩家能选择防御或推进目标。",
            "默认可以随机选择攻击目标，以减少 GM 偏心感；关键反派可按人设和战术目标选择。",
        ]
        special_mechanics = [
            "可以用守卫、限制条件、相性变化、波次或增援制造战术层次。",
            "环境效果应允许玩家用推进目标命刻来解除或反过来利用。",
        ]
        if difficulty == EncounterDifficulty.BOSS:
            special_mechanics.append(
                "Boss 至少应是次要反派，拥有终结点；从单体强敌、多阶段、相性变化、蓄力、增援、环境命刻或多部件中选择合适机制，不要默认使用多部件。"
            )

        return EncounterDesign(
            party_level=party_level,
            pc_count=pc_count,
            difficulty=difficulty,
            soldier_equivalent=soldier_equivalent,
            suggested_enemy_level_range=suggested_range,
            expected_enemy_damage=expected_enemy_damage,
            expected_soldier_hp=expected_soldier_hp,
            enemy_mix=enemy_mix,
            battle_principles=list(BATTLE_DESIGN_PRINCIPLES),
            resource_pressure_notes=list(RESOURCE_PRESSURE_NOTES),
            level_relationship_notes=list(LEVEL_RELATIONSHIP_NOTES),
            ideal_duration_rounds="3-4",
            transparency_notes=transparency_notes,
            special_mechanics=special_mechanics,
            summary=(
                f"{pc_count} 名 PC，队伍等级 {party_level}，{difficulty.value} 遭遇建议约 "
                f"{soldier_equivalent} 个小兵等效战力。"
            ),
        )

    def soldier_equivalent(self, pc_count: int, difficulty: EncounterDifficulty | str) -> int:
        difficulty = EncounterDifficulty(difficulty)
        if difficulty == EncounterDifficulty.EASY:
            return max(1, pc_count - 1)
        if difficulty in {EncounterDifficulty.HARD, EncounterDifficulty.BOSS}:
            return pc_count + 1
        return max(1, pc_count)

    def enemy_level_relationship(self, party_level: int, enemy_level: int) -> str:
        """给 LLM/GM 一个等级关系提示；这是风险提示，不是强制禁止。"""

        if enemy_level < party_level:
            return "敌人等级低于队伍等级，可能会太弱。"
        if enemy_level <= party_level + 5:
            return "敌人等级在队伍等级 +5 以内，较为合适。"
        if enemy_level <= party_level + 10:
            return "敌人等级在队伍等级 +10 以内，算是大威胁。"
        return "敌人等级高出队伍等级 11 级以上，通常可能过强，除非有明确逃跑、谈判或机制解法。"

    def design_npc(
        self,
        name: str,
        *,
        level: int = 5,
        species: str = "humanoid",
        traits: list[str] | None = None,
        attribute_spread: str = "versatile",
        attribute_order: tuple[str, str, str, str] = ("DEX", "INS", "MIG", "WLP"),
        attribute_overrides: dict[str, int] | None = None,
        weaknesses: list[str] | None = None,
        additional_affinities: dict[str, Affinity | str] | None = None,
        status_immunities: list[StatusEffect | str] | None = None,
        armor_initiative_modifier: int = 0,
        physical_defense: int | None = None,
        magic_defense: int | None = None,
        rank: EnemyRank | str = EnemyRank.SOLDIER,
        champion_value: int = 2,
        selected_skill_names: list[str] | None = None,
    ) -> NPCDesignDraft:
        """按 GM 章节规则生成一名 NPC 的数值草案。

        这个方法只负责“骨架和安全线”：等级、物种、属性、HP/MP、技能预算、
        相性与阶级倍率。名称、招式表现、具体技能文本仍应由 LLM 根据场景改写。
        """

        level = max(5, min(60, level))
        species_rule = normalize_species(species)
        rank = EnemyRank(rank)
        attributes = self._npc_attributes(attribute_spread, attribute_order, attribute_overrides, level)
        affinities = {damage_type: Affinity.NORMAL for damage_type in DAMAGE_TYPES}
        affinities.update(species_rule.default_affinities)
        notes = list(species_rule.rules)

        weakness_skill_bonus = 0
        for damage_type in weaknesses or []:
            key = normalize_damage_type(damage_type)
            affinities[key] = Affinity.WEAK
            weakness_skill_bonus += 2 if key == "physical" else 1

        for damage_type, affinity in (additional_affinities or {}).items():
            affinities[normalize_damage_type(damage_type)] = normalize_affinity(affinity)

        immunities = list(species_rule.status_immunities)
        for status in status_immunities or []:
            normalized = normalize_status(status)
            if normalized not in immunities:
                immunities.append(normalized)

        base_skill_budget = species_rule.base_skill_count + (level // 10) + weakness_skill_bonus
        action_count = 1
        soldier_equivalent = 1
        hp_multiplier = 1
        mp_multiplier = 1
        initiative_bonus = 0
        rank_skill_bonus = 0
        rank_notes: list[str] = []
        if rank == EnemyRank.ELITE:
            hp_multiplier = 2
            action_count = 2
            soldier_equivalent = 2
            initiative_bonus = 2
            rank_skill_bonus = 1
            rank_notes.append("精英：最大 HP 翻倍、每轮 2 回合、先攻 +2，并额外选择 1 个技能。")
        elif rank == EnemyRank.CHAMPION:
            champion_value = max(2, champion_value)
            hp_multiplier = champion_value
            mp_multiplier = 2
            action_count = champion_value
            soldier_equivalent = champion_value
            initiative_bonus = champion_value
            rank_skill_bonus = champion_value
            rank_notes.append(
                f"悍将：等效 {champion_value} 名小兵，HP ×{champion_value}、MP ×2、每轮 {champion_value} 回合、先攻 +{champion_value}，并额外选择 {champion_value} 个技能。"
            )

        max_hp = (level * 2 + attributes["MIG"] * 5) * hp_multiplier
        max_mp = (level + attributes["WLP"] * 5) * mp_multiplier
        initiative = (attributes["DEX"] + attributes["INS"]) // 2 + armor_initiative_modifier + initiative_bonus
        defenses = {
            "physical": physical_defense if physical_defense is not None else attributes["DEX"],
            "magic": magic_defense if magic_defense is not None else attributes["INS"],
        }

        selected_skills = []
        for skill_name in selected_skill_names or []:
            try:
                selected_skills.append(npc_skill_rule(skill_name))
            except ValueError:
                notes.append(f"未识别的 NPC 技能“{skill_name}”已保留为自定义技能，请由 GM/LLM 审核强度。")

        if species_rule.weakness_options and not any(affinities[option] == Affinity.WEAK for option in species_rule.weakness_options):
            notes.append(f"{species_rule.name}通常应从 {', '.join(species_rule.weakness_options)} 中选择一种弱点。")

        if species_rule.can_use_equipment:
            notes.append("该物种可装备物品；装备会覆盖或修正物防、魔防、先攻和基础攻击。")
        if species_rule.cannot_use_equipment:
            notes.append("该物种通常不能使用装备；若叙事需要装备，应先给出特殊解释。")

        return NPCDesignDraft(
            name=name,
            level=level,
            species=species_rule,
            rank=rank,
            traits=traits or [],
            attributes=attributes,
            max_hp=max_hp,
            crisis_threshold=max_hp // 2,
            max_mp=max_mp,
            initiative=initiative,
            defenses=defenses,
            affinities=affinities,
            status_immunities=immunities,
            check_bonus=level // 10,
            extra_damage=self._npc_extra_damage(level),
            skill_budget=base_skill_budget + rank_skill_bonus,
            selected_skills=selected_skills,
            action_count=action_count,
            soldier_equivalent=soldier_equivalent,
            rank_notes=rank_notes,
            transparency_notes=[
                "调查行动 7+ 揭示等级、物种、最大 HP/MP；10+ 追加特质、属性、防御、相性；13+ 追加基础攻击和法术。",
                "敌人进入危机、相性变化、蓄力强攻或阶段变化时，应公开告知玩家可观察的信息。",
            ],
            notes=notes,
        )

    def battle_mechanic_suggestions(self, *, boss: bool = False, include_environment: bool = True) -> list[str]:
        """返回战斗机制参考，供 LLM 按场景挑选一两个使用。"""

        suggestions = []
        for rule in BATTLE_MECHANIC_RULES:
            if rule.category == "boss" and not boss:
                continue
            if rule.category == "environment" and not include_environment:
                continue
            suggestions.append(f"{rule.name}：{rule.summary} {rule.gm_guidance}")
        return suggestions

    def apply_rank_template(
        self,
        enemy_name: str,
        rank: EnemyRank | str,
        *,
        champion_value: int = 2,
        ultima_points: int = 0,
        is_villain: bool = False,
    ) -> Character:
        """将小兵模板升阶为精英或悍将，并注册每轮行动次数。"""

        rank = EnemyRank(rank)
        enemy = self.character_manager.get(enemy_name)
        action_count = 1
        hp_multiplier = 1
        initiative_bonus = 0
        if rank == EnemyRank.ELITE:
            hp_multiplier = 2
            action_count = 2
            initiative_bonus = 2
        elif rank == EnemyRank.CHAMPION:
            champion_value = max(2, champion_value)
            hp_multiplier = champion_value
            action_count = champion_value
            initiative_bonus = champion_value
            enemy.max_mp *= 2
            enemy.mp = min(enemy.max_mp, enemy.mp * 2)

        if hp_multiplier > 1:
            enemy.max_hp *= hp_multiplier
            enemy.hp = min(enemy.max_hp, enemy.hp * hp_multiplier)
            enemy.crisis_threshold = enemy.max_hp // 2
        enemy.initiative += initiative_bonus
        if is_villain and "villain" not in enemy.traits:
            enemy.traits.append("villain")

        self.conflict_manager.register_enemy(
            enemy_name,
            rank,
            ultima_points=ultima_points,
            action_count=action_count,
        )
        return enemy

    def multipart_boss_suggestion(self, boss_name: str, pc_count: int) -> list[str]:
        """仅在 Boss 概念适合“巨大身躯/载具/构装体/群体意识”时使用。"""

        body_value = max(2, pc_count - 1)
        return [
            "这是可选方案：只有当 Boss 概念适合多部件时才采用。",
            f"【{boss_name}躯干】作为悍将 {body_value}，保留主要 HP、终结点和阶段变化。",
            "设计 2-4 个【肢体】作为小兵：防御性肢体可保护躯干，施法肢体可释放攻击/防御法术。",
            "躯干可消耗 1 点终结点再生一个被破坏的肢体，让 Boss 战更有 JRPG 多阶段感。",
        ]

    def boss_stage_templates(
        self,
        boss_name: str,
        *,
        theme: str = "overdrive",
        champion_value: int = 3,
    ) -> list[EscalationStage]:
        """生成可直接传给 `ConflictManager.register_enemy` 的 Boss 阶段素材。

        这些素材不是“写死剧情”，而是给 GM/LLM 一个可改写的战术骨架：升格时
        可以刷新终结点、改变相性或行动次数，并把清晰的公开预兆交给玩家。
        调用方应该按 Boss 概念选择一种主题；不要把多部件当成默认结构。
        """

        theme = theme.strip().lower()
        if theme in {"barrier", "shield", "守护", "护盾", "壁垒"}:
            return [
                EscalationStage(
                    name="二阶段·封闭核心",
                    ultima_points=10,
                    hp_restore=None,
                    mp_restore=None,
                    affinity_changes={"physical": Affinity.RESIST, "lightning": Affinity.WEAK},
                    action_count=max(2, champion_value),
                    preferred_actions=["Guard", "Objective", "Hinder"],
                    tactic_hints=[
                        "优先推进护盾或仪式命刻，逼迫英雄分散行动。",
                        "公开提示物理抗性和雷系弱点，让玩家能制定战术。",
                    ],
                    public_cue=f"{boss_name} 的外壳闭合成巨大的魔导盾，核心缝隙中却泄出雷光。",
                    note="适合机关 Boss、防御型肢体或需要玩家拆机制的战斗。",
                )
            ]
        if theme in {"summon", "parts", "multipart", "召唤", "多部件", "肢体"}:
            return [
                EscalationStage(
                    name="二阶段·裂解多部件",
                    ultima_points=10,
                    hp_restore=None,
                    mp_restore=None,
                    action_count=max(2, champion_value),
                    preferred_actions=["Objective", "Attack", "Guard"],
                    tactic_hints=[
                        "用目标行动再生或激活肢体；若没有肢体，就推进威胁命刻。",
                        "让防御性部件保护本体，施法部件制造异常状态。",
                    ],
                    public_cue=f"{boss_name} 的身躯裂解，数个独立部件开始以不同节奏行动。",
                    note="适合多部件首领，鼓励玩家逐步拆解 Boss。",
                )
            ]
        if theme in {"phase_shift", "affinity", "相性", "换相"}:
            return [
                EscalationStage(
                    name="二阶段·相性反转",
                    ultima_points=10,
                    hp_restore=None,
                    mp_restore=None,
                    affinity_changes={"fire": Affinity.RESIST, "ice": Affinity.WEAK, "light": Affinity.RESIST, "dark": Affinity.WEAK},
                    action_count=max(2, champion_value),
                    preferred_actions=["Spell", "Hinder", "Attack"],
                    tactic_hints=[
                        "阶段开始时公开相性变化；之后用法术和异常状态迫使英雄换打法。",
                        "如果玩家识破规律，不要强行否定，改用命刻制造压力。",
                    ],
                    public_cue=f"{boss_name} 周围的元素轮盘倒转，火与光被吞入外壳，冰与暗成为裂隙。",
                    note="适合元素 Boss 或谜题型首领。",
                )
            ]
        return [
            EscalationStage(
                name="二阶段·过载暴走",
                ultima_points=10,
                hp_restore=None,
                mp_restore=None,
                added_statuses=[StatusEffect.ENRAGED],
                action_count=max(2, champion_value),
                preferred_actions=["Attack", "Spell", "Objective"],
                tactic_hints=[
                    "危机后加强进攻，但必须预示强力攻击，让玩家有防御或打断窗口。",
                    "若有威胁命刻，过载阶段应更积极推进它。",
                ],
                public_cue=f"{boss_name} 的核心过载，空气像被赤红色的钟声震碎。",
                note="默认 JRPG 二阶段模板，适合大多数 Boss。",
            )
        ]

    def _resolve_party(self, party: list[Character] | list[str]) -> list[Character]:
        resolved: list[Character] = []
        for item in party:
            if isinstance(item, Character):
                resolved.append(item)
            elif self.character_manager.exists(item):
                resolved.append(self.character_manager.get(item))
        return resolved

    def _enemy_mix(
        self,
        pc_count: int,
        soldier_equivalent: int,
        difficulty: EncounterDifficulty,
        champion_value: int | None,
    ) -> list[str]:
        if difficulty == EncounterDifficulty.BOSS:
            value = champion_value or max(2, soldier_equivalent)
            return [
                f"1 名悍将（等效 {value} 名小兵）",
                "或 1 名单体 Boss + 环境命刻/蓄力/相性变化等机制",
                "或在概念适合时使用多部件 Boss：躯干为精英/悍将，肢体按小兵处理",
            ]
        if soldier_equivalent == 2:
            return ["1 名精英", "或 2 名小兵"]
        if soldier_equivalent > pc_count:
            return [f"{soldier_equivalent} 名小兵", "或 1 名精英 + 若干小兵"]
        return [f"{soldier_equivalent} 名小兵"]

    def _npc_attributes(
        self,
        attribute_spread: str,
        attribute_order: tuple[str, str, str, str],
        attribute_overrides: dict[str, int] | None,
        level: int,
    ) -> dict[str, int]:
        if attribute_spread not in ATTRIBUTE_SPREADS:
            raise ValueError(f"未知属性分配方式：{attribute_spread}")
        values = ATTRIBUTE_SPREADS[attribute_spread]
        if sorted(attribute_order) != ["DEX", "INS", "MIG", "WLP"]:
            raise ValueError("attribute_order 必须正好包含 DEX、INS、MIG、WLP。")
        attributes = {attribute: values[index] for index, attribute in enumerate(attribute_order)}
        for key, value in (attribute_overrides or {}).items():
            if key not in attributes:
                raise ValueError(f"未知属性：{key}")
            attributes[key] = max(6, min(12, value))

        if not attribute_overrides:
            boost_count = sum(1 for threshold in (20, 40, 60) if level >= threshold)
            for _ in range(boost_count):
                target = min(attributes, key=lambda attr: (attributes[attr], attr))
                attributes[target] = self._increase_die(attributes[target])
        return attributes

    def _increase_die(self, die_size: int) -> int:
        if die_size < 8:
            return 8
        if die_size < 10:
            return 10
        if die_size < 12:
            return 12
        return 12

    def _npc_extra_damage(self, level: int) -> int:
        if level >= 60:
            return 15
        if level >= 40:
            return 10
        if level >= 20:
            return 5
        return 0
