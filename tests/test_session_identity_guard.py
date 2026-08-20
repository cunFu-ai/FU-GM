import json

from fu_gm.components.session_identity_guard import SessionIdentityGuard
from fu_gm.components.session_prep_concretizer import SessionPrepConcretizer
from fu_gm.models import SessionDramaticContract


def contract(number: int, *, variant: bool = False) -> SessionDramaticContract:
    return SessionDramaticContract(
        session_number=number,
        title=f"第{number:02d}场",
        location="白花碑驿站",
        focus_thread="失名旅人的归路" if not variant else "守望会是否公开反抗财团",
        opening_disruption=(
            "财团搜查队撞开驿站正门。"
            if not variant
            else "所有风铃突然倒着报出巡守的真名。"
        ),
        signature_image=(
            "染血铜钥匙在白花风铃下缓慢转动。"
            if not variant
            else "倒悬的白花风铃把名字凝成霜字。"
        ),
        dilemma=(
            "保护旅人或保住驿站的中立身份。"
            if not variant
            else "公开证词救下巡守，或沉默以保住整条旧路。"
        ),
        climax_type="门前谈判" if not variant else "让全站都听见的记忆仪式",
    )


def test_identity_guard_rejects_palette_swapped_session() -> None:
    previous = contract(1)
    repeated = contract(2)

    assessment = SessionIdentityGuard().assess(repeated, [previous])

    assert not assessment.distinct
    assert assessment.closest_session == 1
    assert "至少重做" in assessment.repair_instruction


def test_identity_guard_accepts_two_or_more_playable_axes_changing() -> None:
    assessment = SessionIdentityGuard().assess(contract(2, variant=True), [contract(1)])

    assert assessment.distinct
    assert len(assessment.differing_axes) >= 2


class SequenceClient:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self.payloads = payloads
        self.calls: list[dict[str, object]] = []

    def create_chat_completion(self, **kwargs) -> str:
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.payloads) - 1)
        return json.dumps(self.payloads[index], ensure_ascii=False)


def _prep_payload(source: SessionDramaticContract) -> dict[str, object]:
    return {
        "title": source.title,
        "dramatic_question": "英雄这次会选择什么？",
        "opening_disruption": source.opening_disruption,
        "signature_image": source.signature_image,
        "opposition_goal": "财团要封锁旧路。",
        "dilemma": source.dilemma,
        "reversal_evidence": "铜钥匙来自守望会内部。",
        "irreversible_change": "旧路的控制权会改变。",
        "closure_requirement": "必须决定旧路是否开放。",
        "ending_echo": "风铃在选择后改变方向。",
        "memory_anchor": "铜钥匙、是否保护旅人的选择、驿站立场改变。",
        "fantastic_details": ["风铃会冻结说谎者的吐息。"],
        "escalation_ladder": ["搜查队敲门", "巡守封门", "财团切断灯火"],
        "possible_payoffs": ["旧路开放", "旅人获救", "守望会公开站队"],
        "npcs": [],
        "clues": [
            {
                "approach": f"调查方法{index}",
                "source": f"线索来源{index}",
                "visible_lead": f"可见引导{index}",
                "success_reveal": "共同指向铜钥匙来自守望会内部。",
                "fallback": f"失败后转向备用入口{index}",
            }
            for index in range(1, 4)
        ],
        "scenes": [
            {
                "scene_role": role,
                "title": f"场景{index}",
                "location": "白花碑驿站",
                "situation": "搜查队正在施压。",
                "purpose": "让英雄选择如何处理旧路。",
                "pressure": "驿站即将封门。",
                "entry_points": ["跟随铜钥匙的痕迹"],
                "possible_changes": ["旧路控制权改变"],
                "npc_names": [],
            }
            for index, role in enumerate(
                ("strong_start", "alternate_approach", "climax_candidate"),
                start=1,
            )
        ],
    }


def test_concretizer_retries_only_when_recent_identity_is_too_similar() -> None:
    previous = contract(1)
    repeated = contract(2)
    repaired = contract(2, variant=True)
    client = SequenceClient([_prep_payload(repeated), _prep_payload(repaired)])

    concrete = SessionPrepConcretizer(client=client, model="fake").concretize(
        repeated,
        world_context={},
        recent_contracts=[previous],
    )

    assert len(client.calls) == 2
    assert "identity_repair" in json.loads(client.calls[1]["messages"][1].content)
    assert concrete.signature_image == repaired.signature_image
