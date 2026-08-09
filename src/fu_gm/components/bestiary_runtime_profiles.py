from __future__ import annotations

import copy

from fu_gm.core_bestiary import DAMAGE_ORDER
from fu_gm.models import (
    Affinity,
    EffectTiming,
    NPCAbilityProfile,
    NPCAttackEffect,
    StatusEffect,
)


def attack_options_for_bestiary(
    template_name: str,
    attack_name: str,
) -> dict[str, object]:
    """Return non-numeric choices printed on one core-bestiary attack.

    These options cannot be recovered safely by searching prose at runtime.
    Keeping the small table beside the typed trait profiles lets inherited and
    reskinned creatures preserve the original choice/randomization rules.
    """

    return copy.deepcopy(
        _ATTACK_OPTIONS.get(
            (str(template_name or "").strip(), str(attack_name or "").strip()),
            {},
        )
    )


def attack_rules_for_bestiary(
    template_name: str,
    attack_name: str,
) -> dict[str, object]:
    """Return deterministic hit/condition rules for one printed attack."""

    return copy.deepcopy(
        _ATTACK_RULES.get(
            (str(template_name or "").strip(), str(attack_name or "").strip()),
            {},
        )
    )


def ability_profiles_for_bestiary(template_name: str) -> list[NPCAbilityProfile]:
    """Compile executable abilities that are explicit on core bestiary cards.

    The bestiary text remains the source shown to the GM.  These profiles are
    the small deterministic subset needed by the combat runtime; inheritance
    and reskinning therefore preserve mechanics without checking an NPC's
    public name during play.
    """

    profiles = _PROFILES.get(str(template_name or "").strip(), ())
    return copy.deepcopy(list(profiles))


