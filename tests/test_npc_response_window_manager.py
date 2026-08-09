from types import SimpleNamespace

from fu_gm.components.npc_response_window_manager import NPCResponseWindowManager


def frame() -> SimpleNamespace:
    return SimpleNamespace(
        scene_key="scene-1",
        pending_npc_questions=[],
        open_conditions=[],
    )


def test_typed_request_opens_without_reading_public_prose() -> None:
    current = frame()

    request = NPCResponseWindowManager.open_request(
        current,
        npc="白花守望会会长",
        summary="选择由谁护送旅人，并说明落脚点",
        required_items=[
            {"item_id": "escort", "prompt": "护送者"},
            {"item_id": "destination", "prompt": "落脚点"},
        ],
        addressed_actor="伊莉雅",
    )

    assert request is not None
    assert request["source"] == "typed_npc_decision"
    assert NPCResponseWindowManager.remaining_items(request) == [
        {"item_id": "escort", "prompt": "护送者"},
        {"item_id": "destination", "prompt": "落脚点"},
    ]


def test_identical_open_request_is_deduplicated_structurally() -> None:
    current = frame()
    first = NPCResponseWindowManager.open_request(
        current,
        npc="白花守望会会长",
        summary="说明护送安排",
        required_items=[{"item_id": "escort", "prompt": "护送者"}],
        addressed_actor="",
    )
    second = NPCResponseWindowManager.open_request(
        current,
        npc="白花守望会会长",
        summary="换一种说法也不应另开窗口",
        required_items=[{"item_id": "escort", "prompt": "护送者"}],
        addressed_actor="",
    )

    assert first is second
    assert len(current.pending_npc_questions) == 1


def test_conflict_supersedes_open_dialogue_requests_but_keeps_history() -> None:
    current = frame()
    scene = SimpleNamespace(pending_npc_questions=[])
    request = NPCResponseWindowManager.open_request(
        current,
        npc="白花守望会会长",
        summary="说明护送安排",
        required_items=[{"item_id": "escort", "prompt": "护送者"}],
        scene=scene,
    )
    assert request is not None

    superseded = NPCResponseWindowManager.supersede_for_conflict(
        current,
        scene=scene,
    )

    assert superseded == [request["question_id"]]
    assert request["status"] == "superseded"
    assert request["resolution_kind"] == "conflict_started"
    assert NPCResponseWindowManager.pending(current) == []
    assert scene.pending_npc_questions == []


def test_partial_then_complete_response_uses_exact_parts() -> None:
    current = frame()
    request = NPCResponseWindowManager.open_request(
        current,
        npc="白花守望会会长",
        summary="说明安排",
        required_items=[
            {"item_id": "escort", "prompt": "护送者"},
            {"item_id": "destination", "prompt": "落脚点"},
        ],
    )
    assert request is not None

    partial = NPCResponseWindowManager.record_player_response(
        current,
        question_id=request["question_id"],
        actor="伊莉雅",
        response_items=[{"item_id": "escort", "kind": "answer"}],
        evidence="由我护送。",
    )
    assert partial is not None
    assert partial["complete"] is False
    assert NPCResponseWindowManager.remaining_items(request) == [
        {"item_id": "destination", "prompt": "落脚点"}
    ]

    complete = NPCResponseWindowManager.record_player_response(
        current,
        question_id=request["question_id"],
        actor="伊莉雅",
        response_items=[{"item_id": "destination", "kind": "cannot_answer"}],
        evidence="落脚点还没有决定。",
    )
    assert complete is not None
    assert complete["complete"] is True
    assert request["status"] == "resolved"
    assert NPCResponseWindowManager.response_items(request["response_items"]) == [
        {"item_id": "escort", "kind": "answer"},
        {"item_id": "destination", "kind": "cannot_answer"},
    ]


def test_wrong_actor_or_unknown_part_is_rejected() -> None:
    current = frame()
    request = NPCResponseWindowManager.open_request(
        current,
        npc="白花守望会会长",
        summary="由伊莉雅说明护送者",
        required_items=[{"item_id": "escort", "prompt": "护送者"}],
        addressed_actor="伊莉雅",
    )
    assert request is not None

    assert NPCResponseWindowManager.record_player_response(
        current,
        question_id=request["question_id"],
        actor="赛璃",
        response_items=[{"item_id": "escort", "kind": "answer"}],
    ) is None
    assert NPCResponseWindowManager.record_player_response(
        current,
        question_id=request["question_id"],
        actor="伊莉雅",
        response_items=[{"item_id": "unknown", "kind": "answer"}],
    ) is None
    assert request["status"] == "open"


def test_linked_condition_resolution_closes_the_exact_request() -> None:
    current = frame()
    current.open_conditions.append(
        {
            "condition_id": "condition-1",
            "npc": "白花守望会会长",
            "status": "open",
        }
    )
    request = NPCResponseWindowManager.open_request(
        current,
        npc="白花守望会会长",
        summary="证明有安全落脚点",
        required_items=[{"item_id": "destination", "prompt": "落脚点"}],
    )
    assert request is not None
    assert NPCResponseWindowManager.link_condition(
        current,
        question_id=request["question_id"],
        condition_id="condition-1",
    )

    updates = NPCResponseWindowManager.resolve_linked_condition_request(
        current,
        condition_id="condition-1",
        npc="白花守望会会长",
        actor="洛岚",
        public_evidence="洛岚出示了已登记的落脚凭证。",
    )

    assert updates == [
        {
            "question_id": request["question_id"],
            "answered_item_ids": ["destination"],
            "complete": True,
            "resolution_kind": "linked_condition_fulfilled",
        }
    ]
    assert request["status"] == "resolved"


def test_legacy_save_is_migrated_once_and_old_text_keys_are_removed() -> None:
    current = frame()
    record = {
        "question_id": "legacy-question",
        "npc": "白花守望会会长",
        "addressed_actor": "",
        "kind": "player_response",
        "summary": "旧存档问题",
        "required_parts": '["护送者", "落脚点"]',
        "answered_parts": '["护送者"]',
        "response_items": '[{"part":"护送者","kind":"answer"}]',
        "status": "open",
    }
    current.pending_npc_questions.append(record)

    assert NPCResponseWindowManager.pending(current) == [record]
    assert "required_parts" not in record
    assert "answered_parts" not in record
    assert NPCResponseWindowManager.remaining_items(record) == [
        {"item_id": "legacy_2", "prompt": "落脚点"}
    ]
