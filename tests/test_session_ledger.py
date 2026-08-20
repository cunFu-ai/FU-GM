from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.session_ledger import SessionLedger
from fu_gm.app_factory import build_app
from fu_gm.models import Character, EnemyRank


def _pc(name: str, fabula: int = 0) -> Character:
    return Character(
        name=name,
        attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
        max_hp=45,
        hp=45,
        max_mp=45,
        mp=45,
        fabula_points=fabula,
        traits=["pc"],
    )


def test_ledger_counts_actual_fabula_and_ultima_spending() -> None:
    characters = CharacterManager()
    characters.add(_pc("洛岚", fabula=3))
    villain = Character(
        name="黑日将军",
        attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
        max_hp=80,
        hp=80,
        max_mp=40,
        mp=40,
        traits=["enemy", "villain"],
    )
    characters.add(villain)
    conflict = ConflictManager(characters)
    conflict.register_enemy("黑日将军", EnemyRank.VILLAIN, ultima_points=2)
    ledger = SessionLedger()
    characters.register_resource_listener(ledger.record_resource_change)
    conflict.register_ultima_spend_listener(ledger.record_ultima_spent)
    ledger.start("s1", participating_pcs=["洛岚"])

    characters.modify_resource("洛岚", "fabula_points", -2)
    characters.modify_resource("洛岚", "fabula_points", 1)
    conflict.spend_ultima_for_trait_invocation("黑日将军")

    assert ledger.fabula_spent == 2
    assert ledger.ultima_spent == 1


def test_fulfilled_promise_survives_snapshot_and_deduplicates_paraphrases() -> None:
    ledger = SessionLedger()
    ledger.start("s1")

    first = ledger.record_fulfilled_promise(
        {
            "condition_id": "gate-1",
            "npc": "白花守望会会长",
            "condition": "留下担保",
            "promised_result": "打开旧路闸门",
            "promise_key": "access_granted",
            "promise_subject": "旧路闸门",
        }
    )
    duplicate = ledger.record_fulfilled_promise(
        {
            "condition_id": "gate-2",
            "npc": "白花守望会会长",
            "condition": "再给证据",
            "promised_result": "开放旧路",
            "promise_key": "access_granted",
            "promise_subject": "旧路",
        }
    )

    assert first is duplicate
    assert len(ledger.fulfilled_promises) == 1
    restored = SessionLedger()
    restored.apply_snapshot(ledger.to_snapshot())
    assert restored.find_fulfilled_promise(
        npc="白花守望会会长",
        promise_key="access_granted",
    ) is not None


def test_resuming_same_session_does_not_repeat_session_start_fabula_award() -> None:
    app = build_app(use_llm=False)
    hero = _pc("洛岚", fabula=0)
    app.character_manager.add(hero)

    first = app.start_session_tracking("s1", participating_pcs=["洛岚"])
    app.character_manager.modify_resource("洛岚", "fabula_points", -1)
    resumed = app.start_session_tracking("s1", participating_pcs=["洛岚"])

    assert first == ["洛岚"]
    assert resumed == []
    assert app.character_manager.get("洛岚").fabula_points == 0


def test_start_scene_registers_every_participating_pc_once() -> None:
    app = build_app(use_llm=False)
    app.character_manager.add(_pc("伊莉雅", fabula=0))
    app.character_manager.add(_pc("赛璃", fabula=0))
    app.character_manager.add(
        Character(
            name="白花守望会会长",
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=30,
            mp=30,
            traits=["npc"],
        )
    )
    app.start_session_tracking("s1", participating_pcs=["伊莉雅"])

    app.start_scene(
        "风铃廊问路",
        participants=["伊莉雅", "赛璃", "白花守望会会长"],
    )

    assert app.session_ledger.participating_pcs == {"伊莉雅", "赛璃"}
    assert app.character_manager.get("伊莉雅").fabula_points == 1
    assert app.character_manager.get("赛璃").fabula_points == 1

    app.end_scene()
    app.start_scene("旧路闸门", participants=["伊莉雅", "赛璃"])

    assert app.session_ledger.participating_pcs == {"伊莉雅", "赛璃"}
    assert app.character_manager.get("伊莉雅").fabula_points == 1
    assert app.character_manager.get("赛璃").fabula_points == 1


def test_load_reconciliation_repairs_legacy_scene_participants() -> None:
    app = build_app(use_llm=False)
    app.character_manager.add(_pc("伊莉雅", fabula=1))
    app.character_manager.add(_pc("赛璃", fabula=0))
    app.character_manager.add(
        Character(
            name="失忆旅人",
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=30,
            hp=30,
            max_mp=20,
            mp=20,
            traits=["npc"],
        )
    )
    app.session_ledger.start("legacy-s1", participating_pcs=["伊莉雅"])
    app.scene_manager.start_scene(
        "风铃廊问路",
        participants=["伊莉雅", "赛璃", "失忆旅人"],
    )

    added = app.reconcile_session_participants_from_current_scene()

    assert added == ["赛璃"]
    assert app.session_ledger.participating_pcs == {"伊莉雅", "赛璃"}
    assert app.character_manager.get("赛璃").fabula_points == 1
    assert app.reconcile_session_participants_from_current_scene() == []


def test_legacy_snapshot_without_settlement_receipt_remains_loadable() -> None:
    ledger = SessionLedger()

    ledger.apply_snapshot(
        {
            "session_id": "legacy-session",
            "active": False,
            "settled": True,
            "participating_pcs": ["洛岚"],
        }
    )

    assert ledger.settled is True
    assert ledger.last_settlement_receipt == {}