_PROFILES: dict[str, tuple[NPCAbilityProfile, ...]] = {
    "巨齿百足虫": (
        NPCAbilityProfile(
            ability_id="bestiary:giant-centipede:curl-up",
            name="蜷缩",
            source_skill="特殊行动",
            trigger="after_guard",
            effect_type="affinity_change",
            target_scope="self",
            affinity_changes={"physical": Affinity.IMMUNE},
            expires_on=EffectTiming.OWNER_TURN_START,
            description="执行防御行动后，对物理伤害免疫直到下个回合开始。",
        ),
    ),
    "硕鼠": (
        NPCAbilityProfile(
            ability_id="bestiary:dire-rat:cornered",
            name="鼠急跳墙",
            source_skill="危机效果",
            trigger="while_in_crisis",
            effect_type="check_bonus",
            amount=3,
            description="处于危机状态时，所有检定获得+3修正值。",
        ),
        NPCAbilityProfile(
            ability_id="bestiary:dire-rat:swift",
            name="疾速",
            source_skill="特殊能力",
            trigger="clock_change",
            effect_type="clock_extra_segments",
            amount=1,
            keywords=["逃跑", "逃脱", "追击", "追逐"],
            description="影响逃跑或追击命刻时，额外填充或擦除1格。",
        ),
    ),
    "日光熊": (
        NPCAbilityProfile(
            ability_id="bestiary:sun-bear:solar-body",
            name="阳炎之体",
            source_skill="危机效果",
            trigger="while_in_crisis",
            effect_type="modify_attack",
            amount=5,
            damage_type="fire",
            attack_name="熊抱",
            description="危机状态下熊抱额外造成5点伤害并变为火系。",
        ),
    ),
    "轰炮蚁": (
        NPCAbilityProfile(
            ability_id="bestiary:bombardier-ant:burrow",
            name="掘地",
            source_skill="特殊能力",
            trigger="after_guard",
            effect_type="terrain_guard",
            target_scope="self",
            amount=2,
            affinity_changes={"earth": Affinity.WEAK},
            keywords=["泥地", "沙地", "岩石"],
            expires_on=EffectTiming.OWNER_TURN_START,
            description="在泥地、沙地或岩石上防御时物防+2，并获得土系弱点直到下回合开始。",
        ),
    ),
    "白嚎怪": (
        NPCAbilityProfile(
            ability_id="bestiary:white-howler:majestic-aura",
            name="威严光环",
            source_skill="特殊能力",
            trigger="while_present",
            effect_type="status_immunity_aura",
            target_scope="allies",
            statuses=[StatusEffect.SHAKEN],
            description="在场且仍能战斗时，其盟友免疫动摇。",
        ),
    ),
    "灰嚎怪": (
        NPCAbilityProfile(
            ability_id="bestiary:grey-howler:absolute-loyalty",
            name="绝对忠诚",
            source_skill="特殊能力",
            trigger="ally_in_danger",
            effect_type="interpose",
            target_scope="ally",
            description="可代替另一个遇险生物承受该效果。",
        ),
    ),
    "魔导机兵": (
        NPCAbilityProfile(
            ability_id="bestiary:magitek-soldier:exposed-core-affinity",
            name="核心暴露",
            source_skill="危机效果",
            trigger="while_in_crisis",
            effect_type="affinity_change",
            affinity_changes={
                "fire": Affinity.NORMAL,
                "ice": Affinity.NORMAL,
            },
            description="危机状态下失去火系与冰系抵抗相性。",
        ),
        NPCAbilityProfile(
            ability_id="bestiary:magitek-soldier:exposed-core-attack",
            name="核心暴露",
            source_skill="危机效果",
            trigger="while_in_crisis",
            effect_type="modify_attack",
            damage_type="lightning",
            attack_name="战斧劈砍",
            description="危机状态下战斧劈砍变为雷系伤害。",
        ),
    ),
    "强盗": (
        NPCAbilityProfile(
            ability_id="bestiary:bandit:bad-temper",
            name="暴躁脾气",
            source_skill="危机效果",
            trigger="enter_crisis",
            effect_type="clear_statuses",
            statuses=[
                StatusEffect.SLOW,
                StatusEffect.DAZED,
                StatusEffect.WEAKENED,
                StatusEffect.SHAKEN,
                StatusEffect.POISONED,
            ],
            once_per_scene=True,
            description="每场景首次进入危机时，解除除激怒外的异常状态。",
        ),
    ),
    "守卫": (
        NPCAbilityProfile(
            ability_id="bestiary:guard:defensive-formation",
            name="防御阵形",
            source_skill="特殊能力",
            trigger="while_ally_present",
            effect_type="defense_bonus",
            target_scope="self",
            amount=1,
            keywords=["守卫"],
            description="冲突中另有一名守卫时，物防与魔防均+1。",
        ),
        NPCAbilityProfile(
            ability_id="bestiary:guard:constant-vigilance",
            name="时刻警戒",
            source_skill="特殊能力",
            trigger="ally_in_danger",
            effect_type="interpose",
            target_scope="ally",
            description="可代替另一个遇险生物承受该效果。",
        ),
    ),
    "蛇足女妖": (
        NPCAbilityProfile(
            ability_id="bestiary:lamia:silver-tongue",
            name="舌灿莲花",
            source_skill="专精",
            trigger="check_context",
            effect_type="check_bonus",
            amount=3,
            keywords=["交涉", "谈判", "说服", "欺骗", "威胁"],
            description="与交涉相关的对抗检定获得+3修正值。",
        ),
    ),
    "爆炎元素": (
        NPCAbilityProfile(
            ability_id="bestiary:blazing-elemental:detonate",
            name="引爆",
            source_skill="最后一搏",
            trigger="zero_hp",
            effect_type="fixed_damage",
            target_scope="all_other_creatures",
            amount=10,
            damage_type="fire",
            blocked_by_damage_types=["ice"],
            once_per_scene=True,
            description="生命值归零时对场景内所有其他生物造成10点火系伤害；冰系致命伤会阻止引爆。",
        ),
    ),
    "木乃伊": (
        NPCAbilityProfile(
            ability_id="bestiary:mummy:ancient-curse",
            name="古老诅咒",
            source_skill="最后一搏",
            trigger="zero_hp",
            effect_type="status_apply",
            target_scope="all_living_creatures",
            statuses=[StatusEffect.SHAKEN, StatusEffect.WEAKENED],
            once_per_scene=True,
            description="生命值归零时，对场景内所有活物施加动摇和虚弱。",
        ),
    ),
    "电光轮": (
        NPCAbilityProfile(
            ability_id="bestiary:lightning-wheel:swift",
            name="疾速",
            source_skill="特殊能力",
            trigger="clock_change",
            effect_type="clock_extra_segments",
            amount=1,
            keywords=["逃跑", "逃脱", "追击", "追逐"],
            description="影响逃跑或追击命刻时，额外填充或擦除1格。",
        ),
    ),
    "幻菇人": (
        NPCAbilityProfile(
            ability_id="bestiary:fantasy-fungus:broad-cap",
            name="宽帽子",
            source_skill="特殊行动",
            trigger="after_guard",
            effect_type="affinity_change",
            target_scope="triggering_actor",
            affinity_changes={
                damage_type: Affinity.RESIST for damage_type in DAMAGE_ORDER
            },
            expires_on=EffectTiming.OWNER_TURN_START,
            description="防御时选择的另一个生物对所有伤害获得抵抗，直到幻菇人的下个回合开始。",
        ),
    ),
}


_ATTACK_OPTIONS: dict[tuple[str, str], dict[str, object]] = {
    ("魔法提灯", "元素释放"): {
        "random_damage_types": [
            "lightning",
            "lightning",
            "fire",
            "fire",
            "ice",
            "ice",
        ],
    },
    ("猫妖", "鬼火"): {
        "damage_type_options": ["fire", "ice"],
    },
    ("宁芙", "四季之触"): {
        "status_options_on_hit": [
            StatusEffect.DAZED,
            StatusEffect.WEAKENED,
            StatusEffect.SLOW,
            StatusEffect.SHAKEN,
        ],
    },
    ("狙击手", "狙击弓"): {
        "status_options_on_hit": [StatusEffect.DAZED, StatusEffect.SLOW],
    },
}


