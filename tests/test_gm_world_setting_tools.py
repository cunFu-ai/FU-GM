from __future__ import annotations

from fu_gm.gm_tool_execution import GMToolCallLedger
from fu_gm.gm_tool_contracts import GMToolExecutionContext
from fu_gm.http_server import FUGMHttpService


def _context(message: str) -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="列表事实团",
        session_id="s0",
        channel_id="group-1",
        speaker="南星",
        gate_status="session_zero",
        directly_addressed=False,
        metadata={"current_message": message},
    )


def test_list_world_setting_rejects_a_title_that_would_be_discarded(tmp_path) -> None:
    service = FUGMHttpService(data_root=str(tmp_path), use_llm=False)
    message = "苍白司教团把灰晶病包装成灵魂升格，这是我贡献的威胁。"

    receipt = service.gm_tool_registry.execute(
        "create_world_setting",
        {
            "category": "world_threats",
            "name": "苍白司教团",
            "value": "把灰晶病包装成灵魂升格的教团。",
            "visibility": "public",
            "authority": "player_confirmed",
            "reason": "记录玩家明确贡献的世界威胁。",
        },
        _context(message),
    )

    assert receipt.ok is False
    assert receipt.error_code == "WORLD_LIST_ITEM_NAME_MUST_EQUAL_VALUE"
    assert service._runtime("列表事实团").app.world_state.world_profile.world_threats == []


def test_list_world_setting_persists_the_complete_value(tmp_path) -> None:
    service = FUGMHttpService(data_root=str(tmp_path), use_llm=False)
    message = "苍白司教团把灰晶病包装成灵魂升格，这是我贡献的威胁。"
    complete = "苍白司教团把灰晶病包装成灵魂升格。"

    receipt = service.gm_tool_registry.execute(
        "create_world_setting",
        {
            "category": "world_threats",
            "value": complete,
            "visibility": "public",
            "authority": "player_confirmed",
            "reason": "记录玩家明确贡献的世界威胁。",
        },
        _context(message),
    )

    assert receipt.ok is True
    assert service._runtime("列表事实团").app.world_state.world_profile.world_threats == [
        complete
    ]


def test_create_existing_world_setting_redirects_agent_to_exact_update(tmp_path) -> None:
    service = FUGMHttpService(data_root=str(tmp_path), use_llm=False)
    context = _context("镜线内海位于大陆中央，并与外海相连。")
    first_arguments = {
        "category": "map_locations",
        "name": "镜线内海",
        "value": "镜线内海位于大陆中央。",
        "visibility": "public",
        "authority": "player_confirmed",
        "reason": "记录玩家明确提出的地点。",
    }
    created = service.gm_tool_registry.execute(
        "create_world_setting",
        first_arguments,
        context,
    )
    assert created.ok is True

    richer_arguments = {
        **first_arguments,
        "value": "镜线内海位于大陆中央，并通过宽阔海峡与外海相连。",
        "attributes": {
            "feature_type": "inland_sea",
            "position_hint": "center",
        },
        "reason": "补充玩家刚确认的海峡与位置。",
    }
    ledger = GMToolCallLedger(
        registry=service.gm_tool_registry,
        context=context,
        state_summary={},
    )
    duplicate_event = ledger.execute(
        "create_world_setting",
        richer_arguments,
    )
    duplicate = duplicate_event.receipt

    assert duplicate is not None
    assert duplicate.ok is False
    assert duplicate.error_code == "WORLD_SETTING_ALREADY_EXISTS"
    assert duplicate.result["required_next_tool"] == "update_world_setting"
    suggested = duplicate.result["suggested_arguments"]
    assert suggested["category"] == "map_locations"
    assert suggested["name"] == "镜线内海"
    assert suggested["value"] == richer_arguments["value"]
    assert suggested["attributes"] == richer_arguments["attributes"]
    assert suggested["expected_revision"] == created.result["revision"]
    assert ledger.required_retry_tool == "update_world_setting"
    retry_error = ledger.retry_protocol_error(
        {"decision": "final", "reply": "已经记好了。"}
    )
    assert retry_error is not None
    assert retry_error["protocol_error"]["error_code"] == "REDIRECT_TOOL_OMITTED"

    updated_event = ledger.execute("update_world_setting", suggested)
    assert updated_event.receipt is not None
    assert updated_event.receipt.ok is True
    assert ledger.required_retry_pending is False


def test_map_location_treats_empty_optional_hints_as_unspecified(tmp_path) -> None:
    service = FUGMHttpService(data_root=str(tmp_path), use_llm=False)
    context = _context("我贡献一个边境驿站，给商旅歇脚。")

    receipt = service.gm_tool_registry.execute(
        "create_world_setting",
        {
            "category": "map_locations",
            "name": "边境驿站",
            "value": "边境驿站供大陆上的商旅和旅人歇脚。",
            "attributes": {
                "feature_type": "settlement",
                "terrain": "平原",
                "position_hint": "",
                "relative_to": "",
                "relative_position": "",
                "draw_icon": True,
            },
            "visibility": "public",
            "authority": "player_confirmed",
            "reason": "记录玩家明确贡献的地点。",
        },
        context,
    )

    assert receipt.ok is True
    location = service._runtime("列表事实团").app.world_state.map_locations[
        "边境驿站"
    ]
    assert location.position_hint == ""
    assert location.relative_to == ""
    assert location.relative_position == ""
    assert location.feature_type == "settlement"


def test_faction_counts_as_session_zero_political_community_contribution(tmp_path) -> None:
    service = FUGMHttpService(data_root=str(tmp_path), use_llm=False)
    runtime = service._runtime("列表事实团")
    runtime.app.session_zero_manager.start(participants=["南星"])
    message = "我贡献树誓村社。村社不认王权，只和奥灵立约。"

    receipt = service.gm_tool_registry.execute(
        "create_world_setting",
        {
            "category": "factions",
            "name": "树誓村社",
            "value": "树誓村社不认王权，只和奥灵立约。",
            "visibility": "public",
            "authority": "player_confirmed",
            "reason": "记录玩家明确贡献的政治共同体。",
        },
        _context(message),
    )

    participant = runtime.app.session_zero_manager.find_participant("南星")
    assert receipt.ok is True
    assert participant is not None
    assert "kingdom_contributions" in participant.answered_topics
    assert runtime.app.session_zero_manager.state.world.kingdom_contributors == {
        "南星": ["树誓村社"]
    }
