from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from fu_gm.action_brain import HeuristicActionBrain
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.dungeon_manager import DungeonManager
from fu_gm.components.rest_manager import RestManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.session_zero_manager import SessionZeroManager
from fu_gm.components.travel_manager import TravelManager
from fu_gm.components.world_state import WorldState
from fu_gm.config import LLMConfig
from fu_gm.expressor import Expressor
from fu_gm.interceptor import ActionInterceptor
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.scene_orchestrator import SceneOrchestrator
from fu_gm.session_zero_facilitator import HeuristicSessionZeroFacilitator, LLMSessionZeroFacilitator


def build_session_zero_app(
    *,
    use_llm: bool = True,
    model: str = "",
    reasoning_effort: str = "",
    thinking_enabled: bool | None = None,
    gm_personality_prompt: str = "",
    deepseek_roleplay_mode: str = "default",
) -> SceneOrchestrator:
    characters = CharacterManager()
    clocks = ClockManager()
    conflict = ConflictManager(characters)
    scene_manager = SceneManager()
    world_state = WorldState()
    rules = RulesEngine(seed=0)
    interceptor = ActionInterceptor(
        rules_engine=rules,
        character_manager=characters,
        clock_manager=clocks,
        conflict_manager=conflict,
        world_state=world_state,
    )
    fallback_facilitator = HeuristicSessionZeroFacilitator()
    facilitator = fallback_facilitator
    if use_llm:
        config = LLMConfig.from_env()
        if model:
            config.action_model = model
        if reasoning_effort:
            config.reasoning_effort = reasoning_effort
        if thinking_enabled is not None:
            config.thinking_enabled = thinking_enabled
        if config.api_key:
            facilitator = LLMSessionZeroFacilitator(
                client=OpenAICompatibleClient(config),
                model=config.action_model,
                fallback=fallback_facilitator,
                gm_personality_prompt=gm_personality_prompt,
                deepseek_roleplay_mode=deepseek_roleplay_mode,
                allow_fallback=config.allow_heuristic_fallback,
            )

    return SceneOrchestrator(
        action_brain=HeuristicActionBrain(),
        character_manager=characters,
        clock_manager=clocks,
        conflict_manager=conflict,
        world_state=world_state,
        interceptor=interceptor,
        expressor=Expressor(),
        scene_manager=scene_manager,
        session_zero_manager=SessionZeroManager(world_state),
        session_zero_facilitator=facilitator,
        rest_manager=RestManager(characters, clocks),
        travel_manager=TravelManager(rules),
        dungeon_manager=DungeonManager(clocks),
    )