_ATTACK_RULES: dict[tuple[str, str], dict[str, object]] = {
    ("巨齿百足虫", "曲面切割"): {"bonus_if_previous_guard": 5},
    ("吸血蝙蝠", "吸血"): {"recover_hp_fraction": 0.5},
    ("碎响小丑", "小丑飞踢"): {
        "conditional_damage_bonus": 5,
        "conditional_target_statuses": [
            StatusEffect.DAZED,
            StatusEffect.SHAKEN,
        ],
    },
    ("橡实妖精", "犀利针刺"): {
        "conditional_damage_bonus": 5,
        "conditional_target_statuses": [StatusEffect.SLOW],
    },
    ("强盗", "蛮力肘击"): {"target_mp_loss": 10},
    ("狗头人斥候", "钢匕首"): {
        "conditional_damage_bonus": 5,
        "conditional_any_target_status": True,
    },
    ("浮空水母", "酸蚀之触"): {"target_ip_loss": 1},
    ("拟形怪", "偷取物品"): {"target_ip_loss": 2},
    ("曼德拉草", "藤蔓拍击"): {
        "conditional_damage_bonus": 5,
        "conditional_target_statuses": [StatusEffect.SHAKEN],
    },
    ("骷髅法师", "法杖"): {"recover_mp_on_hit": 5},
    ("岩躯野猪", "巨岩暴冲"): {"self_hp_loss_if_all_miss": 20},
    ("锋翼鸟", "锋翼俯冲"): {
        "effects": [
            NPCAttackEffect(
                effect_type="suppress_trait",
                trigger="after_attack",
                target_scope="self",
                trait="飞行",
                expires_on=EffectTiming.OWNER_TURN_START,
                note="攻击结算后失去飞行增益，直到下回合开始。",
            )
        ]
    },
    ("蛇足女妖", "冰冷凝视"): {
        "effects": [
            NPCAttackEffect(
                effect_type="action_restriction",
                action_types=["Objective"],
                expires_on=EffectTiming.OWNER_TURN_END,
                note="目标在其下回合无法执行推进目标行动。",
            )
        ]
    },
    ("爆炎元素", "火焰射流"): {
        "effects": [
            NPCAttackEffect(
                effect_type="suppress_resistance",
                damage_type="fire",
                expires_on=EffectTiming.OWNER_TURN_END,
                note="目标失去火系抵抗，直到爆炎元素下回合结束。",
            )
        ]
    },
    ("魔眼", "混乱凝视"): {
        "effects": [
            NPCAttackEffect(
                effect_type="action_penalty",
                required_status=StatusEffect.DAZED,
                amount=1,
                note="若目标已眩晕，其下回合少执行一次行动。",
            )
        ]
    },
    ("缠根藤", "腐化藤蔓"): {
        "effects": [
            NPCAttackEffect(
                effect_type="action_restriction_while_status",
                required_status=StatusEffect.WEAKENED,
                action_types=["Guard"],
                expires_on=EffectTiming.SCENE_END,
                note="目标虚弱期间无法执行防御行动。",
            )
        ]
    },
    ("木乃伊", "古墓利爪"): {
        "effects": [
            NPCAttackEffect(
                effect_type="affinity_while_status",
                damage_types=list(DAMAGE_ORDER),
                affinity=Affinity.WEAK,
                required_status=StatusEffect.SLOW,
                expires_on=EffectTiming.SCENE_END,
                note="目标迟缓解除前，对所有伤害类型处于弱点状态。",
            )
        ]
    },
    ("鸡蛇怪", "石化啄击"): {
        "effects": [
            NPCAttackEffect(
                effect_type="reactive_check",
                required_status=StatusEffect.SLOW,
                required_status_before_hit=True,
                check_attributes=["MIG", "WLP"],
                target_number=10,
                trait="petrified",
                note="若目标在命中前已处于迟缓状态，须通过难度等级10的【力量+意志】检定，否则石化。",
            )
        ]
    },
    ("陷龙花", "吞龙巨口"): {
        "effects": [
            NPCAttackEffect(
                effect_type="swallow",
                required_status=StatusEffect.WEAKENED,
                required_status_before_hit=True,
                amount=20,
                damage_type="physical",
                clock_segments=4,
                note="若目标在命中前已处于虚弱状态，则将其吞噬。",
            )
        ]
    },
}


__all__ = [
    "ability_profiles_for_bestiary",
    "attack_options_for_bestiary",
    "attack_rules_for_bestiary",
]
