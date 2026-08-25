from __future__ import annotations

from types import SimpleNamespace

from fu_gm.testing.natural_campaign_spine import (
    NaturalCampaignSource,
    build_natural_campaign_beats,
    build_natural_chapter_package,
)
from fu_gm.testing.longrun_table_roster import THREE_PLAYER_LONGRUN_ROSTER
from scripts.run_20_session_campaign_test import TwentySessionCampaignHarness


def _profile() -> SimpleNamespace:
    return SimpleNamespace(
        continent_name="余烬大陆",
        world_shape="中央狭长内海横贯东西",
        magic_tech_role="魔法装置常见，施法者稀少",
        group_concept="临时结成的护送队",
        starting_region="暮钟港",
        selected_first_act_summary="护送一名失忆旅人穿过暮钟港",
        major_locations={"银刃内海": "横贯大陆中央"},
        kingdoms={
            "辉钢联营邦": "位于西北的采掘城联盟",
            "逐风群盟": "位于南方、随季风迁移的岛屿共同体",
        },
        historical_events=["灰烬之潮曾让北方三座城市失去名字"],
        mysteries=["内海为何会回应被遗忘的名字"],
        world_threats=["灰烬之潮正在复苏"],
        villain_seeds=["一名执政官想垄断所有记忆装置"],
        hero_drafts={
            "阿凛": SimpleNamespace(
                hero_name="伊莉雅",
                identity="盾誓骑士",
                theme="希望",
                origin="暮钟港",
            ),
            "南星": SimpleNamespace(
                hero_name="赛璃",
                identity="御魂医师",
                theme="慈悲",
                origin="逐风群盟",
            ),
            "白河": SimpleNamespace(
                hero_name="洛岚",
                identity="出逃工匠",
                theme="赎罪",
                origin="辉钢联营邦",
            ),
        },
    )


def test_natural_campaign_source_uses_only_confirmed_world_and_roster() -> None:
    source = NaturalCampaignSource.from_world(
        _profile(),
        map_locations={
            "暮钟港": SimpleNamespace(description="位于内海北岸"),
        },
        hero_names=("伊莉雅", "赛璃", "洛岚"),
    )

    assert source.starting_region == "暮钟港"
    assert dict(source.locations)["暮钟港"] == "位于内海北岸"
    assert len(source.hero_threads) == 3
    assert all(name in " ".join(source.hero_threads) for name in ("伊莉雅", "赛璃", "洛岚"))


def test_twenty_session_natural_spine_has_distinct_memories_and_no_old_party() -> None:
    source = NaturalCampaignSource.from_world(
        _profile(),
        map_locations={"暮钟港": SimpleNamespace(description="位于内海北岸")},
        hero_names=("伊莉雅", "赛璃", "洛岚"),
    )

    beats = build_natural_campaign_beats(source, target_sessions=20)
    rendered = "\n".join(
        text
        for beat in beats
        for text in (
            beat.title,
            beat.opening_instruction,
            str(dict(beat.episode_identity)),
        )
    )

    assert len(beats) == 20
    assert len({beat.title for beat in beats}) == 20
    assert beats[0].location == "暮钟港"
    assert beats[-1].boss_session is True
    assert "三名英雄与世界获得具体尾声" in rendered
    assert "时雨" not in rendered
    assert "澄砚" not in rendered
    assert "苍祈" not in rendered
    assert "五名英雄" not in rendered


def test_natural_chapter_package_is_grounded_in_selected_first_act() -> None:
    source = NaturalCampaignSource.from_world(
        _profile(),
        map_locations={"暮钟港": SimpleNamespace(description="位于内海北岸")},
        hero_names=("伊莉雅", "赛璃", "洛岚"),
    )

    package = build_natural_chapter_package(source)

    assert package.chapter_title == "暮钟港的第一幕"
    assert package.synopsis == "护送一名失忆旅人穿过暮钟港"
    assert len(package.scenes) == 3
    assert all(scene.location == "暮钟港" for scene in package.scenes)
    assert "白花碑驿站" not in str(package)


def test_natural_runner_projects_spine_to_exact_three_player_table() -> None:
    source = NaturalCampaignSource.from_world(
        _profile(),
        map_locations={"暮钟港": SimpleNamespace(description="位于内海北岸")},
        hero_names=("伊莉雅", "赛璃", "洛岚"),
    )
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.target_sessions = 20
    harness.table_roster = THREE_PLAYER_LONGRUN_ROSTER
    harness._natural_table_active = lambda: True
    harness._natural_campaign_source = lambda: source

    specs = harness._campaign_sessions()
    rendered = str(specs)

    assert len(specs) == 20
    assert all(
        tuple(speaker for speaker, _message in spec.turns)
        == THREE_PLAYER_LONGRUN_ROSTER.player_names
        for spec in specs
    )
    assert all(spec.location for spec in specs)
    assert all(spec.episode_identity for spec in specs)
    assert "时雨" not in rendered
    assert "澄砚" not in rendered
    assert "五名英雄" not in rendered