def main() -> None:
    # 先读取 .env，确保 argparse 默认值能拿到 Session 0 专用配置。
    LLMConfig.from_env()
    parser = argparse.ArgumentParser(description="交互式测试《最终物语》Session 0 世界创建流程。")
    parser.add_argument(
        "--participants",
        nargs="*",
        default=[],
        help="玩家名列表，例如：--participants 阿凛 白河",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="强制使用本地启发式主持器，不调用真实 LLM。",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="使用更快的 Session 0 配置：deepseek-v4-flash、低推理强度、关闭 thinking。",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("FU_GM_SESSION_ZERO_MODEL", ""),
        help="Session 0 单独使用的模型名，例如 deepseek-v4-flash 或 deepseek-v4-pro。",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=os.environ.get("FU_GM_SESSION_ZERO_REASONING_EFFORT", ""),
        choices=["", "low", "medium", "high"],
        help="Session 0 单独使用的 reasoning_effort。",
    )
    parser.add_argument(
        "--thinking",
        choices=["default", "on", "off"],
        default=os.environ.get("FU_GM_SESSION_ZERO_THINKING", "default"),
        help="Session 0 是否启用 DeepSeek thinking。",
    )
    parser.add_argument(
        "--rp-mode",
        choices=["default", "inner_os", "analysis"],
        default=os.environ.get("FU_GM_DEEPSEEK_ROLEPLAY_MODE", "default"),
        help="DeepSeek V4 思考模式提示：default 不注入，inner_os 角色沉浸，analysis 纯分析。",
    )
    parser.add_argument(
        "--gm-style-file",
        default=os.environ.get("FU_GM_STYLE_FILE", ""),
        help="GM 性格文档路径；文件内容会注入 Session 0 主持器。",
    )
    parser.add_argument(
        "--log-file",
        default=os.environ.get("FU_GM_SESSION_ZERO_LOG_FILE", ""),
        help="Session 0 JSONL 日志路径；默认写入 logs/session_zero_时间戳.jsonl。",
    )
    parser.add_argument(
        "--show-structure",
        action="store_true",
        help="显示 accepted_facts、suggestions、questions 等结构化调试字段；默认只显示 GM 回复。",
    )
    args = parser.parse_args()

    model = args.model
    reasoning_effort = args.reasoning_effort
    thinking_enabled = _parse_thinking_flag(args.thinking)
    if args.fast:
        model = model or "deepseek-v4-flash"
        reasoning_effort = reasoning_effort or "low"
        thinking_enabled = False
    gm_personality_prompt = _load_gm_style(args.gm_style_file)
    app = build_session_zero_app(
        use_llm=not args.offline,
        model=model,
        reasoning_effort=reasoning_effort,
        thinking_enabled=thinking_enabled,
        gm_personality_prompt=gm_personality_prompt,
        deepseek_roleplay_mode=args.rp_mode,
    )
    participants = [name.strip() for name in args.participants if name.strip()]
    log_path = _resolve_log_path(args.log_file)
    opening = app.start_session_zero(participants=participants or None)
    print("=== FU-GM Session 0 交互测试 ===")
    print("输入格式：玩家名: 发言。没有冒号时会使用当前轮询玩家或“玩家”。")
    print(
        "命令：/snapshot /missing /summary /first-act /vote <1|2|3> /first-act-confirm [1|2|3] "
        "/secrets /confirm <草稿名> /create /export <目录> /save <战役ID> /exit"
    )
    print(f"日志：{log_path}")
    print()
    _print_response(opening, show_structure=args.show_structure)
    _append_log(log_path, app, event="opening", response=opening)

    while True:
        try:
            raw = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已结束 Session 0 交互。")
            return
        if not raw:
            continue
        if raw in {"/exit", "exit", "quit", "退出"}:
            print("已结束 Session 0 交互。")
            return
        if raw == "/snapshot":
            print(json.dumps(app.session_zero_snapshot(), ensure_ascii=False, indent=2))
            _append_log(log_path, app, event="command", command=raw)
            continue
        if raw == "/missing":
            missing = app.session_zero_snapshot().get("missing_topics", [])
            print("缺失项：" + ("、".join(missing) if missing else "无，Session 0 核心素材已齐。"))
            _append_log(log_path, app, event="command", command=raw)
            continue
        if raw == "/summary":
            print(app.format_session_zero_summary(include_private=True))
            _append_log(log_path, app, event="command", command=raw)
            continue
        if raw == "/first-act":
            if not app.session_zero_manager.state.world.first_act_candidates:
                app.session_zero_manager.generate_first_act_candidates()
            result = app.session_zero_manager.first_act_vote_result()
            print(app.session_zero_manager.prologue_manager.format_candidates(result.candidates))
            print("投票：" + json.dumps(result.vote_counts, ensure_ascii=False))
            print("当前领先：" + result.summary)
            _append_log(log_path, app, event="command", command=raw, result=app.session_zero_manager.snapshot())
            continue
        if raw.startswith("/vote "):
            candidate = raw.removeprefix("/vote ").strip()
            speaker = app.session_zero_manager.current_participant_name() or "玩家"
            app.session_zero_manager.record_first_act_vote(speaker, candidate)
            result = app.session_zero_manager.first_act_vote_result()
            print("已记录投票。当前领先：" + result.summary)
            _append_log(log_path, app, event="command", command=raw, result=app.session_zero_manager.snapshot())
            continue
        if raw.startswith("/first-act-confirm"):
            candidate = raw.removeprefix("/first-act-confirm").strip()
            winner = app.session_zero_manager.confirm_first_act(candidate)
            print("第一幕已确认：" + (winner.title if winner else "尚无可确认候选"))
            _append_log(log_path, app, event="command", command=raw, result=app.session_zero_manager.snapshot())
            continue
        if raw == "/secrets":
            report = app.session_zero_manager.gm_secret_audit_report(include_content=True)
            print(json.dumps(app.session_zero_manager._jsonable(report), ensure_ascii=False, indent=2))
            _append_log(log_path, app, event="command", command=raw)
            continue
        if raw.startswith("/confirm "):
            draft_key = raw.removeprefix("/confirm ").strip()
            result = app.confirm_hero_draft(draft_key)
            _print_validation(draft_key, result)
            _append_log(log_path, app, event="command", command=raw, result=_validation_payload(result))
            continue
        if raw == "/create":
            results = app.create_confirmed_player_characters_from_drafts()
            if not results:
                print("还没有已确认的角色草稿。先用 /confirm <草稿名> 确认。")
                _append_log(log_path, app, event="command", command=raw, result={"created": []})
                continue
            for draft_key, result in results.items():
                print(f"{draft_key}: 成功 - 已创建【{result.character.name}】，初始泽尼特 {result.starting_zenit}")
            _append_log(
                log_path,
                app,
                event="command",
                command=raw,
                result={"created": [result.character.name for result in results.values()]},
            )
            continue
        if raw.startswith("/export "):
            directory = raw.removeprefix("/export ").strip()
            bundle = app.finalize_campaign_creation()
            result = app.write_campaign_sheets(Path(directory), bundle)
            print(f"已导出：{result.output_dir}")
            _append_log(log_path, app, event="command", command=raw, result={"output_dir": str(result.output_dir)})
            continue
        if raw.startswith("/save "):
            campaign_id = raw.removeprefix("/save ").strip()
            path = app.save_campaign_memory(campaign_id)
            print(f"已保存战役记忆：{path}")
            _append_log(log_path, app, event="command", command=raw, result={"path": str(path)})
            continue

        speaker, message = _parse_speaker(raw, app)
        response = app.discuss_session_zero(speaker, message)
        _print_response(response, show_structure=args.show_structure)
        _append_log(log_path, app, event="player_turn", speaker=speaker, message=message, response=response)


