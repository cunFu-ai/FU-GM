from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from fu_gm.http_server import FUGMHttpService  # noqa: E402
from fu_gm.models import HeroDraft  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="运行旧版第零章到第一章恢复长测。",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    parse_args(argv)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_root = PROJECT_ROOT / ".runtime" / "large_tests" / f"session0_ch1_recovery_{stamp}"
    campaign_root = run_root / "campaigns"
    map_root = run_root / "maps"
    run_root.mkdir(parents=True, exist_ok=True)
    os.environ["FU_GM_PROJECT_DIR"] = str(PROJECT_ROOT)
    os.environ["FU_GM_NORTANTIS_OUTPUT_DIR"] = str(map_root)

    campaign_id = f"恢复长跑_第零章到第一章_{stamp}"
    session_id = "session0-to-chapter1-recovery"
    channel_id = "codex-real-api-long-test"
    service = FUGMHttpService(data_root=campaign_root, use_llm=True)
    calls: list[dict[str, Any]] = []

    def invoke(label: str, method: str, route: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        status, body = service.handle(method, route, payload or {})
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        record = {
            "index": len(calls) + 1,
            "label": label,
            "method": method,
            "route": route,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "ok": bool(body.get("ok", status < 400)),
            "speaker": str((payload or {}).get("speaker") or ""),
            "message": str((payload or {}).get("message") or ""),
            "reply": str(body.get("reply") or ""),
            "body": body,
        }
        calls.append(record)
        print(f"[{record['index']:02d}] {label}: {status} / {elapsed_ms}ms / ok={record['ok']}", flush=True)
        return body

    common = {"campaign_id": campaign_id, "session_id": session_id, "channel_id": channel_id}
    invoke("新建战役", "POST", "/v1/campaigns/new", {"campaign_id": campaign_id})
    invoke("会话门控进入第零章", "POST", "/v1/session/gate", {**common, "status": "session_zero"})
    invoke(
        "第零章开场",
        "POST",
        "/v1/session-zero/start",
        {**common, "participants": ["阿凛", "白河", "南星"]},
    )

    session_zero_turns = [
        (
            "阿凛",
            "地图卡是一片类地球大陆：主要陆地叫绯雨大陆，西侧鸦羽山脉，中央镜线内海，"
            "东南潮鸢群岛，南岸是白花碑与终年不散的雾潮。魔法与科技并存，灵魂晶炉驱动车辆和工坊，"
            "古老精灵术则被视为有意志的自然。赤羽旧王国的遗民守护白花碑；三十年前碎月坠落，旧王都在一夜间消失。"
            "我想知道碎月遗迹为何回应英雄羁绊而不是贵族血统。辉钢财团正在收购灰晶病患者的记忆制造魔导兵器。",
        ),
        (
            "白河",
            "钟鸣公国位于镜线内海北岸，正午钟声能安抚灵魂；碎月之夜大钟慢了一拍，所有人都听见未来的哭声。"
            "小队是护送碎月遗物的临时守护者。界限是不详细描写酷刑与性暴力；儿童遇险、身体病变和亲密内容淡出。"
            "我希望故事有英雄气质、希望与悲剧反派，但不走绝望到底的路线。",
        ),
        (
            "南星",
            "潮鸢群岛信奉会迁徙的海风神，擅长制造小型飞翼船；每年归潮祭后都会少一座岛，却没有人记得消失的是哪座。"
            "苍白司教团宣称灰晶病是灵魂升格的祝福，正在诱导病人交出记忆。第一幕从白花碑驿站开始："
            "小队护送一名病人和碎月遗物，辉钢财团的收购队已经抵达。",
        ),
    ]
    for index, (speaker, message) in enumerate(session_zero_turns, start=1):
        invoke(
            f"第零章共创 {index} {speaker}",
            "POST",
            "/v1/session-zero/message",
            {**common, "speaker": speaker, "message": message},
        )

    runtime = service._runtime(campaign_id)
    app = runtime.app
    world = app.world_state.world_profile
    world.campaign_title = "绯雨大陆：碎月回声"
    world.continent_name = "绯雨大陆"
    world.world_style = "科技奇幻为主，融合史诗奇幻与海洋自然奇幻"
    world.map_card = "类地球大陆地图：西侧鸦羽山脉、中央镜线内海、东南潮鸢群岛、南岸雾潮海岸"
    world.magic_tech_role = "灵魂晶炉支撑工业与交通，古老精灵术维持自然和灵魂循环。"
    world.starting_region = "白花碑驿站"
    world.group_concept = "护送碎月遗物、调查灰晶病的临时守护者"
    world.major_locations.update(
        {
            "白花碑驿站": "雾潮南岸的边境驿站，第一幕从这里开始。",
            "钟鸣公国": "位于镜线内海北岸，以安魂钟与灵魂工艺闻名。",
            "潮鸢群岛": "东南海域的飞翼船群岛，每年都会失去一座无人记得的岛。",
            "鸦羽山脉": "大陆西侧的古代遗迹带，碎月坠落后出现异常回声。",
        }
    )
    world.kingdoms.update(
        {
            "钟鸣公国": "正午钟声可安抚灵魂，钟匠与晶炉工匠掌握政治话语权。",
            "赤羽遗民": "散居雾潮南岸，守护白花碑与失落王都的记忆。",
            "潮鸢群岛": "信奉迁徙海风神，以飞翼船维系各岛。",
        }
    )
    app.world_state.upsert_map_location(
        "镜线内海", description="大陆中央的狭长内海。", feature_type="inland_sea", position_hint="center", draw_icon=False
    )
    app.world_state.upsert_map_location(
        "钟鸣公国", description=world.major_locations["钟鸣公国"], feature_type="country",
        relative_to="镜线内海", relative_position="north", faction="钟鸣公国", draw_icon=False
    )
    app.world_state.upsert_map_location(
        "潮鸢群岛", description=world.major_locations["潮鸢群岛"], feature_type="archipelago",
        position_hint="southeast", faction="潮鸢群岛", draw_icon=False
    )
    app.world_state.upsert_map_location(
        "鸦羽山脉", description=world.major_locations["鸦羽山脉"], feature_type="mountain_range",
        position_hint="west", draw_icon=False
    )
    app.world_state.upsert_map_location(
        "雾潮海岸", description="绯雨大陆南岸的终年雾海。", feature_type="coast", position_hint="south", draw_icon=False
    )
    app.world_state.upsert_map_location(
        "白花碑驿站", description=world.major_locations["白花碑驿站"], feature_type="settlement",
        relative_to="雾潮海岸", relative_position="north", faction="赤羽遗民", draw_icon=True
    )
    world.hero_drafts = _validated_hero_drafts()
    validation_results: dict[str, dict[str, Any]] = {}
    for draft_key in world.hero_drafts:
        validation = app.validate_hero_draft(draft_key)
        validation_results[draft_key] = {
            "ready": validation.ready,
            "missing_fields": list(validation.missing_fields),
            "errors": list(validation.errors),
            "warnings": list(validation.warnings),
        }
    created = app.create_confirmed_player_characters_from_drafts()
    candidates = app.session_zero_manager.generate_first_act_candidates()
    selected = app.session_zero_manager.confirm_first_act(candidates[0].candidate_id if candidates else "")
    # This is the regression condition: the table starts before every Session 0 completion flag is satisfied.
    world.completed = False
    app.world_map_manager.sync_from_world_state()
    runtime.log_manager.append_message(
        campaign_id,
        session_id,
        speaker="测试系统",
        content=(
            "长跑测试已用规则校验器固定三张合法角色卡，并刻意保留 Session 0 completed=False；"
            "接下来通过正式冒险门控验证地图生成、内部恢复和实时上下文。"
        ),
        role="system",
        channel_id=channel_id,
        metadata={"validation": validation_results, "created": sorted(created)},
    )
    service._autosave_campaign(runtime, campaign_id)
    completed_before_adventure = world.completed

    gate_body = invoke(
        "确认进入冒险并生成地图",
        "POST",
        "/v1/session/gate",
        {**common, "status": "adventure", "reason": "玩家确认现在开始第一章"},
    )

    chapter_turns = [
        (
            "阿凛",
            "我们准备好了，请时悠把第一章的镜头打开到白花碑驿站。伊莉雅把碎月遗物固定在盾后，先等你描述现场局势。",
        ),
        (
            "阿凛",
            "伊莉雅先停在驿站入口，观察周围有没有被跟踪、封锁或埋伏的迹象；如果有风险，我愿意检定。",
        ),
        (
            "白河",
            "洛岚听完现场动静后，把工具箱按住，想找这里最懂钟声或晶炉的人问几句：最近有没有听见钟声慢一拍？",
        ),
        (
            "南星",
            "赛璃先照看我们护送的那名灰晶病旅人，避开病变细节，只轻声问他：最后是谁提过要收购你的记忆？",
        ),
        (
            "阿凛",
            "伊莉雅愿意消耗1点物语点补一个世界细节：白花碑驿站由赤羽遗民的白花守望会管理，"
            "他们会把每个失去的名字刻在风铃内侧。随后她想向守望会请求一条避开财团关卡的路线。",
        ),
        (
            "白河",
            "洛岚把刚才听到的钟声异常、晶炉反应和旅人的证词并在一起，想判断财团下一步更可能去钟鸣公国还是潮鸢群岛；"
            "如果需要检定，请告诉我使用什么属性和难度等级。",
        ),
        (
            "南星",
            "赛璃看向另外两人，提议继续护送旅人与碎月遗物前往钟鸣公国。"
            "如果你觉得这个场景的压力已经暂时落定，我们想用每人一句同行理由作为第一章收束。",
        ),
    ]
    for index, (speaker, message) in enumerate(chapter_turns, start=1):
        invoke(
            f"第一章回合 {index} {speaker}",
            "POST",
            "/v1/game/turn",
            {**common, "speaker": speaker, "message": message},
        )

    invoke(
        "第一章收团",
        "POST",
        "/v1/session/end",
        {**common, "title": "第一章：白花碑驿站的迟响"},
    )
    audit_route = "/v1/audit/dashboard?" + urlencode(
        {
            "campaign_id": campaign_id,
            "session_id": session_id,
            "channel_id": channel_id,
            "include_private": "true",
            "limit": "200",
        }
    )
    audit = invoke("读取审计仪表盘", "GET", audit_route)

    report = _build_report(
        run_root=run_root,
        campaign_id=campaign_id,
        session_id=session_id,
        channel_id=channel_id,
        calls=calls,
        audit=audit,
        gate_body=gate_body,
        completed_before_adventure=completed_before_adventure,
        validation_results=validation_results,
        created_characters=sorted(result.character.name for result in created.values()),
        selected_first_act=selected.title if selected else "",
        app=app,
    )
    report_json = run_root / "large_test_report.json"
    report_txt = run_root / "large_test_report.txt"
    report_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=lambda value: getattr(value, "value", str(value))),
        encoding="utf-8",
    )
    report_txt.write_text(_format_report(report), encoding="utf-8")
    print(f"REPORT_JSON={report_json}")
    print(f"REPORT_TXT={report_txt}")
    print(f"TRANSCRIPT_TXT={report['artifacts']['transcript_txt']}")
    print(f"MAP_PATH={report['map']['output_path']}")
    return 1 if report["errors"] else 0


