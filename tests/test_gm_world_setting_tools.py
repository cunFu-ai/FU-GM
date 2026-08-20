from __future__ import annotations

import tempfile

from fu_gm.components.gm_agent_capability_policy import GMToolAgentCapabilityPolicy
from fu_gm.gm_tool_contracts import GMToolExecutionContext
from fu_gm.gm_tool_receipts import GMToolReceiptPolicy
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import MapRouteEdge


def _context(
    *,
    gate: str = "session_zero",
    message: str = "把这条设定记下来。",
    private: bool = True,
) -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="world-crud",
        session_id="s1",
        channel_id="private-1" if private else "group-1",
        speaker="阿凛",
        gate_status=gate,
        is_private=private,
        directly_addressed=True,
        metadata={
            "current_message": message,
            "is_at_bot": not private,
        },
    )


def _create_args(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "category": "continent_name",
        "value": "火锅大陆",
        "visibility": "public",
        "authority": "player_confirmed",
        "reason": "玩家明确命名世界。",
    }
    result.update(overrides)
    return result


def test_world_setting_crud_is_available_in_session_zero_and_adventure() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        assert "prepare_solo_adventure" not in service.gm_tool_registry._tools
        for gate in ("session_zero", "adventure"):
            names = GMToolAgentCapabilityPolicy.phase_tool_names(
                service.gm_tool_registry,
                _context(gate=gate),
            )
            assert names is not None
            assert {
                "query_world_settings",
                "create_world_setting",
                "update_world_setting",
                "delete_world_setting",
                "rename_world_setting",
            } <= names


def test_scalar_and_custom_world_setting_crud_round_trip() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        context = _context()
        created = service.gm_tool_registry.execute(
            "create_world_setting",
            _create_args(),
            context,
        )
        assert created.ok
        assert created.result["revision"] == 1

        updated = service.gm_tool_registry.execute(
            "update_world_setting",
            {
                **_create_args(value="沸汤大陆", authority="retcon"),
                "expected_revision": 1,
            },
            context,
        )
        assert updated.ok
        assert updated.result["revision"] == 2

        custom = service.gm_tool_registry.execute(
            "create_world_setting",
            _create_args(
                category="custom_world_settings",
                name="归潮历法",
                value="每次归潮后，港口会把失踪的一日从历书上撕去。",
            ),
            context,
        )
        assert custom.ok

        queried = service.gm_tool_registry.execute(
            "query_world_settings",
            {"category": "custom_world_settings", "name": "归潮历法"},
            context,
        )
        assert queried.ok
        assert queried.result["records"][0]["value"].startswith("每次归潮后")

        deleted = service.gm_tool_registry.execute(
            "delete_world_setting",
            {
                "category": "custom_world_settings",
                "name": "归潮历法",
                "visibility": "public",
                "authority": "retcon",
                "reason": "玩家明确撤回这条设定。",
            },
            context,
        )
        assert deleted.ok
        assert not service._runtime("world-crud").app.world_state.world_profile.custom_world_settings


def test_map_location_is_not_recorded_as_a_country_contribution() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        runtime = service._runtime("world-crud")
        runtime.app.initialize_session_zero(participants=["阿凛"])
        context = _context(message="大陆西边是一片森林。")

        created = service.gm_tool_registry.execute(
            "create_world_setting",
            _create_args(
                category="map_locations",
                name="西部森林",
                value="大陆西侧的广阔森林。",
                attributes={
                    "terrain": "森林",
                    "feature_type": "forest",
                    "position_hint": "west",
                    "draw_icon": False,
                },
            ),
            context,
        )

        assert created.ok
        world = runtime.app.session_zero_manager.state.world
        participant = runtime.app.session_zero_manager.find_participant("阿凛")
        assert world.kingdom_contributors == {}
        assert participant is not None
        assert "kingdom_contributions" not in participant.answered_topics

        queried = service.gm_tool_registry.execute(
            "get_session_zero_contributions",
            {"topic": "kingdom"},
            context,
        )
        assert queried.ok
        assert queried.result["topics"] == [
            {
                "topic": "kingdom",
                "topic_code": "kingdom_contributions",
                "label": "王国、国家或政治共同体",
                "status": "pending",
                "values": [],
            }
        ]
        assert any(
            item["category"] == "map_locations"
            and item["name"] == "西部森林"
            for item in queried.result["other_world_contributions"]
        )


