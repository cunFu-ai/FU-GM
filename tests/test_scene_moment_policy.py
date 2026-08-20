from fu_gm.components.scene_moment_policy import SceneMomentPolicy
from fu_gm.expressor import Expressor


def test_backstage_scene_plan_is_never_used_as_public_fallback() -> None:
    packet = {
        "location": "白花碑驿站",
        "visible_elements": [
            "白花风铃在潮雾里轻响。",
            "失忆旅人站在廊柱旁。",
        ],
        "current_pressure": (
            "让玩家与白花守望会谈判，建立旧路与记忆异状的压力；"
            "必须出现：白花风铃、失忆旅人、白花守望会会长。"
        ),
    }

    fallback = SceneMomentPolicy.fallback(packet)
    heuristic = Expressor().render_scene_moment(packet)

    for text in (fallback, heuristic):
        assert "让玩家" not in text
        assert "必须出现" not in text
        assert "建立旧路" not in text
        assert "白花风铃" in text


def test_sanitizer_rejects_backstage_instruction_even_without_old_formula() -> None:
    text = "让玩家调查旧钟；必须出现：守门人和失忆旅人。"

    assert SceneMomentPolicy.sanitize(text, {}, allow_empty=True) == ""


def test_recap_prefers_latest_fact_and_latest_beat_over_old_nearby_scene_history() -> None:
    recap = SceneMomentPolicy.recap(
        {
            "location": "白花碑驿站·登记小室",
            "public_facts": [
                "白花守望会会长仍在风铃廊守门。",
                "巡逻队要求在门外逐项核验登记记录。",
            ],
            "recent_beats": [
                "会长在旧路闸门旁画下回撤线，并提出一长串放行条件。",
                "洛岚进入登记小室，门被掩到只留一道缝。",
            ],
        }
    )

    assert recap.startswith("众人仍在白花碑驿站·登记小室。")
    assert "巡逻队要求在门外逐项核验登记记录" in recap
    assert "洛岚进入登记小室" in recap
    assert "会长在旧路闸门旁" not in recap


def test_online_nudge_rejects_offline_gm_stage_directions() -> None:
    rejected = (
        "时悠敲了敲桌面：这颗一很有自己的想法。",
        "时悠从屏幕后探出头：人还在吗？",
        "时悠托着下巴等了一会儿：不急。",
        "时悠做了个提醒大家的动作：还在吗？",
        "（时悠笑了笑）这颗一确实很有想法。",
        "（敲桌）这骰子真不给面子。",
        "*托腮* 我继续等。",
        "时悠：我在，慢慢来。",
    )
    allowed = (
        "这颗一，确实很有自己的想法。",
        "刚才那颗一是真不给面子。",
        "我先不替牢门加戏，等你们。",
        "不急，你们商量好再叫我。",
        "别敲桌了，这颗骰子已经够响。",
    )

    for reply in rejected:
        assert SceneMomentPolicy.has_gm_stage_direction(reply)
    for reply in allowed:
        assert not SceneMomentPolicy.has_gm_stage_direction(reply)


def test_scene_moment_rejects_unconfirmed_second_person_action() -> None:
    violation = SceneMomentPolicy.player_agency_violation(
        "雨水顺着符文流下。你走近两步，伸手碰向铁栏。",
        {},
    )

    assert "替玩家角色执行" in violation
    assert SceneMomentPolicy.player_agency_violation(
        "你提灯走过旧牢区，又在铁栏前停下。",
        {},
    )
    assert SceneMomentPolicy.player_agency_violation(
        "你们俩隔着铁栏对视一眼，光雾在脚边翻涌。",
        {},
    )


def test_scene_moment_allows_sensory_framing_and_npc_command() -> None:
    assert not SceneMomentPolicy.player_agency_violation(
        "你听见铁门后传来钥匙声。维蕾娅说：‘你们退后。’",
        {"prepared_npcs": [{"name": "维蕾娅", "public_role": "值夜狱卒"}]},
    )
    assert not SceneMomentPolicy.player_agency_violation(
        "铁门已经打开，你们可以进入。",
        {},
    )


def test_scene_moment_rejects_npc_written_as_second_person() -> None:
    violation = SceneMomentPolicy.player_agency_violation(
        "你——值夜狱卒维蕾娅——沿着湿滑走廊巡视。",
        {"prepared_npcs": [{"name": "维蕾娅", "public_role": "值夜狱卒"}]},
    )

    assert violation
