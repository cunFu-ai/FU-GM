from scripts.migrate_country_contributions import migrate_payload


def _payload(*, contribution: str, evidence: str) -> dict[str, object]:
    return {
        "session_zero": {
            "world": {
                "kingdoms": {"岚国": "风暴海岸的王国。"},
                "kingdom_contributors": {"阿凛": [contribution]},
            },
            "participants": [
                {
                    "name": "阿凛",
                    "answered_topics": ["kingdom_contributions"],
                    "contributions": [evidence],
                }
            ],
        },
        "world_state": {
            "world_profile": {
                "kingdoms": {"岚国": "风暴海岸的王国。"},
                "kingdom_contributors": {"阿凛": [contribution]},
            }
        },
    }


def test_migration_removes_map_location_and_false_completion() -> None:
    migrated, changes = migrate_payload(
        _payload(contribution="西部森林", evidence="大陆西边是一片森林。")
    )

    world = migrated["session_zero"]["world"]
    world_profile = migrated["world_state"]["world_profile"]
    participant = migrated["session_zero"]["participants"][0]
    assert world["kingdom_contributors"] == {}
    assert world_profile["kingdom_contributors"] == {}
    assert "kingdom_contributions" not in participant["answered_topics"]
    assert changes[0]["removed_non_kingdom_values"] == ["西部森林"]
    assert changes[0]["projections_repaired"] == [
        "session_zero.world",
        "world_state.world_profile",
    ]


def test_migration_keeps_real_country_and_explicit_skip() -> None:
    real, real_changes = migrate_payload(
        _payload(contribution="岚国", evidence="我的国家是岚国。")
    )
    assert real["session_zero"]["world"]["kingdom_contributors"] == {
        "阿凛": ["岚国"]
    }
    assert real["world_state"]["world_profile"]["kingdom_contributors"] == {
        "阿凛": ["岚国"]
    }
    assert real_changes == []

    skipped, changes = migrate_payload(
        _payload(contribution="西部森林", evidence="国家这项我先跳过。")
    )
    participant = skipped["session_zero"]["participants"][0]
    assert "kingdom_contributions" in participant["answered_topics"]
    assert changes[0]["explicit_skip_preserved"] is True