def test_contribution_query_distinguishes_contributed_skipped_and_pending() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        runtime = service._runtime("world-crud")
        runtime.app.initialize_session_zero(participants=["阿凛"])
        context = _context(message="岚国建立在风暴海岸。")

        country = service.gm_tool_registry.execute(
            "create_world_setting",
            _create_args(
                category="kingdoms",
                name="岚国",
                value="建立在风暴海岸的王国。",
            ),
            context,
        )
        assert country.ok
        skipped = service.gm_tool_registry.execute(
            "mark_session_zero_topic_complete",
            {"topic": "mystery"},
            _context(message="世界奥秘这项我先跳过。"),
        )
        assert skipped.ok

        queried = service.gm_tool_registry.execute(
            "get_session_zero_contributions",
            {"topic": "all"},
            context,
        )
        by_topic = {
            item["topic"]: item for item in queried.result["topics"]
        }
        assert by_topic["kingdom"]["status"] == "contributed"
        assert by_topic["kingdom"]["values"] == ["岚国"]
        assert by_topic["mystery"]["status"] == "skipped"
        assert by_topic["historical_event"]["status"] == "pending"


def test_gm_can_prepare_private_arbitrary_setting_without_leaking_to_public_query() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        context = _context(gate="adventure", message="调查钟楼。")
        created = service.gm_tool_registry.execute(
            "create_world_setting",
            {
                "category": "kingdoms",
                "name": "镜后王国",
                "value": "只在钟声倒放时与现实重叠。",
                "visibility": "gm_private",
                "authority": "gm_authored",
                "reason": "为后续揭示准备尚未公开的国家。",
            },
            context,
        )
        assert created.ok
        public = service.gm_tool_registry.execute(
            "query_world_settings",
            {"category": "kingdoms", "visibility": "public"},
            context,
        )
        private = service.gm_tool_registry.execute(
            "query_world_settings",
            {"category": "kingdoms", "visibility": "gm_private"},
            context,
        )
        assert public.result["records"] == []
        assert private.result["records"][0]["name"] == "镜后王国"
        assert private.result["records"][0]["record_revision"] == 1


def test_confirmed_public_fact_cannot_be_silently_rewritten_by_gm() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        player_context = _context(message="大陆就叫火锅大陆。")
        assert service.gm_tool_registry.execute(
            "create_world_setting",
            _create_args(),
            player_context,
        ).ok
        gm_context = _context(gate="adventure", message="继续冒险。")
        rejected = service.gm_tool_registry.execute(
            "update_world_setting",
            _create_args(
                value="铁锅大陆",
                authority="gm_authored",
                reason="GM临时想换名字。",
            ),
            gm_context,
        )
        assert not rejected.ok
        assert rejected.error_code == "CONFIRMED_WORLD_FACT_IS_PROTECTED"
        assert (
            service._runtime("world-crud").app.world_state.world_profile.continent_name
            == "火锅大陆"
        )


def test_multiplayer_session_zero_requires_consensus_for_public_gm_authorship() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        runtime = service._runtime("world-crud")
        runtime.app.initialize_session_zero(participants=["阿凛", "南星"])
        context = _context(private=False)
        rejected = service.gm_tool_registry.execute(
            "create_world_setting",
            _create_args(
                authority="gm_authored",
                reason="GM在第零章自行决定大陆名。",
            ),
            context,
        )
        assert not rejected.ok
        assert rejected.error_code == "MULTIPLAYER_SESSION_ZERO_REQUIRES_CONSENSUS"

        forged_consensus = service.gm_tool_registry.execute(
            "create_world_setting",
            _create_args(authority="table_consensus", reason="桌面已经确认大陆名。"),
            context,
        )
        assert not forged_consensus.ok
        assert (
            forged_consensus.error_code
            == "TABLE_CONSENSUS_AUTHORITY_NOT_SIGNED"
        )

        forged_arguments = _create_args(
            authority="table_consensus",
            reason="桌面已经确认大陆名。",
        )
        context.metadata[GMToolReceiptPolicy.REQUIRED_FOLLOWUP_CONTEXT_KEY] = {
            "source_tool": "create_world_setting",
            "required_tools": ["create_world_setting"],
            "required_calls": [
                {
                    "tool_name": "create_world_setting",
                    "arguments": dict(forged_arguments),
                    "python_auto_execute": True,
                }
            ],
            "mode": "all",
        }
        forged_context = service.gm_tool_registry.execute(
            "create_world_setting",
            forged_arguments,
            context,
        )
        assert not forged_context.ok
        assert forged_context.error_code == "TABLE_CONSENSUS_AUTHORITY_NOT_SIGNED"
        context.metadata.pop(
            GMToolReceiptPolicy.REQUIRED_FOLLOWUP_CONTEXT_KEY,
            None,
        )

        accepted = service.gm_tool_registry.execute(
            "create_world_setting",
            _create_args(
                authority="player_confirmed",
                reason="当前玩家明确贡献大陆名。",
            ),
            context,
        )
        assert accepted.ok