def _validated_hero_drafts() -> dict[str, HeroDraft]:
    return {
        "阿凛": HeroDraft(
            player_name="阿凛",
            hero_name="伊莉雅",
            identity="赤羽遗民的盾卫",
            theme="使命",
            origin="白花碑",
            classes={"守护者": 3, "武器大师": 2},
            attributes={"DEX": 8, "INS": 6, "MIG": 10, "WLP": 8},
            bonds=["洛岚：信赖"],
            skills={"铁壁": 1, "保镖": 1, "挺身守护": 1, "近战武器精通": 1, "破防打击": 1},
            equipment=["青铜剑", "青铜盾", "旅行装束"],
            notes=["她发誓不让碎月遗物再次被权力垄断。"],
            confirmed=True,
        ),
        "白河": HeroDraft(
            player_name="白河",
            hero_name="洛岚",
            identity="钟鸣公国的流亡钟匠",
            theme="愧疚",
            origin="镜线内海北岸",
            classes={"造物使": 3, "旅人": 2},
            attributes={"DEX": 8, "INS": 10, "MIG": 6, "WLP": 8},
            bonds=["伊莉雅：钦佩"],
            skills={"便携装置": 1, "秘密配方": 1, "先见之明": 1, "宝物猎人": 1, "见多识广": 1},
            skill_options={"便携装置": ["魔导装置"]},
            equipment=["钢匕首", "旅行装束"],
            notes=["他曾参与过灰晶晶炉的早期设计。"],
            confirmed=True,
        ),
        "南星": HeroDraft(
            player_name="南星",
            hero_name="赛璃",
            identity="潮鸢群岛的御魂使航手",
            theme="希望",
            origin="潮鸢群岛",
            classes={"御魂使": 3, "游说家": 2},
            attributes={"DEX": 8, "INS": 8, "MIG": 6, "WLP": 10},
            bonds=["洛岚：信赖"],
            skills={"灵魂魔法": 1, "御魂系仪式": 1, "治愈之力": 1, "鼓舞": 1, "予以信任": 1},
            spells=["治愈术"],
            equipment=["法杖", "旅行装束"],
            notes=["她相信消失的岛屿仍在灵魂之河的某处航行。"],
            confirmed=True,
        ),
    }


