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


def test_repeated_list_fact_is_an_idempotent_session_zero_contribution(tmp_path) -> None:
    service = FUGMHttpService(data_root=str(tmp_path), use_llm=False)
    runtime = service._runtime("列表事实团")
    runtime.app.session_zero_manager.start(participants=["阿凛", "南星"])
    complete = "先民从受侵蚀的旧大陆渡海而来，把记忆和技艺带到新土地。"

    first_context = _context(complete)
    first_context.speaker = "阿凛"
    first = service.gm_tool_registry.execute(
        "create_world_setting",
        {
            "category": "historical_events",
            "value": complete,
            "visibility": "public",
            "authority": "player_confirmed",
            "reason": "阿凛贡献历史事件。",
        },
        first_context,
    )
    second_context = _context(complete)
    second_context.speaker = "南星"
    second = service.gm_tool_registry.execute(
        "create_world_setting",
        {
            "category": "historical_events",
            "value": complete,
            "visibility": "public",
            "authority": "player_confirmed",
            "reason": "南星明确采用同一历史事件。",
        },
        second_context,
    )

    assert first.ok is True
    assert second.ok is True
    assert second.result["idempotent_contribution"] is True
    assert runtime.app.world_state.world_profile.historical_events == [complete]
    contributors = (
        runtime.app.session_zero_manager.state.world.historical_event_contributors
    )
    assert contributors["阿凛"] == [complete]
    assert contributors["南星"] == [complete]


def test_long_list_world_setting_is_not_limited_by_entity_name_length(tmp_path) -> None:
    service = FUGMHttpService(data_root=str(tmp_path), use_llm=False)
    runtime = service._runtime("列表事实团")
    runtime.app.session_zero_manager.start(participants=["南星"])
    complete = (
        "很久以前，钟鸣公国的静默钟并不是为哀悼而造，而是为了封印一场大瘟疫的记忆。"
        "那场瘟疫让死者无法安息，钟声会让人们暂时遗忘逝者的病痛与恐惧，使生者得以继续生活；"
        "然而封印并不完美，每隔一段时间遗忘就会松动，公国必须再次敲响钟声，因此静默钟逐渐成为权力与仪式的核心。"
    )
    assert len(complete) > 120

    receipt = service.gm_tool_registry.execute(
        "create_world_setting",
        {
            "category": "historical_events",
            "value": complete,
            "visibility": "public",
            "authority": "player_confirmed",
            "reason": "记录玩家明确贡献的重大历史事件。",
        },
        _context(complete),
    )

    assert receipt.ok is True
    assert runtime.app.session_zero_manager.state.world.historical_events == [
        complete
    ]

    revised = complete + "后来，人们开始质疑每一次重新敲钟究竟保护了谁。"
    updated = service.gm_tool_registry.execute(
        "update_world_setting",
        {
            "category": "historical_events",
            "name": complete,
            "value": revised,
            "visibility": "public",
            "authority": "player_confirmed",
            "reason": "记录玩家对同一历史事件的补充。",
        },
        _context(revised),
    )
    assert updated.ok is True
    assert runtime.app.session_zero_manager.state.world.historical_events == [revised]

    deleted = service.gm_tool_registry.execute(
        "delete_world_setting",
        {
            "category": "historical_events",
            "name": revised,
            "visibility": "public",
            "authority": "retcon",
            "reason": "验证长列表项可以被精确删除。",
        },
        _context("删除刚才的历史事件。"),
    )
    assert deleted.ok is True
    assert runtime.app.session_zero_manager.state.world.historical_events == []


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


def test_faction_does_not_count_as_session_zero_country_contribution(tmp_path) -> None:
    service = FUGMHttpService(data_root=str(tmp_path), use_llm=False)
    runtime = service._runtime("列表事实团")
    runtime.app.session_zero_manager.start(participants=["南星"])
    message = "我贡献听风者。他们是情报组织，不是国家或政治共同体。"

    receipt = service.gm_tool_registry.execute(
        "create_world_setting",
        {
            "category": "factions",
            "name": "听风者",
            "value": "听风者是情报组织，不是国家或政治共同体。",
            "visibility": "public",
            "authority": "player_confirmed",
            "reason": "记录玩家明确贡献的情报组织。",
        },
        _context(message),
    )

    participant = runtime.app.session_zero_manager.find_participant("南星")
    assert receipt.ok is True
    assert participant is not None
    assert "kingdom_contributions" not in participant.answered_topics
    assert runtime.app.session_zero_manager.state.world.kingdom_contributors == {}


def test_political_community_uses_kingdom_category_for_session_zero_contribution(
    tmp_path,
) -> None:
    service = FUGMHttpService(data_root=str(tmp_path), use_llm=False)
    runtime = service._runtime("列表事实团")
    runtime.app.session_zero_manager.start(participants=["南星"])

    receipt = service.gm_tool_registry.execute(
        "create_world_setting",
        {
            "category": "kingdoms",
            "name": "树誓村社",
            "value": "树誓村社不认王权，只和奥灵立约。",
            "visibility": "public",
            "authority": "player_confirmed",
            "reason": "记录玩家明确贡献的政治共同体。",
        },
        _context("我贡献树誓村社。村社不认王权，只和奥灵立约。"),
    )

    participant = runtime.app.session_zero_manager.find_participant("南星")
    assert receipt.ok is True
    assert participant is not None
    assert "kingdom_contributions" in participant.answered_topics
    assert runtime.app.session_zero_manager.state.world.kingdom_contributors == {
        "南星": ["树誓村社"]
    }