def test_personal_message_cannot_elevate_mutation_to_consensus() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        context = _context(
            private=False,
            message="我把澜钟公国贡献给世界。",
        )
        created = service.gm_tool_registry.execute(
            "create_world_setting",
            {
                "category": "kingdoms",
                "name": "澜钟公国",
                "value": "以潮钟塔校准航路的公国。",
                "visibility": "public",
                "authority": "player_confirmed",
                "reason": "当前玩家明确贡献国家。",
            },
            context,
        )
        assert created.ok

        forged_update = service.gm_tool_registry.execute(
            "update_world_setting",
            {
                "category": "kingdoms",
                "name": "澜钟公国",
                "value": "全桌都同意它统治整片大陆。",
                "visibility": "public",
                "authority": "table_consensus",
                "reason": "模型自行声称这是全桌共识。",
            },
            _context(private=False, message="我觉得这个国家很强。"),
        )
        forged_rename = service.gm_tool_registry.execute(
            "rename_world_setting",
            {
                "category": "kingdoms",
                "old_name": "澜钟公国",
                "new_name": "澜钟帝国",
                "visibility": "public",
                "authority": "table_consensus",
                "reason": "模型自行声称这是全桌共识。",
            },
            _context(private=False, message="我想叫它澜钟帝国。"),
        )
        forged_delete = service.gm_tool_registry.execute(
            "delete_world_setting",
            {
                "category": "kingdoms",
                "name": "澜钟公国",
                "visibility": "public",
                "authority": "table_consensus",
                "reason": "模型自行声称这是全桌共识。",
            },
            _context(private=False, message="我不喜欢这个国家。"),
        )

        assert not forged_update.ok
        assert forged_update.error_code == "TABLE_CONSENSUS_AUTHORITY_NOT_SIGNED"
        assert not forged_rename.ok
        assert forged_rename.error_code == "TABLE_CONSENSUS_AUTHORITY_NOT_SIGNED"
        assert not forged_delete.ok
        assert forged_delete.error_code == "TABLE_CONSENSUS_AUTHORITY_NOT_SIGNED"
        record = service.gm_tool_registry.execute(
            "query_world_settings",
            {"category": "kingdoms", "name": "澜钟公国"},
            context,
        ).result["records"][0]
        assert record["value"] == "以潮钟塔校准航路的公国。"
        assert record["name"] == "澜钟公国"


