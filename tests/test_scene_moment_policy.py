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