def _parse_speaker(raw: str, app: SceneOrchestrator) -> tuple[str, str]:
    if ":" in raw:
        speaker, message = raw.split(":", 1)
        return speaker.strip() or "玩家", message.strip()
    if "：" in raw:
        speaker, message = raw.split("：", 1)
        return speaker.strip() or "玩家", message.strip()
    speaker = app.session_zero_manager.current_participant_name() or "玩家"
    return speaker, raw


def _parse_thinking_flag(value: str) -> bool | None:
    if value == "on":
        return True
    if value == "off":
        return False
    return None


def _load_gm_style(path_text: str) -> str:
    if not path_text:
        return ""
    path = Path(path_text).expanduser()
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"无法读取 GM 性格文档：{path}；{exc}") from exc


def _resolve_log_path(path_text: str) -> Path:
    if path_text:
        path = Path(path_text).expanduser()
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = Path("logs") / f"session_zero_{stamp}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append_log(path: Path, app: SceneOrchestrator, *, event: str, **payload) -> None:
    facilitator = app.session_zero_facilitator
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        "payload": _json_safe(payload),
        "runtime": {
            "facilitator": type(facilitator).__name__,
            "model": getattr(facilitator, "model", ""),
            "used_fallback": getattr(facilitator, "last_used_fallback", False),
            "fallback_error": getattr(facilitator, "last_error", ""),
        },
        "snapshot": app.session_zero_snapshot(),
        "summary": app.session_zero_summary(include_private=True),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _response_payload(response) -> dict:
    return {
        "message": response.message,
        "stage": response.stage.value,
        "accepted_facts": list(response.accepted_facts),
        "suggestions": list(response.suggestions),
        "questions": list(response.questions),
        "world_updates": response.world_updates,
    }


def _validation_payload(result) -> dict:
    return {
        "draft_key": result.draft_key,
        "ready": result.ready,
        "missing_fields": list(result.missing_fields),
        "errors": list(result.errors),
        "warnings": list(result.warnings),
    }


def _json_safe(value):
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return {key: _json_safe(getattr(value, key)) for key in value.__dataclass_fields__}
    return value


def _print_response(response, *, show_structure: bool = False) -> None:
    print(f"GM：{response.message}")
    if show_structure:
        if response.accepted_facts:
            print("已确认：" + "；".join(response.accepted_facts))
        if response.suggestions:
            print("建议：" + "；".join(response.suggestions))
        if response.questions:
            print("问题：" + "；".join(response.questions))
    print(f"阶段：{response.stage.value}")


def _print_validation(draft_key: str, result) -> None:
    status = "可创建" if result.ready else "仍需补充"
    print(f"角色草稿【{draft_key}】：{status}")
    if result.errors:
        print("错误：" + "；".join(result.errors))
    if result.warnings:
        print("提醒：" + "；".join(result.warnings))
    if result.missing_fields:
        print("缺失：" + "、".join(result.missing_fields))


if __name__ == "__main__":
    main()