def _build_report(**context: Any) -> dict[str, Any]:
    app = context.pop("app")
    audit = context["audit"]
    calls = context["calls"]
    gate_body = context["gate_body"]
    errors = [
        {
            "label": call["label"],
            "status": call["status"],
            "error": str(call["body"].get("error") or "请求返回 ok=false"),
        }
        for call in calls
        if call["status"] >= 400 or not call["ok"]
    ]
    degraded_turns = [
        {
            "label": call["label"],
            "reply": call["reply"],
        }
        for call in calls
        if "模型暂时没有接上" in call["reply"]
    ]
    map_status = dict(gate_body.get("world_map") or {})
    output_path = str(map_status.get("output_path") or "")
    map_events = [
        asdict(event)
        for event in app.world_state.memory_events
        if event.kind in {"world_map_visual", "world_map_visual_error"}
    ]
    transcript_txt = Path(audit.get("logs", {}).get("transcript_txt_path") or "")
    world = app.world_state.world_profile
    all_session_zero_replies = "\n".join(
        call["reply"] for call in calls if call["route"] == "/v1/session-zero/message"
    )
    return {
        **context,
        "errors": errors,
        "degraded_turns": degraded_turns,
        "map": {
            "gate_status": map_status,
            "output_path": output_path,
            "exists": bool(output_path and Path(output_path).exists()),
            "events": map_events,
        },
        "assertions": {
            "session_zero_incomplete_before_adventure": context["completed_before_adventure"] is False,
            "map_generated_before_first_turn": map_status.get("status") in {"generated", "ready"},
            "map_file_exists": bool(output_path and Path(output_path).exists()),
            "travel_day_not_player_configured": not bool(world.travel_day_length),
            "no_world_shape_question": "世界形状" not in all_session_zero_replies,
            "no_travel_day_length_question": "旅行日长度" not in all_session_zero_replies,
            "three_formal_characters": len(app.character_manager.all()) == 3,
            "in_play_world_detail_persisted": any(
                "白花守望会" in event.summary for event in app.world_state.memory_events
            ),
            "no_unrecovered_model_turns": not degraded_turns,
        },
        "recovery_events": [
            asdict(event)
            for event in app.world_state.memory_events
            if event.kind in {"character_recovery", "world_map_visual_error"}
        ],
        "latency": {
            "slowest_calls": sorted(calls, key=lambda item: item["elapsed_ms"], reverse=True)[:12],
            "http": audit.get("runtime", {}).get("http", {}),
            "pipeline": audit.get("runtime", {}).get("pipeline", {}),
            "llm": audit.get("llm", {}),
        },
        "artifacts": {
            "run_root": str(context["run_root"]),
            "transcript_txt": str(transcript_txt),
            "transcript_txt_exists": transcript_txt.exists(),
            "snapshot": str(audit.get("runtime", {}).get("last_saved_path") or ""),
        },
    }


