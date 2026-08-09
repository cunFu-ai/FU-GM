from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from fu_gm.gm_tool_agent import GMToolExecutionContext
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import (
    Character,
    EnemyRank,
    PartyMemberEntry,
    PartySheet,
    SessionDramaticContract,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / ".runtime" / "prompt_probes"


class CapturingClient:
    def __init__(self, delegate: Any, label: str, records: list[dict[str, Any]]) -> None:
        self.delegate = delegate
        self.label = label
        self.records = records
        self.config = delegate.config

    def create_chat_completion(self, **kwargs: Any) -> str:
        messages = [
            {
                "role": str(getattr(message, "role", "")),
                "content": str(getattr(message, "content", "")),
            }
            for message in list(kwargs.get("messages") or [])
        ]
        started = time.monotonic()
        record = {
            "label": self.label,
            "model": str(kwargs.get("model") or ""),
            "operation": str(kwargs.get("operation") or ""),
            "temperature": kwargs.get("temperature"),
            "messages": messages,
        }
        self.records.append(record)
        try:
            result = self.delegate.create_chat_completion(**kwargs)
        except Exception as exc:
            record["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            record["error"] = str(exc)
            raise
        record["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        record["response"] = result
        return result


def tool_context(
    campaign_id: str,
    session_id: str,
    channel_id: str,
    message: str,
    *,
    speaker: str = "阿凛",
) -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id=campaign_id,
        session_id=session_id,
        channel_id=channel_id,
        speaker=speaker,
        gate_status="adventure",
        directly_addressed=True,
        metadata={
            "current_message": message,
            "recent_public_context": "",
        },
    )


def add_character(app: Any, character: Character) -> None:
    if not app.character_manager.exists(character.name):
        app.character_manager.add(character)


def install_capture_clients(
    service: FUGMHttpService,
    app: Any,
    records: list[dict[str, Any]],
) -> None:
    if service.gm_tool_agent is not None:
        core = CapturingClient(
            service.gm_tool_agent._decision_requester.client,
            "core_gm",
            records,
        )
        service.gm_tool_agent.client = core
        service.gm_tool_agent._decision_requester.client = core
    if hasattr(app.expressor, "client"):
        app.expressor.client = CapturingClient(
            app.expressor.client,
            "expressor",
            records,
        )


def write_prompt_review(
    output_dir: Path,
    records: list[dict[str, Any]],
) -> None:
    """Export every exact API prompt so the single-agent path is auditable."""

    prompt_dir = output_dir / "captured_prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    review_lines = [
        "# 国王交涉与卫兵冲突：实际模型提示词",
        "",
        "以下文件均由探针从真实 API 调用参数原样导出，不是从源码重构。",
        "NPC对话与敌方战斗均应只出现core_gm调用，不应存在npc_dialogue或npc_combat子模型。",
        "",
    ]
    selected_phases: dict[str, tuple[str, str]] = {}
    for index, record in enumerate(records, start=1):
        label = str(record.get("label") or "").strip() or "unknown"
        messages = list(record.get("messages") or [])
        if not messages:
            continue
        system_text = str(messages[0].get("content") or "")
        user_text = str(messages[1].get("content") or "") if len(messages) > 1 else ""
        response_text = str(record.get("response") or "")
        if "主动节拍工具决策层" in system_text:
            phase = "heartbeat"
        elif "工具事务收尾层" in system_text:
            phase = "post_tool"
        elif label == "expressor":
            phase = "expression"
        else:
            phase = "decision"
        stem = f"{index:02d}_{label}_{phase}"
        system_name = f"{stem}_system.txt"
        user_name = f"{stem}_user_context.txt"
        response_name = f"{stem}_raw_response.txt"
        messages_name = f"{stem}_messages.json"
        (prompt_dir / system_name).write_text(system_text, encoding="utf-8")
        (prompt_dir / user_name).write_text(user_text, encoding="utf-8")
        (prompt_dir / response_name).write_text(response_text, encoding="utf-8")
        (prompt_dir / messages_name).write_text(
            json.dumps(messages, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        selected_phases.setdefault(
            f"{label}_{phase}",
            (system_name, user_name),
        )
        review_lines.extend(
            [
                f"## {index:02d} {label} / {phase}",
                "",
                f"- 模型：`{record.get('model', '')}`",
                f"- 操作：`{record.get('operation', '')}`",
                f"- 耗时：`{record.get('elapsed_ms', '')} ms`",
                f"- 完整系统提示词：`captured_prompts/{system_name}`（{len(system_text)} 字符）",
                f"- 完整动态上下文：`captured_prompts/{user_name}`（{len(user_text)} 字符）",
                f"- 原始模型回答：`captured_prompts/{response_name}`（{len(response_text)} 字符）",
                f"- 完整messages：`captured_prompts/{messages_name}`",
                "",
            ]
        )
    review_lines.extend(
        [
            "## 代表性完整提示词",
            "",
            "下列条目是上方真实调用的便捷索引，没有删节：",
        ]
    )
    for phase_key in (
        "core_gm_decision",
        "core_gm_heartbeat",
        "core_gm_post_tool",
        "expressor_expression",
    ):
        selected = selected_phases.get(phase_key)
        if selected is None:
            continue
        review_lines.append(
            f"- `{phase_key}`：`captured_prompts/{selected[0]}`；动态上下文：`captured_prompts/{selected[1]}`"
        )
    (output_dir / "prompt_review.md").write_text(
        "\n".join(review_lines).rstrip() + "\n",
        encoding="utf-8",
    )


def setup_scenario(service: FUGMHttpService, campaign_id: str, session_id: str, channel_id: str) -> str:
    runtime = service._runtime(campaign_id)
    app = runtime.app
    app.world_state.apply_party_sheet(
        PartySheet(
            group_concept="揭露北境粮荒真相的巡行者",
            shared_goal="让被总督扣下的粮食送到北境灾民手中",
            starting_region="赤冠王国",
            members=[
                PartyMemberEntry(
                    player_name="阿凛",
                    hero_name="伊莉雅",
                    identity="守护灾民的流浪骑士",
                    theme="正义",
                    origin="北境灰谷",
                    classes={"守护者": 2, "武器大师": 2, "游说家": 1},
                )
            ],
        )
    )
    add_character(
        app,
        Character(
            name="伊莉雅",
            level=5,
            attributes={"DEX": 8, "INS": 10, "MIG": 8, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=45,
            mp=45,
            defenses={"physical": 11, "magic": 10},
            initiative=0,
            weapon_damage=10,
            equipped_main_hand="青铜剑",
            equipped_shield="青铜盾",
            traits=["pc", "守护灾民的流浪骑士", "正义", "北境灰谷"],
        ),
    )
    for name, initiative, defense in (
        ("王城卫兵长", 11, 11),
        ("王城盾卫", 8, 12),
    ):
        add_character(
            app,
            Character(
                name=name,
                level=10,
                attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 8},
                max_hp=70,
                hp=70,
                max_mp=50,
                mp=50,
                defenses={"physical": defense, "magic": 8},
                initiative=initiative,
                weapon_damage=10,
                traits=["enemy", "humanoid"],
            ),
        )
        app.conflict_manager.register_enemy(name, EnemyRank.SOLDIER)

    app.world_state.ensure_npc_persona(
        "赤冠王阿德里安",
        public_identity="赤冠王国的国王",
        role_in_story="掌握开仓与逮捕权的统治者",
        core_drive="保住王国秩序与王位，即使必须牺牲北境",
        manner="寡言、审慎，习惯先听证据再下令",
        speech_style="王者的措辞简洁明确，不说谜语，不重复法令",
        npc_rank="villain",
        leverage="可以下令开仓、调查总督或命卫兵扣押证物",
        authority_scope="只处理赤冠王国内的王令、王仓和王城卫队",
        knowledge_scope="知道北境粮车、总督账册和王城内部的公开政务",
        refusal_move="命卫兵扣押证据并逮捕拒绝服从的人",
        first_scene="赤金王座厅",
        goals=["拿到粮车清单原件", "避免当众承认王室纵容总督"],
        taboos=["不容许任何人在王座厅威胁公开王室丑闻"],
        secrets=["他早已知道总督扣粮，却担心开仓会引发边军倒戈"],
        current_location="赤金王座厅",
        current_mood="戒备",
        current_stance="愿意听证据，但不会轻易交出王仓控制权",
        active_goal="取得证据原件并阻止消息传到王宫外",
        voice_examples=["“把证据放下。说你亲眼见到的部分。”"],
    )
    app.world_state.ensure_npc_persona(
        "王城卫兵长",
        public_identity="守卫王座厅的卫兵长",
        role_in_story="执行国王命令的近卫",
        core_drive="维持王座厅秩序并保护国王",
        manner="冷静守纪",
        speech_style="命令简短，不威吓无关旁观者",
        combat_style="先封住出口，再压制持有证据的人；不攻击失去战意者",
        npc_rank="elite",
        known_skills=["防御精通", "挺身守护"],
        combat_actions=["盾击压制", "封锁出口"],
        current_location="赤金王座厅",
        active_goal="保护国王并收回证据",
    )
    app.world_state.ensure_npc_persona(
        "王城盾卫",
        public_identity="持塔盾的王城近卫",
        role_in_story="卫兵长的支援者",
        core_drive="服从卫兵长并守住王座阶梯",
        manner="沉默、训练有素",
        speech_style="很少开口，只报告战况",
        combat_style="保护卫兵长，优先防御与牵制",
        npc_rank="minor",
        known_skills=["保镖"],
        combat_actions=["塔盾推进"],
        current_location="赤金王座厅",
        active_goal="切断伊莉雅接近国王的路线",
    )

    # This probe supplies its own complete situation.  Do not inherit an
    # unrelated chapter contract from campaign bootstrap, otherwise the
    # captured prompt no longer represents the scenario under evaluation.
    app.story_arc_manager.state.current_pacing_plan.dramatic_contract = (
        SessionDramaticContract()
    )

    setup_message = (
        "伊莉雅已经进入赤金王座厅；赤冠王阿德里安、王城卫兵长和王城盾卫都在场。"
    )
    receipt = service.gm_runtime_tools.start_scene(
        tool_context(campaign_id, session_id, channel_id, setup_message),
        {
            "name": "赤金王座厅的请愿",
            "scene_type": "standard",
            "location": "赤金王座厅",
            "participants": ["伊莉雅", "赤冠王阿德里安", "王城卫兵长", "王城盾卫"],
            "objective": "说服国王开仓放粮，或决定如何处理王室阻挠",
            "private_situation": {
                "premise": "北境粮荒的证据已经被带到国王面前",
                "current_pressure": "宫门即将关闭，证据一旦被扣便很难再传出王宫",
                "visible_elements": ["空着的请愿席", "王座阶下两名近卫", "伊莉雅手中的粮车清单"],
                "clue_pool": ["国王看见总督印玺拓片时并不惊讶"],
                "secrets": ["国王早已知道总督扣粮", "卫兵长接到的密令是优先扣押清单原件"],
                "story_outline": ["国王先听取请愿", "证据触及王室责任", "拒绝交出原件会引发卫兵冲突"],
            },
            "public_opening": (
                "赤金王座厅的门在身后合拢。国王没有让伊莉雅久等，只抬手示意她把北境的证据呈上来；"
                "两名近卫一左一右守在王座阶下。"
            ),
            "player_handoff": "伊莉雅，国王正在等你开口，你先说什么？",
            "evidence": setup_message,
        },
    )
    if not receipt.ok:
        raise RuntimeError(f"场景预设失败：{receipt.error_code} {receipt.message}")
    service.session_gates.activate(
        campaign_id,
        channel_id,
        session_id,
        status="adventure",
        reason="NPC提示词连续场景实跑",
    )
    return receipt.public_fallback_reply


def run() -> Path:
    os.environ.setdefault("FU_GM_CORE_GM_TIMEOUT_SECONDS", "240")
    os.environ.setdefault("FU_GM_TOOL_AGENT_TIMEOUT_SECONDS", "240")
    os.environ.setdefault("FU_GM_CORE_GM_ENDPOINT_ATTEMPT_TIMEOUT_SECONDS", "120")
    os.environ.setdefault("FU_GM_ENDPOINT_ATTEMPT_TIMEOUT_SECONDS", "120")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = OUTPUT_ROOT / f"king_guard_{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)
    campaign_id = f"prompt_probe_king_guard_{stamp}"
    session_id = "audience-and-arrest"
    channel_id = "prompt-probe"
    transcript: list[dict[str, Any]] = []
    prompt_records: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="fu-gm-king-guard-") as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=True)
        opening = setup_scenario(service, campaign_id, session_id, channel_id)
        runtime = service._runtime(campaign_id)
        app = runtime.app
        install_capture_clients(service, app, prompt_records)
        transcript.append({"speaker": "时悠", "text": opening, "kind": "preset_opening"})

        def persist_artifacts() -> None:
            pending_windows = [
                asdict(window)
                for window in app.interceptor.decision_window_manager.pending()
            ]
            (output_dir / "prompt_calls.json").write_text(
                json.dumps(prompt_records, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (output_dir / "transcript.json").write_text(
                json.dumps(transcript, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (output_dir / "transcript.txt").write_text(
                "\n\n".join(
                    f"{item['speaker']}: {item.get('text', '')}"
                    for item in transcript
                    if item.get("text")
                )
                + "\n",
                encoding="utf-8",
            )
            (output_dir / "pending_rule_windows.json").write_text(
                json.dumps(pending_windows, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            write_prompt_review(output_dir, prompt_records)

        persist_artifacts()
        messages = [
            (
                "阿凛",
                "伊莉雅向国王行礼：“陛下，北境饥荒不是叛军造成的。总督扣下了粮车，请准我们查封他的仓库，把粮食还给灾民。”",
            ),
            (
                "阿凛",
                "伊莉雅把总督印玺拓片和粮车清单放上请愿席：“这里有车号、日期和收货人的印记。请您现在下令开仓。”",
            ),
            (
                "阿凛",
                "伊莉雅收回粮车清单：“查封与核验我都接受，但原件不能留在王宫。请让卫兵放我离开；核验时我会带原件到场。”",
            ),
            (
                "阿凛",
                "伊莉雅拔剑斩向挡住宫门的王城卫兵长，试图带着粮车清单冲出去。",
            ),
        ]

        def resolve_blocking_windows(after_message_index: int) -> None:
            for pending_index in range(4):
                blocking = [
                    window
                    for window in app.interceptor.decision_window_manager.pending()
                    if window.blocking
                ]
                if not blocking:
                    return
                transcript.append(
                    {
                        "speaker": "系统",
                        "text": json.dumps(
                            [asdict(window) for window in blocking],
                            ensure_ascii=False,
                        ),
                        "kind": "pending_rule_window",
                    }
                )
                if any(window.kind == "critical_opportunity" for window in blocking):
                    message = "伊莉雅把这次大成功的机会用于【优势】。"
                elif any(
                    window.kind in {
                        "trait_invocation",
                        "bond_invocation",
                        "lucky_seven",
                    }
                    for window in blocking
                ):
                    message = "伊莉雅接受当前检定结果，不援用特质、羁绊、幸运数字或其他重掷。"
                else:
                    raise RuntimeError(
                        "探针遇到不应自动替玩家选择的待决窗口："
                        + "、".join(window.kind for window in blocking)
                    )
                transcript.append(
                    {
                        "speaker": "阿凛",
                        "text": message,
                        "kind": "player_rule_window_response",
                    }
                )
                status, response = service.handle(
                    "POST",
                    "/v1/game/turn",
                    {
                        "campaign_id": campaign_id,
                        "session_id": session_id,
                        "channel_id": channel_id,
                        "speaker": "阿凛",
                        "message": message,
                        "message_id": (
                            f"probe-window-{after_message_index}-{pending_index}"
                        ),
                        "is_at_bot": False,
                    },
                )
                transcript.append(
                    {
                        "speaker": "时悠",
                        "text": str(response.get("reply") or ""),
                        "kind": "gm_rule_window",
                        "status": status,
                        "tool_trace": response.get("tool_trace") or [],
                    }
                )
                persist_artifacts()
            raise RuntimeError("探针在四次合法回应后仍有阻塞窗口。")

        for index, (speaker, message) in enumerate(messages, start=1):
            transcript.append({"speaker": speaker, "text": message, "kind": "player"})
            status, response = service.handle(
                "POST",
                "/v1/game/turn",
                {
                    "campaign_id": campaign_id,
                    "session_id": session_id,
                    "channel_id": channel_id,
                    "speaker": speaker,
                    "message": message,
                    "message_id": f"probe-{index}",
                    "is_at_bot": False,
                },
            )
            if status != 200:
                raise RuntimeError(f"第{index}条消息HTTP失败：{status} {response}")
            transcript.append(
                {
                    "speaker": "时悠",
                    "text": str(response.get("reply") or ""),
                    "kind": "gm",
                    "route": response.get("route"),
                    "tool_trace": response.get("tool_trace") or [],
                    "tool_receipts": response.get("tool_receipts") or [],
                }
            )
            persist_artifacts()
            resolve_blocking_windows(index)

        if not app.conflict_manager.state.active:
            fallback_message = "伊莉雅已经挥剑攻击王城卫兵长，卫兵拔剑迎战。"
            conflict = service.gm_runtime_tools.start_conflict(
                tool_context(campaign_id, session_id, channel_id, fallback_message),
                {
                    "scene_name": "赤金王座厅突围战",
                    "pcs": ["伊莉雅"],
                    "enemies": ["王城卫兵长", "王城盾卫"],
                    "leader": "伊莉雅",
                    "supporters": [],
                    "objective": "带着粮车清单冲出王座厅",
                    "public_opening": "卫兵长拔剑封住宫门，盾卫的塔盾同时压向伊莉雅。",
                    "evidence": fallback_message,
                },
            )
            transcript.append(
                {
                    "speaker": "时悠",
                    "text": conflict.public_fallback_reply,
                    "kind": "typed_conflict_fallback",
                    "ok": conflict.ok,
                    "error": conflict.message,
                }
            )
            persist_artifacts()
            if not conflict.ok:
                raise RuntimeError(f"冲突建立失败：{conflict.error_code} {conflict.message}")

        npc_turn_completed = False
        for step in range(8):
            actor = app.conflict_manager.state.current_actor()
            if not actor:
                break
            if actor == "伊莉雅":
                message = (
                    "伊莉雅举盾执行防御，不为其他生物提供掩护；"
                    "她把装有粮车清单的皮袋压在身后。"
                )
                status, response = service.handle(
                    "POST",
                    "/v1/game/turn",
                    {
                        "campaign_id": campaign_id,
                        "session_id": session_id,
                        "channel_id": channel_id,
                        "speaker": "阿凛",
                        "message": message,
                        "message_id": f"probe-combat-pc-{step}",
                        "is_at_bot": False,
                    },
                )
                transcript.extend(
                    [
                        {"speaker": "阿凛", "text": message, "kind": "player_combat"},
                        {
                            "speaker": "时悠",
                            "text": str(response.get("reply") or ""),
                            "kind": "gm_combat",
                            "status": status,
                            "tool_trace": response.get("tool_trace") or [],
                        },
                    ]
                )
                persist_artifacts()
                continue
            status, response = service.handle(
                "POST",
                "/v1/game/gm-beat",
                {
                    "campaign_id": campaign_id,
                    "session_id": session_id,
                    "channel_id": channel_id,
                    "speaker": "系统",
                    "message": (
                        f"执行当前敌方NPC【{actor}】的一个完整回合。"
                        "伊莉雅正带着粮车清单突围；卫兵奉命封住宫门并夺回证据。"
                    ),
                    "message_id": f"probe-combat-npc-{step}",
                    "force": True,
                },
            )
            transcript.append(
                {
                    "speaker": "时悠",
                    "text": str(response.get("reply") or ""),
                    "kind": "gm_combat_beat",
                    "actor": actor,
                    "status": status,
                    "ok": bool(response.get("ok")),
                    "error": str(response.get("agent_error") or ""),
                    "tool_trace": response.get("agent_trace") or [],
                    "tool_receipts": response.get("tool_receipts") or [],
                }
            )
            persist_artifacts()
            npc_turn_completed = any(
                item.get("tool_name") == "run_current_npc_turn" and item.get("ok")
                for item in list(response.get("tool_receipts") or [])
                if isinstance(item, dict)
            )
            if not npc_turn_completed:
                raise RuntimeError(
                    "核心GM未完成NPC战斗回合："
                    + str(response.get("agent_error") or response)
                )
            break
        if not npc_turn_completed:
            raise RuntimeError("探针没有走到由核心GM直接选择的敌方NPC回合。")

        snapshot = app.session_zero_summary(include_private=True)
        persist_artifacts()
        (output_dir / "final_snapshot.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        report = {
            "campaign_id": campaign_id,
            "model": os.environ.get("FU_GM_ACTION_MODEL", ""),
            "prompt_call_count": len(prompt_records),
            "prompt_call_labels": [record["label"] for record in prompt_records],
            "nested_npc_model_calls": [
                record["label"]
                for record in prompt_records
                if record["label"] in {"npc_dialogue", "npc_combat"}
            ],
            "single_core_gm_npc_path": not any(
                record["label"] in {"npc_dialogue", "npc_combat"}
                for record in prompt_records
            ),
            "npc_turn_completed": npc_turn_completed,
            "conflict_active": app.conflict_manager.state.active,
            "current_actor": app.conflict_manager.state.current_actor(),
            "transcript_entries": len(transcript),
            "output_dir": str(output_dir),
        }
        (output_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return output_dir


if __name__ == "__main__":
    print(run())
