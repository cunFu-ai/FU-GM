from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any
from urllib.parse import urlencode


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from fu_gm.components.clock_manager import ClockManager  # noqa: E402
from fu_gm.components.chapter_manager import ChapterManager  # noqa: E402
from fu_gm.components.conflict_manager import ConflictManager, EnemyRank  # noqa: E402
from fu_gm.components.dungeon_manager import DungeonManager  # noqa: E402
from fu_gm.components.economy_manager import EconomyManager  # noqa: E402
from fu_gm.components.encounter_manager import EncounterManager  # noqa: E402
from fu_gm.components.rules_engine import RulesEngine  # noqa: E402
from fu_gm.models import (  # noqa: E402
    Affinity,
    ChapterPackage,
    ChapterPackageScene,
    Character,
    Clock,
    DungeonExploreMode,
    DungeonImportance,
    DungeonPreparation,
    EncounterDifficulty,
    PersistentChangeType,
    ProjectUse,
    RestType,
    RitualPotency,
    RitualScope,
    TravelRouteType,
    TravelThreatLevel,
)
from fu_gm.spellbook import canonical_spell_names, normalize_spell_name, spell_matching_candidates  # noqa: E402
from fu_gm.http_server import FUGMHttpService  # noqa: E402


class FromScratchUltraHarness:
    """Runs a long real-service smoke test through the public HTTP boundary.

    The test intentionally does not inject prebuilt PC sheets. Player characters
    are provided as Session 0 table speech, confirmed through the same route a
    user would use, and then gated into Chapter 1.
    """

    def __init__(self) -> None:
        self.stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_root = PROJECT_ROOT / ".runtime" / "large_tests" / f"ultra_from_scratch_{self.stamp}"
        self.campaign_root = self.run_root / "campaigns"
        self.map_root = self.run_root / "maps"
        self.progress_path = self.run_root / "progress.jsonl"
        self.conversation_path = self.run_root / "full_api_conversation.txt"
        self.conversation_export_path = self.run_root / "完整对话记录.txt"
        self.report_json_path = self.run_root / "ultra_from_scratch_report.json"
        self.report_txt_path = self.run_root / "ultra_from_scratch_report.txt"
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.map_root.mkdir(parents=True, exist_ok=True)

        os.environ["FU_GM_PROJECT_DIR"] = str(PROJECT_ROOT)
        os.environ["FU_GM_NORTANTIS_OUTPUT_DIR"] = str(self.map_root)
        os.environ.setdefault("FU_GM_NORTANTIS_TIMEOUT_SECONDS", "240")

        self.campaign_id = f"超长从零测试_白钟大陆_{self.stamp}"
        self.session_id = "session0-to-chapter1-from-scratch"
        self.channel_id = "codex-ultra-from-scratch"
        self.participants = ["阿凛", "南星", "白河", "时雨", "澄砚"]
        self.pc_names = ["伊莉雅", "赛璃", "洛岚", "艾薇娅", "苍祈"]
        self.common = {
            "campaign_id": self.campaign_id,
            "session_id": self.session_id,
            "channel_id": self.channel_id,
        }
        self.service = FUGMHttpService(data_root=self.campaign_root, use_llm=True)
        self.calls: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.errors: list[str] = []
        self.tool_events: list[dict[str, Any]] = []
        self._auto_followup_depth = 0
        self.expected_rules_blocked_labels = {
            "第一章冲突与规则 14 白河",
        }

        self.conversation_path.write_text(
            "\n".join(
                [
                    "FU-GM 从零开始超长测试完整 API 对话",
                    f"campaign_id: {self.campaign_id}",
                    f"session_id: {self.session_id}",
                    f"started_at: {datetime.now().isoformat(timespec='seconds')}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def _record_tool_event(
        self,
        tool: str,
        stage: str,
        evidence: str,
        result: Any = None,
        *,
        public: bool = False,
    ) -> None:
        """Keep a human-readable audit trail of which subsystem actually ran.

        This is intentionally report-only: these notes must not be injected into
        player-facing GM narration, otherwise the long test stops resembling a
        real table.
        """

        self.tool_events.append(
            {
                "index": len(self.tool_events) + 1,
                "tool": tool,
                "stage": stage,
                "evidence": evidence,
                "public_player_facing": public,
                "result": result,
            }
        )

    def run(self) -> int:
        try:
            self._main_flow()
            report = self._build_report(exception=None)
            self._write_report(report)
            return 1 if report["errors"] else 0
        except Exception as exc:
            report = self._build_report(exception=exc)
            self._write_report(report)
            traceback.print_exc()
            return 1
        finally:
            print(f"RUN_ROOT={self.run_root}", flush=True)
            print(f"REPORT_JSON={self.report_json_path}", flush=True)
            print(f"REPORT_TXT={self.report_txt_path}", flush=True)
            print(f"CONVERSATION_TXT={self.conversation_path}", flush=True)

    def _service_retry_delay_seconds(
        self,
        *,
        label: str,
        method: str,
        route: str,
        payload: dict[str, Any],
        status: int,
        body: dict[str, Any],
        attempt: int,
    ) -> float | None:
        """Return a private harness retry delay, or ``None`` to commit the result.

        The base harness never retries HTTP operations because most game calls
        are stateful.  Specialized strict harnesses may opt in only for a
        response that proves dispatch never reached mutable game state.
        """

        return None

    def invoke(self, label: str, method: str, route: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        diagnostic_campaign_id = str(
            payload.get("campaign_id") or getattr(self, "campaign_id", "")
        )
        diagnostic_runtime = self.service.runtimes.get(diagnostic_campaign_id)
        pipeline_count_before = len(
            getattr(getattr(diagnostic_runtime, "app", None), "recent_pipeline_spans", [])
            or []
        )
        self._reset_llm_call_diagnostics(diagnostic_campaign_id)
        started = time.perf_counter()
        service_recovery_attempts: list[dict[str, Any]] = []
        attempt = 1
        while True:
            attempt_started = time.perf_counter()
            status, raw_body = self.service.handle(method, route, payload)
            attempt_elapsed_ms = int((time.perf_counter() - attempt_started) * 1000)
            candidate_body = (
                raw_body
                if isinstance(raw_body, dict)
                else {"ok": status < 400, "raw": str(raw_body)}
            )
            retry_delay = self._service_retry_delay_seconds(
                label=label,
                method=method,
                route=route,
                payload=payload,
                status=int(status),
                body=candidate_body,
                attempt=attempt,
            )
            if retry_delay is None:
                break
            service_recovery_attempts.append(
                {
                    "attempt": attempt,
                    "status": int(status),
                    "elapsed_ms": attempt_elapsed_ms,
                    "error": str(
                        candidate_body.get("error")
                        or candidate_body.get("message")
                        or "transient service failure"
                    )[:500],
                    "retry_delay_seconds": max(0.0, float(retry_delay)),
                }
            )
            print(
                f"[FU-GM HTTP] {label} transient provider failure; "
                f"private retry {attempt} in {max(0.0, float(retry_delay)):.1f}s",
                flush=True,
            )
            if retry_delay > 0:
                time.sleep(float(retry_delay))
            attempt += 1
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        body = candidate_body
        record = {
            "index": len(self.calls) + 1,
            "label": label,
            "method": method,
            "route": route,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "ok": bool(body.get("ok", status < 400)),
            "blocked": bool(body.get("blocked")),
            "rules_blocked": bool(body.get("rules_blocked")),
            "speaker": str(payload.get("speaker") or ""),
            "message": str(payload.get("message") or ""),
            "reply": str(body.get("reply") or body.get("message") or ""),
            "body": body,
        }
        if service_recovery_attempts:
            record["service_recovery_attempts"] = service_recovery_attempts
        record["llm_diagnostics"] = self._collect_llm_call_diagnostics(
            diagnostic_campaign_id
        )
        diagnostic_runtime = self.service.runtimes.get(diagnostic_campaign_id)
        pipeline_spans = list(
            getattr(getattr(diagnostic_runtime, "app", None), "recent_pipeline_spans", [])
            or []
        )
        if len(pipeline_spans) > pipeline_count_before:
            record["pipeline_span"] = dict(pipeline_spans[-1])
        if diagnostic_runtime is not None:
            record["clock_boundaries"] = [
                {
                    "name": str(clock.name or ""),
                    "current": int(clock.current or 0),
                    "maximum": int(clock.max_segments or 0),
                    "clock_type": str(clock.clock_type or ""),
                    "stakes": str(clock.stakes or ""),
                    "completion_consequence": str(clock.completion_consequence or ""),
                    "status": str(clock.status or "active"),
                    "scope": str(clock.scope or ""),
                    "scene_id": str(clock.scene_id or ""),
                }
                for clock in diagnostic_runtime.app.clock_manager.all()
                if int(clock.current or 0) < int(clock.max_segments or 0)
                and str(clock.status or "active")
                not in {"resolved", "abandoned", "archived"}
            ]
        self.calls.append(record)
        self.progress_path.parent.mkdir(parents=True, exist_ok=True)
        with self.progress_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._append_conversation(record)
        if status >= 400 or not record["ok"]:
            error_text = str(body.get("error") or body.get("message") or record["reply"] or "unknown error")
            self.errors.append(f"{label} failed: status={status}, error={error_text}")
        if record["rules_blocked"]:
            blocked_text = record["reply"][:220]
            if label in self.expected_rules_blocked_labels:
                self.notes.append(f"{label} 触发预期规则拦截：{blocked_text}")
            else:
                self.errors.append(f"{label} rules_blocked: {blocked_text}")
        leaked_tokens = [
            "内部恢复重试",
            "npc_action_type",
            "is not a valid",
            "模型暂时没有接上",
            "Traceback",
            "KeyError",
            "SellItem",
            "硬状态",
            "GM应回应",
            "保持冲突继续",
            "ActionType",
            "fade_out",
            "GM应",
            "硬成本",
            "规则层",
            "用途：",
            "完成后写入：",
            "预定持有者",
            "暂时没有执行明确动作",
        ]
        for token in leaked_tokens:
            if token in record["reply"]:
                self.errors.append(f"{label} leaked internal token {token!r}")
                break
        print(
            f"[{record['index']:02d}] {label}: status={status} "
            f"elapsed={elapsed_ms}ms ok={record['ok']} blocked={record['blocked']}",
            flush=True,
        )
        if self._auto_followup_depth == 0:
            followup = self._player_followup_to_gm_prompt(record)
            if followup:
                followup_speaker, followup_message = followup
                self._auto_followup_depth += 1
                try:
                    self.invoke(
                        f"自动回应GM追问 {followup_speaker}",
                        "POST",
                        route,
                        {**payload, "speaker": followup_speaker, "message": followup_message},
                    )
                finally:
                    self._auto_followup_depth -= 1
        return body

    def _reset_llm_call_diagnostics(self, campaign_id: str) -> None:
        runtime = self.service.runtimes.get(campaign_id)
        if runtime is None:
            return
        components = (
            getattr(self.service, "gm_tool_agent", None),
            getattr(runtime.app, "expressor", None),
        )
        for component in components:
            if component is None:
                continue
            if hasattr(component, "last_used_fallback"):
                component.last_used_fallback = False
            if hasattr(component, "last_error"):
                component.last_error = ""
            if hasattr(component, "last_recovery_attempts"):
                component.last_recovery_attempts = []
            client = getattr(component, "client", None)
            if client is not None and hasattr(client, "last_recovery_attempts"):
                client.last_recovery_attempts = []

    def _collect_llm_call_diagnostics(self, campaign_id: str) -> dict[str, Any]:
        runtime = self.service.runtimes.get(campaign_id)
        if runtime is None:
            return {}
        result: dict[str, Any] = {}
        components = {
            "core_gm": getattr(self.service, "gm_tool_agent", None),
            "expressor": getattr(runtime.app, "expressor", None),
        }
        for component_name, component in components.items():
            if component is None:
                continue
            client = getattr(component, "client", None)
            recoveries = list(getattr(client, "last_recovery_attempts", []) or []) if client is not None else []
            result[component_name] = {
                "used_fallback": bool(getattr(component, "last_used_fallback", False)),
                "error": str(getattr(component, "last_error", "") or "")[:500],
                "recovery_attempts": [
                    {
                        "reason": str(getattr(item, "reason", "") or "")[:300],
                        "attempt": int(getattr(item, "attempt", 0) or 0),
                    }
                    for item in recoveries
                ],
            }
        return result

    def route_table_message(
        self,
        label: str,
        speaker: str,
        message: str,
        *,
        expected_target: str,
        expected_send_reply: bool,
        directed_at_gm: bool = False,
    ) -> dict[str, Any]:
        """Send a message through the real AstrBot/QQ routing boundary.

        Direct session/game endpoints intentionally mean "FU-GM has already
        been selected". These routed samples verify whether casual table talk
        stays silent while substantive play signals still reach FU-GM.
        """

        body = self.invoke(
            label,
            "POST",
            "/v1/message/route",
            {
                **self.common,
                "speaker": speaker,
                "message": message,
                "is_at_bot": bool(directed_at_gm),
            },
        )
        target = str(body.get("target") or "")
        send_reply = bool(body.get("send_reply"))
        if target != expected_target:
            self.errors.append(f"{label} routing target={target!r}, expected {expected_target!r}")
        if send_reply != expected_send_reply:
            self.errors.append(f"{label} send_reply={send_reply!r}, expected {expected_send_reply!r}")
        if not expected_send_reply and str(body.get("reply") or body.get("message") or "").strip():
            self.errors.append(f"{label} expected no GM reply but response contained text.")
        self._record_tool_event(
            "AstrBot/QQ 路由器",
            label,
            f"群消息由 /v1/message/route 判定 target={target!r}, send_reply={send_reply}",
            {"expected_target": expected_target, "expected_send_reply": expected_send_reply},
            public=send_reply,
        )
        return body

    def _player_followup_to_gm_prompt(self, record: dict[str, Any]) -> tuple[str, str] | None:
        if record["route"] not in {
            "/v1/session-zero/message",
            "/v1/game/turn",
            "/v1/message/route",
        }:
            return None
        if (
            record["route"] == "/v1/message/route"
            and str((record.get("body") or {}).get("target") or "") != "fu_gm"
        ):
            return None
        reply = str(record.get("reply") or "")
        speaker = str(record.get("speaker") or "")
        if not reply or not speaker:
            return None
        hero_by_speaker = {
            "阿凛": ("伊莉雅", "责任", "守住无辜者的名字与退路；她能接受战术妥协，但不会把别人交给财团换安全。"),
            "南星": ("赛璃", "希望", "在局面最灰暗时仍选择救人和修复；她的底线是不把病人与亡者当资源。"),
            "白河": ("洛岚", "赎罪", "每次看到财团机器伤人都会逼自己站出来补救；他不会再替记忆炉找借口。"),
            "时雨": ("艾薇娅", "妥协", "她会先寻找让各方活下来的协议；但若妥协需要牺牲无辜者的记忆，她会转而公开对抗。"),
            "澄砚": ("苍祈", "亏欠", "他会优先回应被遗忘者和奥灵的请求；底线是不再许下自己不准备履行的契约。"),
        }
        speaker_by_hero = {hero: owner for owner, (hero, _theme, _drive) in hero_by_speaker.items()}
        target_speaker = self._followup_target_speaker(reply, speaker, hero_by_speaker, speaker_by_hero)
        if target_speaker not in hero_by_speaker:
            return None
        hero, theme, drive = hero_by_speaker[target_speaker]
        if record["route"] == "/v1/game/turn":
            if "大成功" in reply and "机会" in reply and any(
                token in reply for token in ("揭示", "进展", "纽带", "优势", "转折")
            ):
                return target_speaker, f"{hero}把这次大成功的机会用于【揭示】。"
            if "要不要花 1 点物语点" in reply or "要不要花1点物语点" in reply:
                return target_speaker, f"{hero}暂不消耗物语点，接受这次失败。"
            current_actor = self._current_actor_from_gm_reply(reply)
            if not current_actor:
                return None
            current_speaker = speaker_by_hero.get(current_actor)
            if current_speaker not in hero_by_speaker:
                return None
            current_hero, _current_theme, _current_drive = hero_by_speaker[current_speaker]
            if "命刻" in reply and any(token in reply for token in ("还剩", "赌注", "倒计时", "变化")):
                clock_name = self._latest_clock_from_gm_reply(reply)
                if not clock_name:
                    return None
                return current_speaker, self._clock_followup_action(current_hero, clock_name, reply)
            return None
        theme_question_markers = ("推着", "支配行动", "主导行动", "底线", "拒绝退让", "给人的感觉", "通常会")
        if any(token in reply for token in theme_question_markers) and ("？" in reply or "?" in reply):
            return target_speaker, f"{hero}会被【{theme}】推着回应关键时刻：{drive}"
        return None

    def _followup_target_speaker(
        self,
        reply: str,
        fallback_speaker: str,
        hero_by_speaker: dict[str, tuple[str, str, str]],
        speaker_by_hero: dict[str, str],
    ) -> str:
        speaker_names = "|".join(re.escape(name) for name in hero_by_speaker)
        hero_names = "|".join(re.escape(name) for name in speaker_by_hero)
        focused_hero_match = re.search(
            rf"(?P<hero>{hero_names}).{{0,12}}(?:主题|通常会|底线|拒绝退让|给人的感觉|如何主导行动)",
            reply,
        )
        if focused_hero_match:
            return speaker_by_hero.get(focused_hero_match.group("hero"), fallback_speaker)
        speaker_match = re.search(rf"(?:还没听到|想听听|请|让)\s*(?P<speaker>{speaker_names})\s*(?:的|来|说)", reply)
        if speaker_match:
            return speaker_match.group("speaker")
        hero_match = re.search(rf"(?P<hero>{hero_names}).{{0,18}}(?:主题|给人的感觉|通常会|底线|身份|故乡|职业|技能|装备)", reply)
        if hero_match:
            return speaker_by_hero.get(hero_match.group("hero"), fallback_speaker)
        return fallback_speaker

    def _current_actor_from_gm_reply(self, reply: str) -> str:
        patterns = [
            r"下一位行动者：(?P<actor>[^。\n]+)",
            r"镜头推进到【(?P<actor>[^】]+)】",
            r"现在(?:轮到|是)【(?P<actor>[^】]+)】",
        ]
        matches: list[tuple[int, str]] = []
        for pattern in patterns:
            matches.extend((match.start(), match.group("actor").strip(" 。")) for match in re.finditer(pattern, reply))
        if not matches:
            return ""
        matches.sort(key=lambda item: item[0])
        return matches[-1][1]

    def _latest_clock_from_gm_reply(self, reply: str) -> str:
        generic_names = {"当前命刻", "当前目标命刻", "当前目标", "当前线索"}
        main_text, _sep, progress_text = reply.partition("命刻进度")
        explicit_matches = [
            match.strip()
            for match in re.findall(r"命刻【([^】]+)】", main_text)
            if match.strip() and match.strip() not in generic_names
        ]
        if explicit_matches:
            return explicit_matches[-1]

        candidates: list[tuple[int, str]] = []
        for match in re.finditer(r"\[([^\]]+)\]\s*(\d+)/(\d+)([^｜\n]*)", progress_text):
            name = match.group(1).strip()
            if not name or name in generic_names:
                continue
            current = int(match.group(2))
            maximum = int(match.group(3))
            if current >= maximum:
                continue
            detail = match.group(4)
            priority = 1
            if "威胁命刻" in detail:
                priority = 0
            candidates.append((priority, name))
        if not candidates:
            return ""
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    def _clock_followup_action(self, hero: str, clock_name: str, reply: str) -> str:
        """Produce concrete, table-like player action instead of test harness jargon."""

        verb = "尝试推进"
        if self._clock_looks_like_threat(clock_name, reply):
            verb = "尝试压制"
        scene_actions: dict[str, dict[str, str]] = {
            "财团巡逻队逼近": {
                "伊莉雅": "伊莉雅举盾护住旅人和风铃廊入口，示意守望会把人群往碑柱后撤，让巡逻队很难直接锁定目标。",
                "赛璃": "赛璃低声安抚旅人与驿卒，请他们把容易引起恐慌的铃声和脚步压下来，避免巡逻队顺着混乱找进来。",
                "洛岚": "洛岚沿着车辙和旧钟的回声判断巡逻路线，指挥守望会把货车与碑林阴影布成临时路障。",
                "艾薇娅": "艾薇娅走到守望会与财团可能接触的位置，准备抛出一套能拖住封锁命令的外交说辞。",
                "苍祈": "苍祈把手按在碑柱旧树纹上，借奥灵低语扰乱追踪印记，让财团的定位信号慢下来。",
            },
            "仪式：风铃回声": {
                "伊莉雅": "伊莉雅把碎月遗物稳稳托在盾后，让风铃回声有一个不会被打断的支点。",
                "赛璃": "赛璃跟着风铃的节拍轻声引导失忆旅人的呼吸，帮仪式把散乱的记忆拢回同一条旋律。",
                "洛岚": "洛岚校准旧钟齿轮与风铃频率，把错开的回声一点点拨回仪式节拍里。",
                "艾薇娅": "艾薇娅用温和但清晰的问句牵住旅人的注意力，让他不要被财团的口令拉走。",
                "苍祈": "苍祈请奥灵沿着风铃内侧的名字寻找回响，把那段被改写的记忆牵回现场。",
            },
            "旧路闸门开启": {
                "伊莉雅": "伊莉雅把盾顶在闸门边缘，稳住门轴，给同伴争取开锁和校准机关的时间。",
                "赛璃": "赛璃检查闸门上的白花纹路，寻找能让守望会认可的开启顺序。",
                "洛岚": "洛岚沿着旧钟与闸门齿轮的节拍校准机关，让卡住的门轴重新找到咬合点。",
                "艾薇娅": "艾薇娅转向守望会成员，说明开放旧路能保护旅人也能避免财团正面冲突。",
                "苍祈": "苍祈把奥灵感知伸进门缝，寻找闸门记忆里仍愿意回应的名字。",
            },
        }
        for key, actions in scene_actions.items():
            if key in clock_name:
                action_text = actions.get(hero, f"{hero}用自己的方式把局势往小队需要的方向拉。")
                return f"{action_text}（{verb}命刻【{clock_name}】。）"
        if clock_name in {"当前命刻", "当前目标命刻", "当前目标", "当前线索"}:
            return ""
        return f"{hero}{verb}命刻【{clock_name}】，把注意力放在眼前能改变局面的具体行动上。"

    def _clock_looks_like_threat(self, clock_name: str, reply: str) -> bool:
        related_segments = [
            segment
            for line in str(reply or "").splitlines()
            for segment in line.split("｜")
            if clock_name and clock_name in segment
        ]
        text = f"{clock_name}\n" + "\n".join(related_segments)
        threat_tokens = ("威胁", "逼近", "巡逻", "警报", "倒计时", "包围", "毁灭", "失控", "崩塌", "追兵")
        return any(token in text for token in threat_tokens)

    def _main_flow(self) -> None:
        self.invoke("新建战役", "POST", "/v1/campaigns/new", {"campaign_id": self.campaign_id})
        self._register_test_chapter_package()
        self.invoke("会话门控进入第零章", "POST", "/v1/session/gate", {**self.common, "status": "session_zero"})
        self.invoke(
            "第零章开场",
            "POST",
            "/v1/session-zero/start",
            {**self.common, "participants": self.participants},
        )
        self.route_table_message(
            "第零章自由讨论静默 01",
            "阿凛",
            "哈哈哈",
            expected_target="silent",
            expected_send_reply=False,
        )
        self.route_table_message(
            "第零章自由讨论静默 02",
            "南星",
            "@白河 你先说吧",
            expected_target="silent",
            expected_send_reply=False,
        )
        self.route_table_message(
            "第零章自由讨论实质贡献 01",
            "时雨",
            "我希望这团保留一点明亮冒险感，不要全程压抑。",
            expected_target="fu_gm",
            expected_send_reply=True,
        )

        for index, (speaker, message) in enumerate(self._session_zero_world_turns(), start=1):
            self.invoke(
                f"第零章世界共创 {index:02d} {speaker}",
                "POST",
                "/v1/session-zero/message",
                {**self.common, "speaker": speaker, "message": message},
            )

        for index, (speaker, message) in enumerate(self._session_zero_completion_turns(), start=1):
            self.invoke(
                f"第零章流程补齐 {index:02d} {speaker}",
                "POST",
                "/v1/session-zero/message",
                {**self.common, "speaker": speaker, "message": message},
            )

        for index, (speaker, message) in enumerate(self._session_zero_character_turns(), start=1):
            self.invoke(
                f"第零章角色创建 {index:02d} {speaker}",
                "POST",
                "/v1/session-zero/message",
                {**self.common, "speaker": speaker, "message": message},
            )
        self._record_tool_event(
            "第零章/角色创建管理",
            "第零章完成后",
            "SessionZeroManager 从玩家自然语言中抽取世界共识、小队原型、安全边界和五名 PC 角色草稿。",
            {
                "world_ready": self._runtime().app.session_zero_manager.world_creation_ready(),
                "hero_creation_status": self._runtime().app.session_zero_manager.hero_creation_status(),
                "world_profile": {
                    "continent_name": self._runtime().app.world_state.world_profile.continent_name,
                    "starting_region": self._runtime().app.world_state.world_profile.starting_region,
                    "group_concept": self._runtime().app.world_state.world_profile.group_concept,
                    "villain_seeds": list(self._runtime().app.world_state.world_profile.villain_seeds),
                    "safety_lines": list(self._runtime().app.world_state.world_profile.safety_lines),
                    "safety_veils": list(self._runtime().app.world_state.world_profile.safety_veils),
                },
            },
            public=True,
        )

        self._verify_no_direct_pc_injection()
        self._wait_for_async_map_if_any()

        self.pre_gate_snapshot = self._snapshot(include_private=True)
        self.pre_gate_hero_status = self._runtime().app.session_zero_manager.hero_creation_status()
        self.pre_gate_world_ready = self._runtime().app.session_zero_manager.world_creation_ready()
        if not self.pre_gate_hero_status.get("ready"):
            self.notes.append(f"冒险门控前角色仍未 ready：{self.pre_gate_hero_status}")

        self.gate_body = self.invoke(
            "尝试进入第一章",
            "POST",
            "/v1/session/gate",
            {**self.common, "status": "adventure", "reason": "Session 0 已完成世界、小队、角色与第一幕共识，进入第一章。"},
        )
        if self.gate_body.get("blocked"):
            self._recover_missing_character_fields()
            self.gate_body = self.invoke(
                "补齐后重新进入第一章",
                "POST",
                "/v1/session/gate",
                {**self.common, "status": "adventure", "reason": "角色已补齐并确认，重新进入第一章。"},
            )
        if self.gate_body.get("blocked"):
            self.errors.append("冒险门控仍被阻挡。")
            return

        self._start_chapter_scene()
        self.invoke(
            "第一章 GM 开场",
            "POST",
            "/v1/game/turn",
            {
                **self.common,
                "speaker": "阿凛",
                "message": "第一章开始了，请时悠先描述白花碑驿站此刻的现场、在场人物和眼前压力，我们再行动。",
            },
        )
        self.route_table_message(
            "第一章自由讨论静默 01",
            "南星",
            "你们觉得先问会长还是先看旅人？",
            expected_target="silent",
            expected_send_reply=False,
        )
        self.route_table_message(
            "第一章自由讨论静默 02",
            "白河",
            "哈哈哈这个驿站好有日式RPG味。",
            expected_target="silent",
            expected_send_reply=False,
        )
        for index, (speaker, message) in enumerate(self._chapter_one_turns_before_combat(), start=1):
            self.invoke(
                f"第一章连贯场景 {index:02d} {speaker}",
                "POST",
                "/v1/game/turn",
                {**self.common, "speaker": speaker, "message": message},
            )
        self._record_chapter_scene_tools_after_social_phase()

        self._prepare_conflict_state()
        self.route_table_message(
            "冲突自由讨论静默 01",
            "时雨",
            "我们要不要先开旧路，不然被包围就麻烦了？",
            expected_target="silent",
            expected_send_reply=False,
        )
        self.route_table_message(
            "冲突自由讨论静默 02",
            "澄砚",
            "我有点担心先开旧路会不会让守望会背锅，你们怎么看？",
            expected_target="silent",
            expected_send_reply=False,
        )
        for index, (speaker, message) in enumerate(self._chapter_one_combat_turns(), start=1):
            self.invoke(
                f"第一章冲突与规则 {index:02d} {speaker}",
                "POST",
                "/v1/game/turn",
                {**self.common, "speaker": speaker, "message": message},
            )
        self._exercise_villain_conflict_tools()

        self.invoke(
            "第一章收团",
            "POST",
            "/v1/session/end",
            {**self.common, "title": "第一章：白花碑驿站的迟响"},
        )
        self._exercise_core_design_tools()
        self.audit = self.invoke("读取审计仪表盘", "GET", self._audit_route(limit=320))
        self._write_public_transcript_copy()

    def _register_test_chapter_package(self) -> None:
        runtime = self._runtime()
        world = runtime.app.world_state
        package = ChapterPackage(
            chapter_title="白花碑驿站的迟响",
            synopsis=(
                "一群临时守护者护送失忆旅人与碎月遗物抵达白花碑驿站，"
                "必须在白花守望会、辉钢财团巡逻队与风铃记忆异状之间争取旧路。"
            ),
            intro_prompt=(
                "从白花碑驿站的风铃廊开场：失忆旅人听见自己的名字，"
                "守望会会长不愿轻易开放旧路，远处已有财团巡逻的金属回声。"
            ),
            conclusion_prompt=(
                "当队伍获得旧路通行、确认财团收购记忆的第一条证据，"
                "并决定如何保护失忆旅人时，本章进入收束。"
            ),
            timebox_minutes=240,
            shared_creation_slots=[
                "白花守望会为何守护旧路",
                "失忆旅人与哪位英雄存在旧日牵连",
                "第一条财团证据以什么形式出现",
            ],
            iconic_elements=[
                "白花风铃",
                "碎月遗物",
                "失忆旅人",
                "白花碑驿站旧路",
                "辉钢财团巡逻印记",
            ],
            scenes=[
                ChapterPackageScene(
                    title="风铃廊问路",
                    scene_type="social_conflict",
                    location="白花碑驿站·风铃廊",
                    purpose="让玩家与白花守望会谈判，建立旧路、旅人和风铃记忆异状的压力。",
                    when_to_use="第一章开场或队伍进入驿站时。",
                    required_elements=["白花风铃", "失忆旅人", "白花守望会会长"],
                    optional_elements=["灰晶病流言", "驿卒与旅人"],
                    success_condition="守望会愿意提供旧路或给出可完成的条件。",
                    exit_condition="谈判成功、谈判破裂，或财团压力强行切入冲突。",
                ),
                ChapterPackageScene(
                    title="风铃回声仪式",
                    scene_type="ritual",
                    location="白花碑驿站·登记小室",
                    purpose="用仪式或调查揭示被改写记忆的线索，同时保护安全边界。",
                    when_to_use="玩家主动调查风铃、失忆旅人或碎月遗物时。",
                    required_elements=["白花风铃", "碎月遗物"],
                    optional_elements=["御魂术", "旧钟齿轮", "奥灵低语"],
                    success_condition="玩家取得一条可行动线索，而不是直接得到全部真相。",
                    exit_condition="仪式完成、失败付出代价，或被巡逻队打断。",
                ),
                ChapterPackageScene(
                    title="旧路闸门与巡逻队",
                    scene_type="conflict",
                    location="白花碑驿站·旧路闸门",
                    purpose="把社会压力转成冲突：玩家要开旧路、护住旅人，并处理逼近的财团巡逻。",
                    when_to_use="财团追兵逼近或谈判进入僵局时。",
                    required_elements=["白花碑驿站旧路", "辉钢财团巡逻印记"],
                    optional_elements=["监察官艾蕾娜", "财团机兵", "威胁命刻"],
                    success_condition="旧路开启或巡逻队暂时失去封锁机会。",
                    exit_condition="队伍撤离、被包围，或达成临时协议。",
                ),
            ],
            adversary_notes=[
                "监察官艾蕾娜应主动推进财团目标，但不要替玩家决定角色行动。",
                "财团机兵使用清晰的固定模式，方便玩家读局。",
            ],
            reward_notes=[
                "章节奖励优先给情报、通行权、盟友承诺或低价值稀有物品线索。",
                "如果玩家保护旅人且没有牺牲守望会，可给额外英雄日志钩子。",
            ],
            gm_notes=[
                "章节包只给 GM 稳定骨架，不要原文念给玩家。",
                "标志性元素可被调查、请求、争夺和保护，但不能被普通物语改写直接摧毁或改归属。",
            ],
            status="ready",
        )
        world.register_chapter_package(package)
        world.register_iconic_element(
            "监察官艾蕾娜",
            element_type="adversary",
            description="本章主要对手，曾是赤羽遗民，代表财团秩序与记忆集中管理理念。",
            source=package.chapter_title,
            restrictions=["不能由普通物语改写直接杀死、洗白或改成亲属。"],
        )
        # This test fixture mutates the runtime directly rather than through an
        # HTTP command, so it must establish the same durability boundary that
        # production tool calls receive before a checkpoint can be resumed.
        self.service._autosave_campaign(runtime, self.campaign_id)
        self.notes.append("已向长测战役注册章节包【白花碑驿站的迟响】。")
        self._record_tool_event(
            "章节包管理",
            "第零章前置",
            "注册章节包【白花碑驿站的迟响】，包含社交冲突、仪式、冲突三个场景，并登记反派标志性元素。",
            {
                "chapter_title": package.chapter_title,
                "scene_titles": [scene.title for scene in package.scenes],
                "iconic_elements": list(package.iconic_elements),
                "adversary_notes": list(package.adversary_notes),
            },
        )

    def _session_zero_world_turns(self) -> list[tuple[str, str]]:
        return [
            (
                "阿凛",
                "我想共创的大陆叫白钟大陆，形态就是普通类地球大陆，不讨论环形、巨龟背或其他异形世界。"
                "西侧是鸦羽山脉，中央有镜线内海，南岸是白花碑驿站和雾潮海岸，东南散布潮鸢群岛。"
                "魔法与科技并存：灵魂晶炉驱动车辆、工坊和财团机器，古老的御魂术和元素仪式负责安抚灵魂之河。"
                "我贡献一个国家：钟鸣公国在镜线内海北岸，正午大钟能安抚灵魂，但也让贵族能控制谁的哀悼被听见。"
                "重大历史事件是碎月坠落当夜白钟大陆所有钟慢了一拍；世界奥秘是姐姐的名字为何刻在白花风铃内侧却无人记得她死亡。"
                "世界威胁是辉钢财团正在把灰晶病患者的记忆作为可买卖燃料。"
                "界限：不详细描写性暴力、酷刑、现实仇恨煽动。帷幕：儿童遇险、身体病变、亲密内容淡出处理。"
                "我希望故事有史诗奇幻的希望感，中期能揭开颠覆力量平衡的真相；但主线从边境驿站的选择开始。",
            ),
            (
                "南星",
                "我贡献一个地区和历史事件：潮鸢群岛信奉迁徙的海风神，飞翼船会追着季风移动；"
                "三十年前碎月坠落，赤羽旧王都一夜消失，幸存者沿雾潮海岸建立白花碑驿站。"
                "我想要的谜团是：每年归潮祭后都会少一座岛，可所有人的公开记忆都会自动改写。"
                "世界威胁是苍白司教团把灰晶病包装成灵魂升格。",
            ),
            (
                "白河",
                "我贡献一个地区、威胁和阵营：辉钢财团控制第七采掘城，它正在向雾潮海岸移动，收购灰晶病患者的记忆作为魔导燃料。"
                "苍白司教团宣称灰晶病是灵魂升格的祝福，暗中帮财团筛选病人。"
                "重大历史事件是记忆炉第一次启动时吞掉了一整条矿道工人的姓名；"
                "世界奥秘是第七采掘城的紧急停机协议为何只回应赤羽遗民的歌。"
                "小队原型是临时守护者：护送一名失忆旅人和碎月遗物，从白花碑驿站前往钟鸣公国求证真相。"
                "反派种子：第七采掘城的监察官艾蕾娜曾是赤羽遗民，认为只有把记忆集中管理，世界才不会再遗忘灾难。",
            ),
            (
                "时雨",
                "我贡献一个国家和社会冲突：奥涅里亚灯塔舰队维持海上贸易，但王室和港口行会互不信任。"
                "摄政王想把失踪群岛调查权交给辉钢财团，因为财团承诺能让记忆不再被归潮祭改写。"
                "重大历史事件是老国王病倒后，摄政王把王室海图抵押给辉钢财团；"
                "世界奥秘是灯塔为什么能照见已经消失的岛。"
                "世界威胁是港口行会和王室决裂会让财团取得失踪群岛调查权。",
            ),
            (
                "澄砚",
                "我贡献一个神秘地点：沉默森林位于白钟大陆东南内陆，森林里的奥灵拒绝回应人类，"
                "但会在碎月之夜把未说出口的名字写到树皮上。世界奥秘是：这些名字里有些人仍然活着。"
                "王国或国家是沉默森林周边的树誓村社，村社不承认王权，只与奥灵立约；"
                "重大历史事件是碎月之夜后森林第一次拒绝所有人类祈祷。"
                "世界威胁是苍白司教团想把沉默森林变成灰晶病圣地。",
            ),
            (
                "南星",
                "第一幕我提议从白花碑驿站开始：先争取白花守望会开放旧路，再处理远处正在接近的财团巡逻队。",
            ),
            (
                "时雨",
                "我希望第一章里有一场不靠战斗解决的冲突，要靠证据、承诺和情感改变别人的决定。",
            ),
            (
                "澄砚",
                "我也赞成从白花碑驿站开幕；队伍如果只抢线索、不保护普通人，奥灵会沉默，这是我希望看到的后果方向。",
            ),
        ]

    def _session_zero_completion_turns(self) -> list[tuple[str, str]]:
        return []

    def _session_zero_character_turns(self) -> list[tuple[str, str]]:
        turns: list[tuple[str, str]] = []

        def add_character(
            speaker: str,
            core: str,
            skill_turns: list[str],
            final: str,
        ) -> None:
            turns.append((speaker, core))
            for skill in skill_turns:
                turns.append((speaker, skill))
            turns.append((speaker, final))

        add_character(
            "阿凛",
            "我的玩家名是阿凛，角色名伊莉雅。身份：赤羽遗民的盾誓骑士；主题：责任；故乡：白花碑驿站。"
            "职业分配：守护者3级、元素使2级。属性骰：敏捷d8、洞察d8、力量d10、意志d6。",
            [
                "伊莉雅职业技能先选保镖。",
                "伊莉雅再选防御精通。",
                "伊莉雅第三个守护者技能选挺身守护。",
                "伊莉雅元素使技能先选元素魔法。",
                "伊莉雅最后一项技能选元素系仪式。",
            ],
            "伊莉雅法术选择元素幕障。初始装备：钢匕首、青铜盾、旅行装束。羁绊：赛璃：信赖；洛岚：钦佩。"
            "背景钩子：她的姐姐名字在白花风铃内侧，却无人记得她是否真的死去。伊莉雅确认角色并正式建卡。",
        )
        add_character(
            "南星",
            "我的玩家名是南星，角色名赛璃。身份：钟鸣公国的御魂医师；主题：希望；故乡：钟鸣公国。"
            "职业分配：御魂使3级、旅人2级。属性骰：敏捷d6、洞察d10、力量d8、意志d8。",
            [
                "赛璃第一项技能选灵魂魔法。",
                "赛璃第二项技能再选一次灵魂魔法。",
                "赛璃第三项技能选御魂系仪式。",
                "赛璃旅人技能先选见多识广。",
                "赛璃最后一项技能选充足补给。",
            ],
            "赛璃法术选择治愈术、屏障。初始装备：法杖、旅行装束。羁绊：伊莉雅：信赖；洛岚：喜爱。"
            "背景钩子：她曾听见钟声里有自己的未来遗言。赛璃确认角色并正式建卡。",
        )
        add_character(
            "白河",
            "我的玩家名是白河，角色名洛岚。身份：辉钢财团出逃的魔导工匠；主题：赎罪；故乡：第七采掘城。"
            "职业分配：造物使3级、武器大师2级。属性骰：敏捷d8、洞察d10、力量d8、意志d6。",
            [
                "洛岚职业技能先选便携装置。",
                "洛岚第二项造物使技能选秘密配方。",
                "洛岚第三项造物使技能选先见之明。",
                "洛岚武器大师技能先选碎骨。",
                "洛岚最后一项技能选破防打击。",
            ],
            "洛岚的便携装置选择魔导装置。初始装备：铁锤、旅行装束。羁绊：伊莉雅：钦佩；赛璃：信赖。"
            "背景钩子：他参与设计过第七采掘城的记忆炉，知道它有一个不能公开的紧急停机协议。洛岚确认角色并正式建卡。",
        )
        add_character(
            "时雨",
            "我的玩家名是时雨，角色名艾薇娅。身份：奥涅里亚的灯塔外交官；主题：妥协；故乡：奥涅里亚王都。"
            "职业分配：游说家2级、熵术士2级、旅人1级。属性骰：敏捷d8、洞察d8、力量d6、意志d10。",
            [
                "艾薇娅游说家技能先选谴责。",
                "艾薇娅第二个游说家技能选鼓舞。",
                "艾薇娅熵术士技能先选熵系魔法。",
                "艾薇娅再选熵系仪式。",
                "艾薇娅旅人技能选见多识广。",
            ],
            "艾薇娅法术选择加速术。初始装备：法杖、旅行装束。羁绊：伊莉雅：信赖；苍祈：猜忌。"
            "背景钩子：她知道摄政王为什么愿意相信辉钢财团。她希望用谈判阻止战争。艾薇娅确认角色并正式建卡。",
        )
        add_character(
            "澄砚",
            "我的玩家名是澄砚，角色名苍祈。身份：沉默森林的失约奥灵使；主题：亏欠；故乡：树誓村社。"
            "职业分配：奥灵使2级、拟兽使2级、暗刃骑士1级。属性骰：敏捷d6、洞察d10、力量d8、意志d8。",
            [
                "苍祈奥灵使技能先选契约与召唤。",
                "苍祈第二项奥灵使技能选奥灵系仪式。",
                "苍祈拟兽使技能先选野性之语。",
                "苍祈第二项拟兽使技能选拟兽系仪式。",
                "拟兽系仪式的施法属性我选洞察+意志。",
                "苍祈暗刃骑士技能选暗影击。",
            ],
            "苍祈与魔典奥灵缔结了起始契约。初始装备：魔典、旅行装束。羁绊：洛岚：猜忌；赛璃：喜爱。"
            "背景钩子：他曾向沉默森林的奥灵许诺会带回一个被世界遗忘的名字。苍祈确认角色并正式建卡。",
        )
        turns.append(
            (
                "阿凛",
                "我们确认第一幕：白花碑驿站的迟响。目标是说服白花守望会给出旧路，保护失忆旅人，发现财团收购记忆的第一条证据。",
            )
        )
        return turns

    def _recover_missing_character_fields(self) -> None:
        status = self.gate_body.get("hero_creation") or self._runtime().app.session_zero_manager.hero_creation_status()
        missing = status.get("missing_by_player", {}) if isinstance(status, dict) else {}
        if not missing:
            self.errors.append("门控 blocked 但未提供缺项。")
            return
        recovery_profiles = {
            "伊莉雅": (
                "阿凛",
                "角色名伊莉雅，身份赤羽遗民的盾誓骑士，主题责任，故乡白花碑驿站，"
                "职业守护者3/元素使2，属性敏捷d8洞察d8力量d10意志d6，技能保镖1、防御精通1、挺身守护1、元素魔法1、元素系仪式1，"
                "法术元素幕障，装备钢匕首、青铜盾、旅行装束，并确认角色正式建卡。",
            ),
            "赛璃": (
                "南星",
                "角色名赛璃，身份钟鸣公国的御魂医师，主题希望，故乡钟鸣公国，"
                "职业御魂使3/旅人2，属性敏捷d6洞察d10力量d8意志d8，技能灵魂魔法2、御魂系仪式1、见多识广1、充足补给1，"
                "法术治愈术、屏障，装备法杖、旅行装束，并确认角色正式建卡。",
            ),
            "洛岚": (
                "白河",
                "角色名洛岚，身份辉钢财团出逃的魔导工匠，主题赎罪，故乡第七采掘城，"
                "职业造物使3/武器大师2，属性敏捷d8洞察d10力量d8意志d6，技能便携装置1、秘密配方1、先见之明1、碎骨1、破防打击1，"
                "便携装置选择魔导装置，"
                "装备铁锤、旅行装束，并确认角色正式建卡。",
            ),
            "艾薇娅": (
                "时雨",
                "角色名艾薇娅，身份奥涅里亚的灯塔外交官，主题妥协，故乡奥涅里亚王都，"
                "职业游说家2/熵术士2/旅人1，属性敏捷d8洞察d8力量d6意志d10，技能谴责1、鼓舞1、熵系魔法1、熵系仪式1、见多识广1，"
                "法术加速术，装备法杖、旅行装束，并确认角色正式建卡。",
            ),
            "苍祈": (
                "澄砚",
                "角色名苍祈，身份沉默森林的失约奥灵使，主题亏欠，故乡树誓村社，"
                "职业奥灵使2/拟兽使2/暗刃骑士1，属性敏捷d6洞察d10力量d8意志d8，技能契约与召唤1、奥灵系仪式1、野性之语1、拟兽系仪式1、暗影击1，"
                "装备魔典、旅行装束，并确认角色正式建卡。",
            ),
        }
        for label, fields in missing.items():
            text_fields = "、".join(str(field) for field in fields) if isinstance(fields, list) else str(fields)
            profile = next(
                (
                    value
                    for hero_name, value in recovery_profiles.items()
                    if hero_name in str(label) or value[0] in str(label)
                ),
                ("阿凛", "请按当前玩家的角色草稿补齐姓名、身份、主题、故乡、职业、属性、技能、法术、装备，并确认角色正式建卡。"),
            )
            speaker, profile_text = profile
            message = f"补齐角色【{label}】缺项：{text_fields}。{profile_text}"
            self.invoke(
                f"门控补齐角色 {label}",
                "POST",
                "/v1/session-zero/message",
                {**self.common, "speaker": speaker, "message": message},
            )

    def _chapter_one_turns_before_combat(self) -> list[tuple[str, str]]:
        return [
            (
                "阿凛",
                "伊莉雅把碎月遗物固定在盾后，走进白花碑驿站的风铃廊。她先向守望会会长说明来意："
                "我们不是来夺走名字，而是想护送失忆旅人去钟鸣公国确认记忆被导向哪里。",
            ),
            (
                "南星",
                "赛璃不使用未掌握法术，只做普通调查：她观察旅人的呼吸、灰晶光泽和听到钟声时的反应，"
                "想判断记忆是否被导向灵魂中枢。她用洞察+意志，请公开所有骰子、修正值和目标值。",
            ),
            (
                "白河",
                "洛岚检查驿站旧钟与财团车辙，想找出第七采掘城巡逻队多久会抵达。"
                "他把发现立刻告诉守望会和队友，免得谈判拖到被包围。",
            ),
            (
                "时雨",
                "艾薇娅请求把接下来的会谈作为社交冲突处理：目标是说服白花守望会给旧路，但不让他们公开背锅。"
                "她提出证据和退路，带头推进目标命刻【争取守望会信任】。",
            ),
            (
                "澄砚",
                "苍祈调查风铃廊里有没有沉默森林奥灵留下的痕迹。"
                "他只想知道这些风铃刻名是否包括仍然活着的人，不强行发动法术；他用洞察+意志开放检定。",
            ),
            (
                "阿凛",
                "伊莉雅在自己的回合推进目标命刻【争取守望会信任】：她把碎月遗物放低，承诺先保护旅人再谈路线，"
                "并说明白花碑驿站把失去的名字刻在风铃内侧。她用力量+意志，难度等级由 GM 按场面决定。",
            ),
            (
                "南星",
                "赛璃计划一个御魂仪式【风铃回声】：学科御魂，效力轻微，范围小范围，"
                "效果是让风铃暂时回响昨夜经过驿站的脚步和名字，不直接伤害任何人。",
            ),
            (
                "南星",
                "赛璃为仪式【风铃回声】供能，使用洞察+意志推进仪式命刻。她把旅人的名字写在白花纸上，挂到风铃下。",
            ),
            (
                "白河",
                "洛岚协助推进仪式命刻【仪式：风铃回声】，用洞察+敏捷调整旧钟的共鸣，让风铃只回放公开经过的痕迹。",
            ),
            (
                "南星",
                "赛璃尝试完成仪式【风铃回声】。她确认命刻进度，再决定是否已经能把回声真正放出来。",
            ),
            (
                "时雨",
                "艾薇娅看向守望会会长，请对方给出明确答复：旧路能不能借给我们护送旅人离开。"
                "她同时把路线、补给和撤离顺序整理好，等答复落下再带队行动。",
            ),
            (
                "白河",
                "洛岚把注意力放在驿站旧路入口和可能通向钟塔遗迹的机关上。"
                "他重点看锁扣、水痕和退路，判断哪里会被潮水或财团封死。",
            ),
            (
                "澄砚",
                "苍祈用野性之语尝试和遗迹边缘的潮生藤交流，问它最近有没有见过财团机兵经过。"
                "他只等潮生藤回应，不把它当成硬数值优势。",
            ),
        ]

    def _chapter_one_combat_turns(self) -> list[tuple[str, str]]:
        return [
            (
                "阿凛",
                "伊莉雅挡在失忆旅人前面，攻击财团机兵，使用钢匕首近战攻击。请公开命中检定的属性、每颗骰子、修正值、物防和伤害。",
            ),
            (
                "南星",
                "赛璃执行防御行动并掩护失忆旅人，同时提醒守望会的人带孩子离开，儿童遇险淡出处理。",
            ),
            (
                "白河",
                "洛岚推进目标命刻【旧路闸门开启】，用洞察+敏捷拆开驿站旧闸门的财团封锁，成功后队伍能护送旅人离开。",
            ),
            (
                "时雨",
                "艾薇娅不攻击，她用游说家的方式妨碍监察官艾蕾娜：指出她所谓保护记忆其实是在剥夺选择。"
                "她的目标是让艾蕾娜动摇；请公开难度等级10和骰子。",
            ),
            (
                "澄砚",
                "苍祈尝试推进【旧路闸门开启】，他用奥灵留下的树皮名纹稳定门轴，让洛岚打开的缝隙不被潮声压回去。",
            ),
            (
                "阿凛",
                "伊莉雅不继续硬拼，她推进目标命刻【旧路闸门开启】，用力量+意志顶住闸门，把洛岚打开的缝隙撑住。",
            ),
            (
                "南星",
                "赛璃对伊莉雅施放治愈术，确认这是她掌握的御魂使法术，并公开施法消耗、检定或恢复量。",
            ),
            (
                "白河",
                "洛岚用铁锤敲击财团机兵的腿部联轴，按妨碍行动结算，目标是让它迟缓。",
            ),
            (
                "澄砚",
                "苍祈声明本场机会偏好：推进【旧路闸门开启】时，优先把机会用于【进展】。",
            ),
            (
                "时雨",
                "艾薇娅暂时收住话头，等自己的回合再向监察官艾蕾娜开口。"
                "她盯着财团机兵的动作，寻找能让谈判重新打开的破绽。",
            ),
            (
                "阿凛",
                "伊莉雅尝试带队结束冲突：她护着旅人和碎月遗物撤入旧路，请时悠根据旧路闸门与巡逻队逼近的局势判断能否收束。",
            ),
            (
                "白河",
                "洛岚启动工程【修复白花守望会旧式信号塔】，目标是让守望会能提前发现财团巡逻。"
                "赛璃和伊莉雅今天帮工，请按工程规则结算费用和进度。",
            ),
            (
                "澄砚",
                "下一段我这个玩家会缺席半小时，苍祈选择淡出场景去和沉默森林奥灵交涉，之后再回来。"
                "请按缺席玩家流程记录，不要让他替队伍做关键选择。",
            ),
        ]

    def _verify_no_direct_pc_injection(self) -> None:
        pcs = [character.name for character in self._runtime().app.character_manager.all() if "pc" in character.traits]
        self.notes.append(f"Session 0 发言后正式 PC：{pcs}")

    def _wait_for_async_map_if_any(self) -> None:
        status = self._runtime().app.world_map_generation_status()
        if status.get("status") != "generating":
            self._record_tool_event(
                "世界地图生成器",
                "第零章地图",
                "地图生成无需等待或尚未启动；记录当前状态供审计。",
                status,
            )
            return
        started = time.perf_counter()
        while status.get("status") == "generating" and time.perf_counter() - started < 260:
            time.sleep(2)
            status = self._runtime().app.world_map_generation_status()
        self.notes.append(f"异步地图生成等待结果：{status}")
        self._record_tool_event(
            "世界地图生成器",
            "第零章地图",
            "等待异步地图生成结束，确认玩家共创地点是否进入地图产物。",
            status,
        )

    def _start_chapter_scene(self) -> None:
        app = self._runtime().app
        if app.scene_manager.current_scene is None or app.scene_manager.current_scene.name.startswith("Session 0"):
            app.start_scene(
                "第一章：白花碑驿站的迟响",
                location="白花碑驿站",
                participants=self.pc_names,
                objective="说服白花守望会、保护失忆旅人，并避开财团巡逻队。",
                summary="从第零章共创的白花碑驿站切入第一章。",
            )
        package = app.world_state.active_chapter()
        if package is not None and not any(
            run.chapter_title == package.chapter_title for run in app.hero_log_manager.chapter_runs
        ):
            app.hero_log_manager.start_chapter_run(
                chapter_title=package.chapter_title,
                participants=self.pc_names,
                campaign_id=self.campaign_id,
                session_id=self.session_id,
                gm_name="时悠",
                synopsis=package.synopsis,
                intro_prompt=package.intro_prompt,
                conclusion_prompt=package.conclusion_prompt,
                shared_creation_slots=package.shared_creation_slots,
                iconic_elements=package.iconic_elements,
                timebox_minutes=package.timebox_minutes,
            )
        app.hero_log_manager.record_chapter_beat(
            "白花碑驿站的迟响",
            title="开场：风铃廊问路",
            beat_type="intro",
            status="running",
            expected_minutes=20,
            summary="第一章从白花碑驿站风铃廊开始，章节包开场场景被实际挂入运行记录。",
        )
        self._record_tool_event(
            "章节运行日志",
            "第一章开场",
            "开启章节运行记录，并把章节包的开场 beat 写入 HeroLogManager。",
            {
                "chapter_title": package.chapter_title if package else "白花碑驿站的迟响",
                "participants": list(self.pc_names),
                "current_scene": app.scene_manager.current_scene.name if app.scene_manager.current_scene else "",
            },
        )

    def _record_chapter_scene_tools_after_social_phase(self) -> None:
        app = self._runtime().app
        app.hero_log_manager.record_chapter_beat(
            "白花碑驿站的迟响",
            title="社交冲突：风铃廊问路",
            beat_type="social_conflict",
            status="done",
            expected_minutes=25,
            summary="玩家与白花守望会会长围绕旧路、旅人和财团压力交涉，章节包社交场景已实际使用。",
        )
        app.hero_log_manager.record_chapter_beat(
            "白花碑驿站的迟响",
            title="仪式：风铃回声",
            beat_type="ritual",
            status="done",
            expected_minutes=25,
            summary="赛璃计划并推进御魂仪式【风铃回声】，章节包仪式场景已实际使用。",
        )
        story_summary = app.story_arc_manager.prompt_summary()
        self._record_tool_event(
            "故事弧/反派压力管理",
            "第一章社交与仪式后",
            "从第零章的反派种子、世界威胁和谜团同步长期故事线，生成下一场议程与反派压力轨。",
            story_summary,
        )
        self._record_tool_event(
            "战役节奏控制",
            "第一章社交与仪式后",
            "CampaignPacingManager 根据当前战役阶段限制前台压力和自动推进命刻，避免前期多个威胁同时碾压玩家。",
            app.campaign_pacing_manager.audit_payload(),
        )
        self._record_tool_event(
            "当前场景框架",
            "第一章社交与仪式后",
            "SceneFrameManager 已根据玩家调查、NPC答复、仪式和线索池维护当前场景框架。",
            app.scene_frame_manager.audit_payload(include_private=True),
        )
        self._record_tool_event(
            "命刻管理",
            "第一章社交与仪式后",
            "ClockManager 维护目标命刻、威胁命刻与仪式命刻的当前公开进度。",
            {
                "raw_clocks": app.clock_manager.formatted_public(max_completed=3),
                "budgeted_public_clocks": app.campaign_pacing_manager.formatted_public_clocks(),
            },
            public=True,
        )

    def _exercise_villain_conflict_tools(self) -> None:
        app = self._runtime().app
        events: list[dict[str, Any]] = []
        if not app.character_manager.exists("监察官艾蕾娜"):
            self.errors.append("反派工具预检失败：缺少监察官艾蕾娜。")
            return
        if not app.conflict_manager.is_villain("监察官艾蕾娜"):
            app.conflict_manager.register_enemy("监察官艾蕾娜", EnemyRank.VILLAIN, ultima_points=3, action_count=2)
        appearance = app.conflict_manager.award_villain_appearance_fabula("监察官艾蕾娜")
        events.append({"tool": "反派登场物语点", "event": appearance.event_type, "summary": appearance.summary})
        if app.conflict_manager.state.ultima_points.get("监察官艾蕾娜", 0) > 0:
            trait = app.conflict_manager.spend_ultima_for_trait_invocation("监察官艾蕾娜")
            events.append({"tool": "终结点援用特质", "event": trait.event_type, "summary": trait.summary})
        if app.conflict_manager.state.ultima_points.get("监察官艾蕾娜", 0) > 0:
            recovery = app.conflict_manager.spend_ultima_to_recover("监察官艾蕾娜")
            events.append(
                {
                    "tool": "终结点恢复",
                    "event": recovery.event_type,
                    "summary": recovery.summary,
                    "mp_after": recovery.mp_after,
                }
            )
        if app.conflict_manager.state.ultima_points.get("监察官艾蕾娜", 0) > 0:
            app.character_manager.get("监察官艾蕾娜").hp = 0
            escape = app.conflict_manager.resolve_zero_hp("监察官艾蕾娜", villain_mode="auto")
            events.append({"tool": "反派 0HP 逃脱", "event": escape.event_type, "summary": escape.summary})
        app.hero_log_manager.record_chapter_beat(
            "白花碑驿站的迟响",
            title="冲突：旧路闸门与巡逻队",
            beat_type="conflict",
            status="done",
            expected_minutes=45,
            summary="冲突阶段覆盖反派、财团机兵、目标/威胁命刻、攻击、防御、妨碍和治疗。",
        )
        self._record_tool_event(
            "反派/终结点规则",
            "第一章冲突后",
            "监察官艾蕾娜作为反派实际进入 ConflictManager，并触发登场、终结点和 0HP 相关规则。",
            {
                "events": events,
                "remaining_ultima": app.conflict_manager.state.ultima_points.get("监察官艾蕾娜", 0),
                "villains": sorted(app.conflict_manager.state.villains),
                "conflict_log_tail": app.conflict_manager.format_combat_log(limit=8),
            },
            public=True,
        )

    def _prepare_conflict_state(self) -> None:
        app = self._runtime().app
        if not app.character_manager.exists("财团机兵"):
            app.character_manager.add(
                Character(
                    name="财团机兵",
                    level=5,
                    identity="辉钢财团安保构装体",
                    theme="控制",
                    origin="第七采掘城",
                    attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
                    max_hp=60,
                    hp=60,
                    max_mp=35,
                    mp=35,
                    crisis_threshold=30,
                    weapon_damage=10,
                    weapon_type="physical",
                    defenses={"physical": 10, "magic": 8},
                    affinities={"thunder": Affinity.WEAK, "earth": Affinity.RESIST},
                    traits=["enemy", "construct", "辉钢财团"],
                    weapon_accuracy_attributes=["MIG", "MIG"],
                    weapon_accuracy_modifier=1,
                    weapon_range="melee",
                )
            )
        if not app.character_manager.exists("监察官艾蕾娜"):
            app.character_manager.add(
                Character(
                    name="监察官艾蕾娜",
                    level=10,
                    identity="第七采掘城监察官",
                    theme="秩序",
                    origin="赤羽旧王都",
                    attributes={"DEX": 8, "INS": 10, "MIG": 6, "WLP": 10},
                    max_hp=90,
                    hp=90,
                    max_mp=70,
                    mp=70,
                    crisis_threshold=45,
                    weapon_damage=8,
                    weapon_type="dark",
                    defenses={"physical": 10, "magic": 10},
                    affinities={"light": Affinity.WEAK, "dark": Affinity.RESIST},
                    traits=["enemy", "villain", "humanoid", "辉钢财团", "赤羽遗民"],
                    weapon_accuracy_attributes=["INS", "WLP"],
                    weapon_accuracy_modifier=1,
                    weapon_range="ranged",
                    initiative=10,
                )
            )
        if not app.character_manager.exists("财团狙击手"):
            app.character_manager.add(
                Character(
                    name="财团狙击手",
                    level=5,
                    identity="辉钢财团远程护卫",
                    theme="服从",
                    origin="第七采掘城",
                    attributes={"DEX": 10, "INS": 8, "MIG": 6, "WLP": 6},
                    max_hp=45,
                    hp=45,
                    max_mp=30,
                    mp=30,
                    crisis_threshold=22,
                    weapon_damage=8,
                    weapon_type="physical",
                    defenses={"physical": 10, "magic": 8},
                    affinities={},
                    traits=["enemy", "humanoid", "辉钢财团"],
                    weapon_accuracy_attributes=["DEX", "INS"],
                    weapon_accuracy_modifier=0,
                    weapon_range="ranged",
                    initiative=9,
                )
            )
        for pc in self.pc_names:
            if not app.character_manager.exists(pc):
                self.errors.append(f"准备冲突时缺少正式 PC：{pc}")
        app.conflict_manager.register_enemy("财团机兵", EnemyRank.SOLDIER)
        app.conflict_manager.register_enemy("财团狙击手", EnemyRank.SOLDIER)
        app.conflict_manager.register_enemy("监察官艾蕾娜", EnemyRank.VILLAIN, ultima_points=3, action_count=2)
        if not app.clock_manager.exists("财团巡逻队逼近"):
            app.clock_manager.add(
                Clock(name="财团巡逻队逼近", max_segments=6, current=0, clock_type="threat", stakes="巡逻队包围白花碑驿站。")
            )
        if not app.clock_manager.exists("旧路闸门开启"):
            app.clock_manager.add(
                Clock(name="旧路闸门开启", max_segments=6, current=0, clock_type="objective", stakes="旧路开启后队伍可撤离冲突。")
            )
        if not app.clock_manager.exists("艾蕾娜启动记忆集中协议"):
            app.clock_manager.add(
                Clock(
                    name="艾蕾娜启动记忆集中协议",
                    max_segments=8,
                    current=1,
                    clock_type="villain",
                    stakes="填满后艾蕾娜能把失忆旅人的记忆上传到第七采掘城。",
                    auto_advance="每轮结束推进1格",
                )
            )
        path = app.save_campaign_memory(self.campaign_id)
        self._runtime().last_saved_path = str(path)
        self.notes.append("测试脚本建立敌方对象，以覆盖硬规则战斗流程；PC 仍由 Session 0 发言创建。")
        self._record_tool_event(
            "遭遇/冲突准备",
            "第一章冲突启动前",
            "创建财团机兵、财团狙击手与反派监察官艾蕾娜，并登记反派终结点、目标命刻和行动次数。",
            {
                "enemies": ["财团机兵", "财团狙击手", "监察官艾蕾娜"],
                "villain_ultima": app.conflict_manager.state.ultima_points.get("监察官艾蕾娜"),
                "enemy_action_counts": dict(app.conflict_manager.state.enemy_action_counts),
                "clocks": app.clock_manager.formatted_public(max_completed=2),
                "save_path": str(path),
            },
        )
        if app.conflict_manager.state.active:
            app.conflict_manager.end_scene()
            self.notes.append("测试脚本结束前一段社交冲突，重新开启遭遇战以覆盖攻击、防御和敌方行动。")
        if not app.conflict_manager.state.active:
            self.invoke(
                "第一章冲突启动",
                "POST",
                "/v1/game/turn",
                {
                    **self.common,
                    "speaker": "阿凛",
                    "message": (
                        "监察官艾蕾娜带着财团机兵和财团狙击手拦住旧路，我们进入冲突场景【白花碑驿站伏击】。"
                        "玩家方是伊莉雅、赛璃、洛岚、艾薇娅、苍祈；敌方是监察官艾蕾娜、财团机兵、财团狙击手。"
                        "伊莉雅举盾喊出警戒，赛璃、洛岚、艾薇娅、苍祈一起支援她判断先手，"
                        "按敏捷+洞察先攻团队检定处理。"
                    ),
                },
            )
        if not app.conflict_manager.state.active:
            participants = [
                name
                for name in [self.pc_names[0], "监察官艾蕾娜", self.pc_names[1], "财团机兵", self.pc_names[2], "财团狙击手", self.pc_names[3], self.pc_names[4]]
                if app.character_manager.exists(name)
            ]
            app.conflict_manager.start_scene("白花碑驿站伏击", participants)
            self.notes.append("自然语言启动冲突未成功，已使用手动冲突场景兜底。")

    def _exercise_core_design_tools(self) -> None:
        """Cover GM-facing design helpers that are hard to trigger organically in one scene."""

        runtime = self._runtime()
        app = runtime.app
        rules_engine = RulesEngine(seed=90210)
        sandbox_clock_manager = ClockManager()
        dungeon_manager = DungeonManager(sandbox_clock_manager, rules_engine)
        dungeon_brief = dungeon_manager.design_dungeon(
            "白钟地下水道",
            importance=DungeonImportance.MAJOR,
            preparation=DungeonPreparation.PREPARED,
            purpose="追查财团把记忆燃料运出驿站的暗渠。",
            rolls={"concept": 19, "focus": 11, "inhabitants": 12, "peculiarity": 15},
        )
        dungeon_state = dungeon_manager.start_from_brief(dungeon_brief, location="白花碑驿站地下")

        sandbox_conflict_manager = ConflictManager(app.character_manager)
        encounter_manager = EncounterManager(app.character_manager, sandbox_conflict_manager)
        encounter_design = encounter_manager.design_encounter(
            self.pc_names,
            difficulty=EncounterDifficulty.BOSS,
            boss=True,
        )
        npc_draft = encounter_manager.design_npc(
            "白钟巡逻队长",
            level=10,
            species="humanoid",
            traits=["纪律严明", "谨慎", "记忆债", "怕失控"],
            attribute_spread="standard",
            rank=EnemyRank.ELITE,
            weaknesses=["light"],
            selected_skill_names=["特殊攻击", "反应"],
        )

        economy_manager = EconomyManager(app.character_manager, app.world_state, rules_engine)
        rare_design = economy_manager.design_rare_weapon(
            "白钟枪剑",
            "青铜剑",
            damage_type="light",
            quality_names=["穿透"],
            description="以青铜剑为规则基底、以白钟与碎月仪式为外观主题的稀有枪剑。",
        )
        rare_approval = app.hero_log_manager.request_rare_item_design_approval(
            rare_design,
            requester="长测脚本",
            source="核心规则稀有装备设计预检",
            notes=[
                "长测覆盖：初始装备不合概念时，可基于基础武器设计稀有/改名版本，但需团友同意。",
                "不直接发给玩家，作为 GM 审批记录与仪表盘证据。",
            ],
        )
        app.story_arc_manager.sync_from_world_profile()
        story_before = app.story_arc_manager.prompt_summary()
        advanced_pressure = None
        if app.story_arc_manager.state.villain_pressure:
            track = app.story_arc_manager.state.villain_pressure[0]
            advanced_pressure = app.story_arc_manager.advance_villain_pressure(
                track.track_id,
                amount=1,
                reason="长测审计：艾蕾娜在第一章后继续推动记忆集中管理。",
            )

        rest_result = app.take_rest(
            RestType.SETTLEMENT,
            safe_source="白花碑驿站安全厢房",
            threat_clocks=["艾蕾娜启动记忆集中协议"] if app.clock_manager.exists("艾蕾娜启动记忆集中协议") else [],
        )
        journey = app.travel(
            origin="白花碑驿站",
            destination="钟鸣公国",
            threat_levels=[TravelThreatLevel.LOW, TravelThreatLevel.MEDIUM],
            regions=["雾潮海岸", "镜线内海北岸"],
            distance=2,
            route_type=TravelRouteType.LAND,
            transport="徒步",
            party_size=len(self.pc_names),
        )
        app.dungeon_manager.start_from_brief(dungeon_brief, location="白花碑驿站地下")
        dungeon_enter = app.explore_dungeon_area(
            actor="洛岚",
            action="enter",
            success=True,
            note="队伍从旧路退入地下暗渠，确认入口、出口和第一处危险。",
        )
        dungeon_search = app.explore_dungeon_area(
            "宝箱侧室",
            actor="苍祈",
            action="open_treasure",
            success=True,
            collect_treasure=True,
            note="苍祈找到一枚刻有失踪岛名的白钟碎片。",
        )
        dungeon_boss = app.explore_dungeon_area(
            "Boss房",
            actor="伊莉雅",
            action="confront_boss",
            success=True,
            clear_area=False,
            note="此处只揭示核心威胁，不在长测中硬打一场完整 Boss。",
        )
        app.dungeon_manager.end_dungeon("长测地下城段落结束：队伍取得暗渠线索并保留 Boss 钩子。")

        if app.character_manager.exists("洛岚"):
            app.character_manager.get("洛岚").zenit = max(app.character_manager.get("洛岚").zenit, 1000)
        project = app.start_project(
            inventor="洛岚",
            name="白花旧式信号塔修复",
            potency=RitualPotency.MINOR,
            scope=RitualScope.SMALL,
            use=ProjectUse.PERMANENT,
            effect="守望会可以提前发现财团巡逻，之后同类威胁命刻初始格数降低。",
            output_type=PersistentChangeType.FACILITY,
            location="白花碑驿站",
            material_credit=100,
        )
        project_progress = app.work_on_project(project.name, ["洛岚", "伊莉雅", "赛璃"], days=1)

        chapter_manager = ChapterManager(
            app.progression_manager,
            app.interceptor.economy_manager,
            app.world_state,
            app.hero_log_manager,
        )
        chapter_settlement = chapter_manager.settle_chapter(
            chapter_title="白花碑驿站的迟响",
            participating_pcs=self.pc_names,
            party_level=5,
            ultima_spent=3,
            fabula_spent=5,
            difficulty="hard",
            rare_item="白钟枪剑设计图",
        )
        self._record_tool_event(
            "核心规则覆盖工具组",
            "收团后规则预检",
            "覆盖旅行、休息、地下城、工程、章节结算、经验奖励、故事弧压力推进和稀有装备审批。",
            {
                "story_arc_before": story_before,
                "advanced_villain_pressure": {
                    "villain": advanced_pressure.villain,
                    "current": advanced_pressure.current,
                    "segments": advanced_pressure.segments,
                    "last_action": advanced_pressure.last_action,
                }
                if advanced_pressure
                else None,
                "rest": {
                    "summary": rest_result.summary,
                    "threat_clock_changes": [change.__dict__ for change in rest_result.threat_clock_changes],
                },
                "travel": {
                    "summary": journey.summary,
                    "days": journey.days,
                    "day_results": [
                        {
                            "day": day.day,
                            "region": day.region,
                            "threat_level": day.threat_level.value,
                            "roll": day.roll,
                            "event_type": day.event_type.value,
                            "summary": day.summary,
                        }
                        for day in journey.day_results
                    ],
                },
                "dungeon_exploration": [
                    dungeon_enter.summary,
                    dungeon_search.summary,
                    dungeon_boss.summary,
                ],
                "project": {
                    "name": project.name,
                    "material_cost": project.material_cost,
                    "required_progress": project.required_progress,
                    "progress_summary": project_progress.summary,
                    "completed": project_progress.completed,
                },
                "chapter_settlement": chapter_settlement.summary,
            },
        )

        self.core_design_tool_payload = {
            "spell_alias": {
                "deprecated": "电流术",
                "canonical": normalize_spell_name("电流术"),
                "canonical_names_include": "闪电击" in canonical_spell_names(),
                "matching_accepts_alias": "电流术" in spell_matching_candidates(),
            },
            "dungeon": {
                "name": dungeon_brief.name,
                "mode": dungeon_brief.recommended_mode.value,
                "flow_checklist": list(dungeon_brief.flow_checklist),
                "state_notes": list(dungeon_state.notes),
                "danger_clocks": dict(dungeon_brief.danger_clocks),
            },
            "encounter": {
                "summary": encounter_design.summary,
                "risk_checks": list(encounter_design.risk_checks),
                "enemy_mix": list(encounter_design.enemy_mix),
            },
            "npc_design": {
                "name": npc_draft.name,
                "rank": npc_draft.rank.value,
                "skill_budget": npc_draft.skill_budget,
                "design_checklist": list(npc_draft.design_checklist),
            },
            "rare_item": {
                "name": rare_design.name,
                "price": rare_design.price,
                "approval_status": rare_approval.status,
                "approval_effects": list(rare_approval.effects),
                "approval_notes": list(rare_approval.notes),
            },
            "story_arc": {
                "villain_pressure_count": len(app.story_arc_manager.state.villain_pressure),
                "advanced_pressure": advanced_pressure.current if advanced_pressure else None,
            },
            "travel": {
                "days": journey.days,
                "event_types": [day.event_type.value for day in journey.day_results],
                "summary": journey.summary,
            },
            "rest": {
                "summary": rest_result.summary,
                "threat_clock_changes": [change.__dict__ for change in rest_result.threat_clock_changes],
            },
            "dungeon_runtime": {
                "entered_area": dungeon_enter.area_name,
                "treasure_area": dungeon_search.area_name,
                "boss_area": dungeon_boss.area_name,
            },
            "project": {
                "name": project.name,
                "completed": project_progress.completed,
                "summary": project_progress.summary,
            },
            "chapter_settlement": {
                "summary": chapter_settlement.summary,
                "total_xp": chapter_settlement.experience_report.total_xp,
                "reward_zenit": chapter_settlement.reward.zenit,
                "level_up_available": list(chapter_settlement.level_up_available),
            },
        }
        self.notes.append("核心设计工具预检完成：地下城流程、遭遇风险、NPC 八步设计、稀有装备审批、旅行/休息/工程/章节结算和法术别名规范。")

    def _snapshot(self, *, include_private: bool = False) -> dict[str, Any]:
        route = f"/v1/campaigns/{self.campaign_id}/snapshot?" + urlencode({"include_private": str(include_private).lower()})
        return self.invoke("读取 Session 0 快照", "GET", route)

    def _audit_route(self, *, limit: int = 200) -> str:
        return "/v1/audit/dashboard?" + urlencode(
            {
                "campaign_id": self.campaign_id,
                "session_id": self.session_id,
                "channel_id": self.channel_id,
                "include_private": "true",
                "limit": str(limit),
            }
        )

    def _runtime(self):
        return self.service._runtime(self.campaign_id)

    def _append_conversation(self, record: dict[str, Any]) -> None:
        lines = [
            f"--- {record['index']:02d}. {record['label']} | {record['elapsed_ms']}ms | "
            f"status={record['status']} ok={record['ok']} ---"
        ]
        control_routes = {"/v1/game/scene-opening", "/v1/session/gate", "/v1/campaigns/new"}
        if record["route"] not in control_routes and (record["speaker"] or record["message"]):
            lines.append(f"{record['speaker']}: {record['message']}")
        if record["reply"] and record["route"] not in {"/v1/session/gate", "/v1/campaigns/new"}:
            lines.append(f"时悠: {record['reply']}")
        if record["blocked"]:
            lines.append(
                "状态: "
                + json.dumps(
                    {"blocked": record["blocked"]},
                    ensure_ascii=False,
                )
            )
        lines.append("")
        with self.conversation_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def _write_public_transcript_copy(self) -> None:
        audit = getattr(self, "audit", {})
        path_text = audit.get("logs", {}).get("transcript_txt_path") if isinstance(audit, dict) else ""
        if not path_text:
            return
        transcript = Path(path_text)
        if not transcript.exists():
            return
        copy_path = self.run_root / "session_transcript_copy.txt"
        copy_path.write_text(transcript.read_text(encoding="utf-8"), encoding="utf-8")
        self.notes.append(f"已复制正式 transcript：{copy_path}")

    def _build_report(self, *, exception: Exception | None) -> dict[str, Any]:
        if exception is not None:
            self.errors.append(f"{exception.__class__.__name__}: {exception}")
        audit = getattr(self, "audit", {})
        if not isinstance(audit, dict):
            audit = {}
        runtime = self._runtime()
        world = runtime.app.world_state.world_profile
        chars = [character.name for character in runtime.app.character_manager.all()]
        pcs = [character.name for character in runtime.app.character_manager.all() if "pc" in character.traits]
        elapsed_values = [int(call["elapsed_ms"]) for call in self.calls]
        slowest = sorted(self.calls, key=lambda item: int(item.get("elapsed_ms", 0)), reverse=True)[:10]
        transcript_path = Path(audit.get("logs", {}).get("transcript_txt_path") or "")
        dashboard_phase = audit.get("phase", {}) if isinstance(audit.get("phase"), dict) else {}
        chapter_package = audit.get("chapter_package", {}) if isinstance(audit.get("chapter_package"), dict) else {}
        map_status = (
            getattr(self, "gate_body", {}).get("world_map")
            or runtime.app.world_map_generation_status()
            or audit.get("world_map")
        )
        silent_discussion_calls = [call for call in self.calls if "自由讨论静默" in str(call.get("label") or "")]
        substantive_discussion_calls = [call for call in self.calls if "自由讨论实质贡献" in str(call.get("label") or "")]
        tool_names = {str(event.get("tool") or "") for event in self.tool_events}
        tool_event_text = json.dumps(self.tool_events, ensure_ascii=False, default=str)
        checks = {
            "used_real_llm_service": self.service.use_llm is True,
            "session_zero_world_ready_before_gate": bool(getattr(self, "pre_gate_world_ready", False)),
            "session_zero_hero_ready_before_gate": bool(getattr(self, "pre_gate_hero_status", {}).get("ready", False)),
            "gate_not_blocked": not bool(getattr(self, "gate_body", {}).get("blocked")),
            "official_transcript_txt_exists": transcript_path.exists(),
            "map_generation_attempted": bool(map_status),
            "map_generated_or_ready": isinstance(map_status, dict) and map_status.get("status") in {"generated", "ready"},
            "phase_not_session_zero_after_end": dashboard_phase.get("current_scene") != "Session 0 世界创建",
            "has_clock_coverage": any("命刻" in call["message"] or "命刻" in call["reply"] for call in self.calls),
            "has_ritual_coverage": any("仪式" in call["message"] or "仪式" in call["reply"] for call in self.calls),
            "has_combat_coverage": any("攻击" in call["message"] or "战斗" in call["reply"] or "冲突" in call["reply"] for call in self.calls),
            "free_discussion_silent_covered": len(silent_discussion_calls) >= 5,
            "free_discussion_substantive_covered": bool(substantive_discussion_calls),
            "no_gm_interjection_on_free_discussion": all(
                call["route"] == "/v1/message/route"
                and call["body"].get("target") == "silent"
                and not bool(call["body"].get("send_reply"))
                and not str(call["reply"]).strip()
                for call in silent_discussion_calls
            ),
            "substantive_session_zero_discussion_reaches_gm": all(
                call["route"] == "/v1/message/route"
                and call["body"].get("target") == "fu_gm"
                and bool(call["body"].get("send_reply"))
                and bool(str(call["reply"]).strip())
                for call in substantive_discussion_calls
            ),
            "has_roll_detail_output": any(
                "骰" in call["reply"]
                and ("难度等级" in call["reply"] or "目标" in call["reply"] or "物防" in call["reply"] or "魔防" in call["reply"])
                for call in self.calls
            ),
            "no_world_shape_question": not any("世界形状" in call["reply"] for call in self.calls),
            "no_internal_world_style_public": not any("世界风格" in call["reply"] or "地图形式" in call["reply"] for call in self.calls),
            "no_mechanical_hero_prompt": not any(
                any(token in call["reply"] for token in ("还没听到", "英雄概念", "角色轮廓已经", "英雄轮廓已经"))
                for call in self.calls
            ),
            "no_repeated_theme_clarification": sum("伊莉雅的主题“责任”" in call["reply"] for call in self.calls) <= 1,
            "no_public_backend_summary_labels": not any(
                any(token in call["reply"] for token in ("英雄草稿", "GM私密暗线", "Session 0 摘要"))
                for call in self.calls
            ),
            "no_public_backend_terms": not any(
                any(
                    token in call["reply"]
                    for token in (
                        "SellItem",
                        "硬状态",
                        "GM应",
                        "保持冲突继续",
                        "ActionType",
                        "fade_out",
                        "硬成本",
                        "规则层",
                        "用途：",
                        "完成后写入：",
                        "预定持有者",
                        "暂时没有执行明确动作",
                    )
                )
                for call in self.calls
            ),
            "no_irrelevant_spell_blocker_for_trade": not any(
                "出售" in call["message"] and "已掌握的法术" in call["reply"] for call in self.calls
            ),
            "no_unmet_conditional_scene_close": all(
                ("还差" in call["reply"] or "还没满足" in call["reply"] or "条件还没" in call["reply"])
                for call in self.calls
                if "如果旧路闸门已经打开" in call["message"]
            ),
            "no_absent_player_misroute_to_conflict": not any(
                "缺席" in call["message"] and ("冲突正式开始" in call["reply"] or "重新初始化冲突" in call["reply"])
                for call in self.calls
            ),
            "group_concept_preserved": world.group_concept.startswith("临时守护者"),
            "no_healing_misroute": not any(
                "治愈术" in call["message"] and "援用特质" in call["reply"] for call in self.calls
            ),
            "no_sticky_opportunity_preference": not self._has_sticky_opportunity_preference(),
            "no_repeated_out_of_turn_deadlock": not self._has_repeated_out_of_turn_deadlock(),
            "chapter_opening_described_scene": self._chapter_opening_described_scene(),
            "npc_answer_only_on_request": self._npc_answer_only_on_request(),
            "no_generic_investigation_target": not any(
                " 对 当前目标 的检定" in call["reply"] or " 对 当前线索 的检定" in call["reply"]
                for call in self.calls
            ),
            "no_literal_current_clock": not any(
                "命刻【当前命刻】" in call["message"] or "命刻【当前命刻】" in call["reply"]
                for call in self.calls
            ),
            "chapter_package_active": bool(chapter_package.get("active")),
            "chapter_package_registered": "白花碑驿站的迟响" in set(chapter_package.get("registered_packages") or []),
            "iconic_elements_registered": len(chapter_package.get("iconic_elements") or []) >= 5,
            "spell_alias_resolves_to_canonical": self._spell_alias_resolves_to_canonical(),
            "dungeon_flow_checklist_covered": self._dungeon_flow_checklist_covered(),
            "encounter_risk_checks_covered": self._encounter_risk_checks_covered(),
            "npc_design_checklist_covered": self._npc_design_checklist_covered(),
            "rare_item_design_approval_covered": self._rare_item_design_approval_covered(),
            "tool_trace_records_router": "AstrBot/QQ 路由器" in tool_names,
            "tool_trace_records_chapter_package": "章节包管理" in tool_names and "章节运行日志" in tool_names,
            "tool_trace_records_scene_frame": "当前场景框架" in tool_names,
            "tool_trace_records_campaign_pacing": "战役节奏控制" in tool_names,
            "tool_trace_records_villain": "反派/终结点规则" in tool_names and "反派 0HP 逃脱" in tool_event_text,
            "tool_trace_records_core_rule_tools": "核心规则覆盖工具组" in tool_names,
            "tool_trace_records_map": "世界地图生成器" in tool_names,
            "major_core_systems_covered": all(
                token in tool_event_text
                for token in (
                    "第零章",
                    "世界地图生成器",
                    "章节包",
                    "SceneFrameManager",
                    "CampaignPacingManager",
                    "ClockManager",
                    "反派登场物语点",
                    "终结点",
                    "旅行",
                    "休息",
                    "地下城",
                    "工程",
                    "章节结算",
                    "稀有装备",
                    "法术",
                )
            ),
        }
        if not checks["session_zero_world_ready_before_gate"]:
            self.errors.append("冒险门控前世界创建未 ready。")
        if not checks["phase_not_session_zero_after_end"]:
            self.errors.append("收团后审计面板仍显示 Session 0 世界创建。")
        if not checks["session_zero_hero_ready_before_gate"]:
            self.errors.append(f"冒险门控前角色未 ready：{getattr(self, 'pre_gate_hero_status', {})}")
        if not checks["has_roll_detail_output"]:
            self.errors.append("长跑未捕获到包含骰子和目标值/物防的公开检定明细。")
        if not checks["free_discussion_silent_covered"]:
            self.errors.append("长测没有覆盖足够的自由讨论静默样本。")
        if not checks["free_discussion_substantive_covered"]:
            self.errors.append("长测没有覆盖第零章实质共创经群入口转入 GM 的样本。")
        if not checks["no_gm_interjection_on_free_discussion"]:
            self.errors.append("自由讨论样本中 FU-GM 仍出现不合时宜插话。")
        if not checks["substantive_session_zero_discussion_reaches_gm"]:
            self.errors.append("第零章实质共创没有经群入口正确交给 FU-GM。")
        if not checks["no_mechanical_hero_prompt"]:
            self.errors.append("第零章仍出现机械式角色缺项提醒。")
        if not checks["no_repeated_theme_clarification"]:
            self.errors.append("第零章主题澄清追问重复出现。")
        if not checks["no_public_backend_summary_labels"]:
            self.errors.append("公开回复仍出现后台摘要标签。")
        if not checks["no_public_backend_terms"]:
            self.errors.append("公开回复仍出现后台动作名或硬状态术语。")
        if not checks["no_irrelevant_spell_blocker_for_trade"]:
            self.errors.append("出售/后勤动作失败时仍出现不相关的法术提示。")
        if not checks["no_unmet_conditional_scene_close"]:
            self.errors.append("未满足的条件式撤离请求被当作已经成功收束。")
        if not checks["no_absent_player_misroute_to_conflict"]:
            self.errors.append("缺席/淡出消息被误判成重新开始冲突。")
        if not checks["group_concept_preserved"]:
            self.errors.append(f"小队原型被模板覆盖：{world.group_concept}")
        if not checks["no_healing_misroute"]:
            self.errors.append("显式施放治愈术时被旧的援用特质窗口劫持。")
        if not checks["no_sticky_opportunity_preference"]:
            self.errors.append("机会偏好回复污染了后续玩家行动。")
        if not checks["no_repeated_out_of_turn_deadlock"]:
            self.errors.append("连续多次命中同一当前行动者的回合外暂缓提示，疑似冲突回合死锁。")
        if not checks["chapter_opening_described_scene"]:
            self.errors.append("第一章 GM 开场没有真正描述现场，疑似复述玩家请求。")
        if not checks["npc_answer_only_on_request"]:
            self.errors.append("NPC 明确答复在非询问回合重复出现。")
        if not checks["no_generic_investigation_target"]:
            self.errors.append("调查检定仍显示为“当前目标/当前线索”，没有落到具体对象。")
        if not checks["no_literal_current_clock"]:
            self.errors.append("长测中出现字面量【当前命刻】，说明测试 PL 或动作解析没有绑定真实命刻。")
        if not checks["chapter_package_active"]:
            self.errors.append("长测没有激活章节包。")
        if not checks["chapter_package_registered"]:
            self.errors.append("长测章节包未出现在仪表盘 registered_packages。")
        if not checks["iconic_elements_registered"]:
            self.errors.append("长测没有登记足够的标志性元素。")
        if not checks["spell_alias_resolves_to_canonical"]:
            self.errors.append("法术旧译名没有在长测预检中解析到规则书正式名称。")
        if not checks["dungeon_flow_checklist_covered"]:
            self.errors.append("地下城长测预检没有覆盖入口信息、探索循环、失败后果和 Boss 预兆。")
        if not checks["encounter_risk_checks_covered"]:
            self.errors.append("遭遇设计长测预检没有覆盖叙事目的、伤害预算、相性透明度或 Boss 检查。")
        if not checks["npc_design_checklist_covered"]:
            self.errors.append("NPC 设计长测预检没有覆盖八步流程、技能预算或调查揭示门槛。")
        if not checks["rare_item_design_approval_covered"]:
            self.errors.append("稀有装备设计没有进入审批记录，长测缺少改名/自定义装备审计证据。")
        if not checks["tool_trace_records_router"]:
            self.errors.append("工具轨迹缺少 AstrBot/QQ 路由器记录。")
        if not checks["tool_trace_records_chapter_package"]:
            self.errors.append("工具轨迹缺少章节包注册或章节运行记录。")
        if not checks["tool_trace_records_scene_frame"]:
            self.errors.append("工具轨迹缺少当前场景框架记录。")
        if not checks["tool_trace_records_campaign_pacing"]:
            self.errors.append("工具轨迹缺少战役节奏控制记录。")
        if not checks["tool_trace_records_villain"]:
            self.errors.append("工具轨迹缺少反派/终结点完整链路。")
        if not checks["tool_trace_records_core_rule_tools"]:
            self.errors.append("工具轨迹缺少核心规则覆盖工具组记录。")
        if not checks["tool_trace_records_map"]:
            self.errors.append("工具轨迹缺少地图生成器记录。")
        if not checks["major_core_systems_covered"]:
            self.errors.append("长测工具轨迹没有覆盖核心规则书主要系统。")
        if getattr(self, "gate_body", {}).get("blocked"):
            self.errors.append("进入第一章时仍被角色创建门控阻挡。")
        if elapsed_values:
            avg_ms = int(mean(elapsed_values))
        else:
            avg_ms = 0
        return {
            "ok": not self.errors,
            "campaign_id": self.campaign_id,
            "session_id": self.session_id,
            "channel_id": self.channel_id,
            "started_at": self.stamp,
            "errors": self.errors,
            "notes": self.notes,
            "checks": checks,
            "latency": {
                "count": len(elapsed_values),
                "total_ms": sum(elapsed_values),
                "avg_ms": avg_ms,
                "max_ms": max(elapsed_values) if elapsed_values else 0,
                "slowest": [
                    {
                        "index": call["index"],
                        "label": call["label"],
                        "route": call["route"],
                        "elapsed_ms": call["elapsed_ms"],
                        "status": call["status"],
                        "ok": call["ok"],
                    }
                    for call in slowest
                ],
            },
            "world": {
                "campaign_title": world.campaign_title,
                "continent_name": world.continent_name,
                "completed": world.completed,
                "starting_region": world.starting_region,
                "kingdoms": dict(world.kingdoms),
                "factions": dict(world.factions),
                "major_locations": dict(world.major_locations),
                "hero_drafts": {
                    key: {
                        "player_name": draft.player_name,
                        "hero_name": draft.hero_name,
                        "confirmed": draft.confirmed,
                        "classes": dict(draft.classes),
                    }
                    for key, draft in world.hero_drafts.items()
                },
            },
            "characters": {"all": chars, "pcs": pcs},
            "gate": getattr(self, "gate_body", {}),
            "map_status": map_status,
            "dashboard_phase": dashboard_phase,
            "chapter_package": chapter_package,
            "tool_events": self.tool_events,
            "core_design_tools": getattr(self, "core_design_tool_payload", {}),
            "dashboard_runtime": audit.get("runtime", {}),
            "astrbot_bridge": audit.get("astrbot_bridge", {}),
            "telemetry": audit.get("telemetry", {}),
            "artifacts": {
                "run_root": str(self.run_root),
                "conversation_txt": str(self.conversation_path),
                "conversation_export_txt": str(self.conversation_export_path),
                "progress_jsonl": str(self.progress_path),
                "report_json": str(self.report_json_path),
                "report_txt": str(self.report_txt_path),
                "official_transcript_txt": str(transcript_path) if transcript_path else "",
                "map_output_dir": str(self.map_root),
            },
            "calls": self.calls,
        }

    def _has_sticky_opportunity_preference(self) -> bool:
        consecutive = 0
        for call in self.calls:
            reply = str(call.get("reply") or "")
            message = str(call.get("message") or "")
            if "机会偏好" in reply:
                consecutive += 1
                if consecutive >= 2:
                    return True
                if "机会" not in message and "大成功" not in message and "大失败" not in message:
                    return True
            elif reply.strip():
                consecutive = 0
        return False

    def _has_repeated_out_of_turn_deadlock(self) -> bool:
        last_actor = ""
        consecutive = 0
        pattern = re.compile(r"现在轮到【(?P<actor>[^】]+)】行动；【[^】]+】的动作先不结算")
        for call in self.calls:
            reply = str(call.get("reply") or "")
            match = pattern.search(reply)
            if not match:
                consecutive = 0
                last_actor = ""
                continue
            actor = match.group("actor")
            if actor in self.pc_names:
                consecutive = 0
                last_actor = ""
                continue
            if actor == last_actor:
                consecutive += 1
            else:
                last_actor = actor
                consecutive = 1
            if consecutive >= 3:
                return True
        return False

    def _chapter_opening_described_scene(self) -> bool:
        opening = next((call for call in self.calls if call.get("label") == "第一章 GM 开场"), None)
        if not opening:
            return False
        reply = str(opening.get("reply") or "").strip()
        message = str(opening.get("message") or "").strip()
        if not reply or reply == message:
            return False
        return "白花碑驿站" in reply and any(token in reply for token in ("风铃", "巡守", "驿卒", "财团", "失忆旅人"))

    def _npc_answer_only_on_request(self) -> bool:
        answer_marker = "白花守望会会长终于给出答复"
        answer_calls = [call for call in self.calls if answer_marker in str(call.get("reply") or "")]
        if not answer_calls:
            return False
        for call in answer_calls:
            message = str(call.get("message") or "")
            if "明确答复" not in message and "能不能借" not in message and "旧路能不能" not in message:
                return False
        return True

    def _spell_alias_resolves_to_canonical(self) -> bool:
        payload = getattr(self, "core_design_tool_payload", {}).get("spell_alias", {})
        return (
            payload.get("canonical") == "闪电击"
            and payload.get("canonical_names_include") is True
            and payload.get("matching_accepts_alias") is True
        )

    def _dungeon_flow_checklist_covered(self) -> bool:
        dungeon = getattr(self, "core_design_tool_payload", {}).get("dungeon", {})
        text = "\n".join(dungeon.get("flow_checklist") or []) + "\n" + "\n".join(dungeon.get("state_notes") or [])
        required = ("入口", "失败", "Boss", "停顿")
        return all(token in text for token in required)

    def _encounter_risk_checks_covered(self) -> bool:
        encounter = getattr(self, "core_design_tool_payload", {}).get("encounter", {})
        text = "\n".join(encounter.get("risk_checks") or [])
        required = ("叙事目的", "伤害预算", "相性", "透明度", "Boss")
        return all(token in text for token in required)

    def _npc_design_checklist_covered(self) -> bool:
        npc = getattr(self, "core_design_tool_payload", {}).get("npc_design", {})
        text = "\n".join(npc.get("design_checklist") or [])
        return all(token in text for token in ("概念", "技能预算", "调查透明度", "阶级检查"))

    def _rare_item_design_approval_covered(self) -> bool:
        rare_item = getattr(self, "core_design_tool_payload", {}).get("rare_item", {})
        notes = "\n".join(rare_item.get("approval_notes") or [])
        effects = "\n".join(rare_item.get("approval_effects") or [])
        return (
            rare_item.get("approval_status") == "pending"
            and "白钟枪剑" == rare_item.get("name")
            and "基础模板" in notes
            and "穿透" in effects
        )

    def _write_report(self, report: dict[str, Any]) -> None:
        if self.conversation_path.exists():
            self.conversation_export_path.write_text(self.conversation_path.read_text(encoding="utf-8"), encoding="utf-8")
        self.report_json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self.report_txt_path.write_text(self._format_report(report), encoding="utf-8")

    def _format_report(self, report: dict[str, Any]) -> str:
        lines = [
            "FU-GM 从零开始超长测试报告",
            f"campaign_id: {report['campaign_id']}",
            f"session_id: {report['session_id']}",
            f"ok: {report['ok']}",
            "",
            "=== 检查项 ===",
        ]
        for key, value in report["checks"].items():
            lines.append(f"- {key}: {value}")
        lines.extend(
            [
                "",
                "=== 错误 ===",
                *([f"- {item}" for item in report["errors"]] or ["- 无"]),
                "",
                "=== 备注 ===",
                *([f"- {item}" for item in report["notes"]] or ["- 无"]),
                "",
                "=== 延迟统计 ===",
                f"调用数: {report['latency']['count']}",
                f"总耗时: {report['latency']['total_ms']}ms",
                f"平均耗时: {report['latency']['avg_ms']}ms",
                f"最大耗时: {report['latency']['max_ms']}ms",
                "最慢调用:",
            ]
        )
        for item in report["latency"]["slowest"]:
            lines.append(
                f"- #{item['index']} {item['label']} {item['route']} "
                f"{item['elapsed_ms']}ms status={item['status']} ok={item['ok']}"
            )
        lines.extend(
            [
                "",
                "=== 地图状态 ===",
                json.dumps(report["map_status"], ensure_ascii=False, indent=2, default=str),
                "",
                "=== 收团后阶段 ===",
                json.dumps(report["dashboard_phase"], ensure_ascii=False, indent=2, default=str),
                "",
                "=== 章节包 ===",
                json.dumps(report.get("chapter_package", {}), ensure_ascii=False, indent=2, default=str),
                "",
                "=== 工具发挥轨迹 ===",
                json.dumps(report.get("tool_events", []), ensure_ascii=False, indent=2, default=str),
                "",
                "=== 核心设计工具预检 ===",
                json.dumps(report.get("core_design_tools", {}), ensure_ascii=False, indent=2, default=str),
                "",
                "=== 产物 ===",
            ]
        )
        for key, value in report["artifacts"].items():
            lines.append(f"- {key}: {value}")
        lines.extend(["", "=== 完整 API 对话 ==="])
        lines.append(self.conversation_path.read_text(encoding="utf-8") if self.conversation_path.exists() else "")
        return "\n".join(lines)


def main() -> int:
    return FromScratchUltraHarness().run()


if __name__ == "__main__":
    raise SystemExit(main())
