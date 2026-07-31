from fu_gm.components.ally_npc_manager import AllyNPCManager
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.chapter_manager import ChapterManager
from fu_gm.components.economy_manager import EconomyManager
from fu_gm.components.hero_log_manager import HeroLogManager
from fu_gm.components.progression_manager import ProgressionManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.world_state import WorldState
from fu_gm.models import Character


def _hero(name: str) -> Character:
    return Character(
        name=name,
        attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
        max_hp=45,
        hp=45,
        max_mp=45,
        mp=45,
        traits=["pc"],
    )


def test_chapter_settlement_writes_per_hero_logs_and_duplicate_warning() -> None:
    characters = CharacterManager()
    characters.add(_hero("伊莉雅"))
    characters.add(_hero("洛岚"))
    world = WorldState()
    hero_logs = HeroLogManager()
    chapter = ChapterManager(
        ProgressionManager(characters, world),
        EconomyManager(characters, world, RulesEngine(seed=1)),
        world,
        hero_log_manager=hero_logs,
    )

    chapter.settle_chapter(
        chapter_title="白花碑驿站",
        participating_pcs=["伊莉雅", "洛岚"],
        party_level=5,
        difficulty="normal",
    )
    chapter.settle_chapter(
        chapter_title="白花碑驿站",
        participating_pcs=["伊莉雅"],
        party_level=5,
        difficulty="easy",
    )

    payload = hero_logs.audit_payload()
    assert len(payload["entries"]) == 3
    assert payload["entries"][0]["zenit_awarded"] > 0
    assert any("不应重复领取奖励" in warning for warning in payload["warnings"])


def test_rare_item_approval_snapshot_roundtrip() -> None:
    manager = HeroLogManager()
    approval = manager.request_rare_item_approval(
        item_name="白钟钥匙",
        requester="洛岚",
        item_type="artifact",
        source="项目制作",
        effects=["可在白钟相关仪式中作为材料"],
    )
    manager.approve_rare_item(approval.request_id, note="符合当前章节奖励强度。")

    restored = HeroLogManager()
    restored.apply_snapshot(manager.to_snapshot())

    payload = restored.audit_payload()
    assert payload["rare_item_approvals"][0]["status"] == "approved"
    assert "白钟钥匙" in payload["rare_item_approvals"][0]["item_name"]


def test_rare_item_design_approval_keeps_core_design_audit() -> None:
    characters = CharacterManager()
    world = WorldState()
    economy = EconomyManager(characters, world, RulesEngine(seed=1))
    design = economy.design_rare_weapon(
        "白钟枪剑",
        "青铜剑",
        damage_type="light",
        quality_names=["穿透"],
        description="以青铜剑模板换皮成枪剑。",
    )
    manager = HeroLogManager()

    approval = manager.request_rare_item_design_approval(
        design,
        requester="洛岚",
        source="白花碑驿站章节奖励",
        notes=["适合和白钟遗迹主题绑定。"],
    )

    payload = manager.audit_payload()
    stored = payload["rare_item_approvals"][0]
    assert approval.price == design.price
    assert stored["item_name"] == "白钟枪剑"
    assert stored["source"] == "白花碑驿站章节奖励"
    assert any("穿透" in effect for effect in stored["effects"])
    assert any("基础模板：青铜剑" in note for note in stored["notes"])
    assert any("参考价格" in note for note in stored["notes"])
    joined_notes = "\n".join(stored["notes"])
    assert "命中【敏捷+力量】+1" in joined_notes
    assert "【高值+6】光" in joined_notes
    assert "单手；近战" in joined_notes
    assert "DEX" not in joined_notes
    assert "MIG" not in joined_notes
    assert "light" not in joined_notes
    assert "melee" not in joined_notes


def test_chapter_run_scaffold_tracks_beats_and_roundtrips() -> None:
    manager = HeroLogManager()
    run = manager.start_chapter_run(
        chapter_title="白花碑驿站",
        participants=["伊莉雅", "洛岚"],
        synopsis="护送失忆旅人穿过旧路。",
        shared_creation_slots=["旧路由谁守护", "财团为什么追来"],
        iconic_elements=["白花风铃"],
    )
    manager.record_chapter_beat(
        "白花碑驿站",
        title="风铃廊谈判",
        beat_type="scene",
        status="done",
        summary="守望会愿意开旧路，但不替队伍背锅。",
    )

    restored = HeroLogManager()
    restored.apply_snapshot(manager.to_snapshot())
    payload = restored.audit_payload()

    assert run.status == "running"
    assert payload["chapter_runs"][0]["shared_creation_slots"] == ["旧路由谁守护", "财团为什么追来"]
    assert "标志性元素" in payload["chapter_runs"][0]["warnings"][0]
    assert payload["chapter_runs"][0]["beats"][-1]["title"] == "风铃廊谈判"
    assert restored.chapter_runs[0].beats[-1].status == "done"


def test_ally_npc_triggers_without_full_turn_state() -> None:
    allies = AllyNPCManager()
    allies.register_ally("钟匠阿瑟", role="知情者", scene="白花碑驿站")
    allies.add_ability(
        "钟匠阿瑟",
        name="旧钟调律",
        timing="round_end",
        description="让一枚与钟声相关的威胁命刻暂停自动推进一次。",
        mechanical_hint="可作为 GM 裁定：本轮末一个钟声命刻不自动推进。",
        uses_remaining=1,
        public_cue="阿瑟把手按在旧钟齿轮上",
    )

    first = allies.trigger("round_end", scene="白花碑驿站", context="财团巡逻逼近")
    second = allies.trigger("round_end", scene="白花碑驿站")

    assert len(first) == 1
    assert "旧钟调律" in first[0].summary
    assert second == []

    restored = AllyNPCManager()
    restored.apply_snapshot(allies.to_snapshot())
    assert restored.audit_payload()["allies"][0]["abilities"][0]["uses_remaining"] == 0


def test_ally_npc_preset_library_adds_common_support_windows() -> None:
    allies = AllyNPCManager()
    allies.register_ally("白花钟匠", role="支援者", scene="白花碑驿站")
    ability = allies.add_preset_ability("白花钟匠", "第二次机会")

    results = allies.trigger("pc_zero_hp", scene="白花碑驿站", context="伊莉雅即将倒下")

    assert ability.uses_remaining == 0
    assert len(results) == 1
    assert results[0].ability_name == "第二次机会"
    assert "保留 1 HP" in results[0].mechanical_hint
    assert "survive" in allies.audit_payload()["allies"][0]["abilities"][0]["tags"]