def _format_report(report: dict[str, Any]) -> str:
    lines = [
        "真实长跑测试报告：第零章到第一章结束",
        f"campaign_id: {report['campaign_id']}",
        f"session_id: {report['session_id']}",
        f"data_root: {report['run_root']}",
        "",
        "=== 关键结论 ===",
        f"错误数量: {len(report['errors'])}",
        f"重试后仍降级的模型轮次: {len(report['degraded_turns'])}",
        f"冒险前 Session 0 completed: {report['completed_before_adventure']}",
        f"正式角色: {', '.join(report['created_characters'])}",
        f"第一幕: {report['selected_first_act']}",
        f"地图状态: {json.dumps(report['map']['gate_status'], ensure_ascii=False)}",
        f"地图文件存在: {report['map']['exists']}",
        f"地图路径: {report['map']['output_path']}",
        f"完整对话 TXT: {report['artifacts']['transcript_txt']}",
        "",
        "=== 验收断言 ===",
    ]
    for key, value in report["assertions"].items():
        lines.append(f"{key}: {value}")
    if report["errors"]:
        lines.extend(["", "=== 错误 ==="])
        for error_item in report["errors"]:
            lines.append(json.dumps(error_item, ensure_ascii=False))
    if report["degraded_turns"]:
        lines.extend(["", "=== 重试后仍降级的模型轮次 ==="])
        for degraded in report["degraded_turns"]:
            lines.append(json.dumps(degraded, ensure_ascii=False))

    lines.extend(["", "=== 最慢调用 ==="])
    for call in report["latency"]["slowest_calls"]:
        lines.append(
            f"{call['elapsed_ms']}ms | {call['label']} | {call['method']} {call['route']} | "
            f"status={call['status']} ok={call['ok']}"
        )

    lines.extend(["", "=== HTTP 路由延迟 ==="])
    for route, metrics in report["latency"]["http"].get("by_route", {}).items():
        lines.append(
            f"{route}: count={metrics.get('count', 0)}, average={metrics.get('average_ms', 0)}ms, "
            f"max={metrics.get('max_ms', 0)}ms"
        )

    lines.extend(["", "=== 游戏回合流水线 ==="])
    for span in report["latency"]["pipeline"].get("recent_turns", []):
        lines.append(
            " | ".join(
                [
                    f"action={span.get('action_type', '')}",
                    f"total={span.get('total_ms', 0)}ms",
                    f"panel={span.get('build_panel_ms', 0)}ms",
                    f"core_gm={span.get('core_gm_ms', 0)}ms",
                    f"rules={span.get('rules_ms', 0)}ms",
                    f"memory={span.get('memory_writeback_ms', 0)}ms",
                    f"expressor={span.get('expressor_ms', 0)}ms",
                    f"recovery={json.dumps(span.get('recovery', []), ensure_ascii=False)}",
                ]
            )
        )

    lines.extend(["", "=== LLM 延迟 ==="])
    for component in (
        "action_client",
        "expressor_client",
        "session_zero_client",
        "casual_client",
        "summarizer_client",
    ):
        payload = report["latency"]["llm"].get(component, {})
        lines.append(
            f"{component}: calls={payload.get('total_calls', 0)}, "
            f"recent_average={payload.get('average_recent_elapsed_ms', 0)}ms"
        )
        for call in payload.get("slowest_recent", [])[:3]:
            lines.append(
                f"  {call.get('elapsed_ms', 0)}ms | model={call.get('model', '')} | "
                f"ok={call.get('ok', False)} | error={call.get('error', '')}"
            )

    lines.extend(["", "=== 完整 API 对话 ==="])
    for call in report["calls"]:
        lines.append(
            f"\n--- {call['index']:02d}. {call['label']} | {call['elapsed_ms']}ms | "
            f"status={call['status']} ok={call['ok']} ---"
        )
        if call["message"]:
            lines.append(f"{call['speaker']}: {call['message']}")
        if call["reply"]:
            lines.append(f"时悠: {call['reply']}")
        elif call["body"].get("error"):
            lines.append(f"error: {call['body']['error']}")
        elif call["label"] == "第一章收团":
            lines.append("summary: " + json.dumps(call["body"].get("summary", {}), ensure_ascii=False))
        elif call["label"] == "读取审计仪表盘":
            lines.append("审计仪表盘已读取；完整结构保存在 large_test_report.json。")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