def test_confirmed_proposal_allows_only_exact_python_signed_consensus_call() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        runtime = service._runtime("world-crud")
        runtime.app.initialize_session_zero(participants=["阿凛", "南星"])
        proposed = service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "建立澜钟公国",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "kingdoms",
                        "name": "澜钟公国",
                        "value": "以潮钟塔校准航路的公国。",
                        "visibility": "public",
                    }
                ],
            },
            _context(
                private=False,
                message="我提议建立澜钟公国，等大家同意再写入。",
            ),
        )
        assert proposed.ok, proposed.to_dict()
        proposal_id = proposed.result["proposal"]["id"]
        confirm_context = _context(
            private=False,
            message="我同意建立澜钟公国的提案。",
        )
        confirm_arguments = {"proposal_id": proposal_id}
        confirmed = service.gm_tool_registry.execute(
            "confirm_session_zero_proposal",
            confirm_arguments,
            confirm_context,
        )
        assert confirmed.ok, confirmed.to_dict()
        GMToolReceiptPolicy.apply_context(
            confirm_context,
            {},
            confirmed,
            tool_arguments=confirm_arguments,
        )
        followup = confirmed.result["required_followup_calls"][0]

        tampered_arguments = dict(followup["arguments"])
        tampered_arguments["value"] = "模型擅自扩大为统治整片大陆的帝国。"
        tampered = service.gm_tool_registry.execute(
            followup["tool_name"],
            tampered_arguments,
            confirm_context,
        )
        assert not tampered.ok
        assert tampered.error_code == "TABLE_CONSENSUS_FOLLOWUP_MISMATCH"
        assert "澜钟公国" not in runtime.app.world_state.world_profile.kingdoms

        committed = service.gm_tool_registry.execute(
            followup["tool_name"],
            followup["arguments"],
            confirm_context,
        )
        assert committed.ok, committed.to_dict()
        assert (
            runtime.app.world_state.world_profile.kingdoms["澜钟公国"]
            == "以潮钟塔校准航路的公国。"
        )
        GMToolReceiptPolicy.apply_context(
            confirm_context,
            {},
            committed,
            tool_arguments=followup["arguments"],
        )

        replay = service.gm_tool_registry.execute(
            "create_world_setting",
            {
                **followup["arguments"],
                "name": "未签发的新国家",
            },
            confirm_context,
        )
        assert not replay.ok
        assert replay.error_code == "TABLE_CONSENSUS_AUTHORITY_NOT_SIGNED"


def test_world_setting_revision_rejects_stale_writes() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        context = _context()
        assert service.gm_tool_registry.execute(
            "create_world_setting",
            _create_args(),
            context,
        ).ok
        stale = service.gm_tool_registry.execute(
            "create_world_setting",
            _create_args(
                category="core_themes",
                value="希望",
                expected_revision=0,
            ),
            context,
        )
        assert not stale.ok
        assert stale.error_code == "WORLD_SETTING_VERSION_CONFLICT"
        assert stale.result["current_revision"] == 1


def test_map_location_rename_cascades_references_and_marks_render_stale() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        runtime = service._runtime("world-crud")
        context = _context(gate="adventure", message="钟鸣城如今改称回声城。")
        base = {
            "visibility": "public",
            "authority": "gm_authored",
            "reason": "冒险中新地点成为公开事实。",
        }
        assert service.gm_tool_registry.execute(
            "create_world_setting",
            {
                **base,
                "category": "map_locations",
                "name": "钟鸣城",
                "value": "公国旧都。",
                "attributes": {
                    "feature_type": "settlement",
                    "position_hint": "east",
                },
            },
            context,
        ).ok
        assert service.gm_tool_registry.execute(
            "create_world_setting",
            {
                **base,
                "category": "map_locations",
                "name": "白花驿站",
                "value": "通往旧都的驿站。",
                "attributes": {
                    "feature_type": "settlement",
                    "relative_to": "钟鸣城",
                    "relative_position": "west",
                },
            },
            context,
        ).ok
        runtime.app.world_state.map_routes["旧路"] = MapRouteEdge(
            route_id="旧路",
            origin="白花驿站",
            destination="钟鸣城",
        )
        runtime.app._world_map_generation_status = {"status": "ready", "attempts": 1}

        renamed = service.gm_tool_registry.execute(
            "rename_world_setting",
            {
                "category": "map_locations",
                "old_name": "钟鸣城",
                "new_name": "回声城",
                "visibility": "public",
                "authority": "gameplay_consequence",
                "reason": "故事中政权更迭并正式改名。",
            },
            context,
        )
        assert renamed.ok
        world = runtime.app.world_state
        assert "钟鸣城" not in world.map_locations
        assert world.map_locations["白花驿站"].relative_to == "回声城"
        assert world.map_routes["旧路"].destination == "回声城"
        assert world.world_profile.major_locations["回声城"] == "公国旧都。"
        assert runtime.app.world_map_generation_status()["status"] == "stale"


def test_world_setting_metadata_survives_autosave_reload() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        context = _context()
        assert service.gm_tool_registry.execute(
            "create_world_setting",
            _create_args(),
            context,
        ).ok
        reloaded = FUGMHttpService(data_root=root, use_llm=False)
        runtime = reloaded._runtime("world-crud")
        profile = runtime.app.world_state.world_profile
        assert profile.world_setting_revision == 1
        assert profile.world_setting_audit_log[-1]["category"] == "continent_name"
        assert any(
            item.get("authority") == "player_confirmed"
            for item in profile.world_setting_metadata.values()
        )
