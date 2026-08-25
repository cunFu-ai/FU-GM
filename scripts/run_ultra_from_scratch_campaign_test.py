from __future__ import annotations

import json
import os
import argparse
import hashlib
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
from fu_gm.components.campaign_state_transaction import CampaignStateTransaction  # noqa: E402
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
from fu_gm.testing.legal_actions import LegalActionLayer  # noqa: E402
from fu_gm.testing.luna_player_agent import LunaPlayerAgent  # noqa: E402
from fu_gm.testing.codex_subagent_spool import CodexSubagentSpoolClient  # noqa: E402
from fu_gm.llm_client_bundle import TestLLMClientBundle  # noqa: E402
from fu_gm.testing.player_simulator import ConstrainedPlayerSimulator  # noqa: E402
from fu_gm.testing.replay_models import ReplayScenario, ReplayStep  # noqa: E402


class FromScratchUltraHarness:
    """Runs a hybrid long integration test.

    Session 0 table speech uses the public message boundary, but chapter setup,
    conflict fixtures and late rule probes still contain direct component calls.
    Reports must therefore never present this harness as production E2E proof.
    """

    def __init__(
        self,
        *,
        codex_spool_root: Path | None = None,
    ) -> None:
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

        self._configure_test_environment(self.map_root)
        if codex_spool_root is not None:
            self._disable_external_api_credentials_for_spool()
        self.test_llm_bundle = self._build_test_llm_bundle(codex_spool_root)

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
        self.service = FUGMHttpService(
            data_root=self.campaign_root,
            use_llm=True,
            test_llm_bundle=self.test_llm_bundle,
        )
        self.calls: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.errors: list[str] = []
        self.tool_events: list[dict[str, Any]] = []
        self.player_simulation_metrics: list[dict[str, Any]] = []
        self.player_legal_actions = LegalActionLayer()
        self.player_simulator = self._build_player_simulator()
        self._auto_followup_depth = 0
        self._rule_followup_depth = 0
        self.expected_rules_blocked_labels = {
            "第一章冲突与规则 14 白河",
        }
        self.test_fidelity = {
            "classification": "hybrid_component_integration",
            "production_e2e_verified": False,
            "direct_component_paths": [
                "session_gate",
                "chapter_package_registration",
                "conflict_fixture_injection",
                "core_rule_component_probes",
            ],
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

    @staticmethod
    def _configure_test_environment(map_root: Path) -> None:
        os.environ["FU_GM_PROJECT_DIR"] = str(PROJECT_ROOT)
        os.environ["FU_GM_NORTANTIS_OUTPUT_DIR"] = str(map_root)
        os.environ.setdefault("FU_GM_NORTANTIS_TIMEOUT_SECONDS", "240")
        # 长测宁可等待上游恢复，也不能让一次短暂超时打开熔断器后，
        # 把后续尚未提交的玩家发言都误记成成功调用。
        os.environ.setdefault("FU_GM_TIMEOUT_SECONDS", "240")
        os.environ.setdefault("FU_GM_ENDPOINT_ATTEMPT_TIMEOUT_SECONDS", "60")
        os.environ.setdefault("FU_GM_CORE_GM_TIMEOUT_SECONDS", "300")
        os.environ.setdefault("FU_GM_CORE_GM_ENDPOINT_ATTEMPT_TIMEOUT_SECONDS", "45")
        os.environ.setdefault("FU_GM_CORE_GM_RECOVERY_MAX_RETRIES", "4")
        os.environ.setdefault("FU_GM_CORE_GM_CIRCUIT_BREAKER_ENABLED", "0")

    def _disable_external_api_credentials_for_spool(self) -> None:
        """保证子智能体长测没有退回外部模型端点的可能。"""

        os.environ["FU_GM_DOTENV_PATH"] = str(
            self.run_root / ".codex-spool-do-not-load-dotenv"
        )
        os.environ["FU_GM_DISABLE_EXTERNAL_LLM_TRANSPORT"] = "1"
        for name in tuple(os.environ):
            if name in {
                "OPENAI_API_KEY",
                "DEEPSEEK_API_KEY",
                "ANTHROPIC_API_KEY",
            }:
                os.environ.pop(name, None)
                continue
            if name.startswith("FU_GM_") and name.endswith("_API_KEY"):
                os.environ.pop(name, None)

    @classmethod
    def from_run_root(
        cls,
        run_root: Path,
        *,
        codex_spool_root: Path | None = None,
    ) -> "FromScratchUltraHarness":
        """从已持久化的长测目录恢复，不重放已提交的桌面消息。"""

        root = Path(run_root).expanduser().resolve()
        progress_path = root / "progress.jsonl"
        if not progress_path.exists():
            raise FileNotFoundError(f"长测目录缺少 progress.jsonl：{root}")
        calls = [
            json.loads(line)
            for line in progress_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not calls:
            raise ValueError(f"长测目录没有可恢复调用：{root}")
        campaign_id = str(
            (calls[0].get("body") or {}).get("campaign_id") or ""
        ).strip()
        if not campaign_id:
            raise ValueError("无法从首个调用恢复 campaign_id。")

        self = object.__new__(cls)
        self.stamp = root.name.replace("ultra_from_scratch_", "", 1)
        self.run_root = root
        self.campaign_root = root / "campaigns"
        self.map_root = root / "maps"
        self.progress_path = progress_path
        self.conversation_path = root / "full_api_conversation.txt"
        self.conversation_export_path = root / "完整对话记录.txt"
        self.report_json_path = root / "ultra_from_scratch_report.json"
        self.report_txt_path = root / "ultra_from_scratch_report.txt"
        self._configure_test_environment(self.map_root)
        if codex_spool_root is not None:
            self._disable_external_api_credentials_for_spool()
        self.test_llm_bundle = self._build_test_llm_bundle(codex_spool_root)
        self.campaign_id = campaign_id
        self.session_id = "session0-to-chapter1-from-scratch"
        self.channel_id = "codex-ultra-from-scratch"
        self.participants = ["阿凛", "南星", "白河", "时雨", "澄砚"]
        self.pc_names = ["伊莉雅", "赛璃", "洛岚", "艾薇娅", "苍祈"]
        self.common = {
            "campaign_id": self.campaign_id,
            "session_id": self.session_id,
            "channel_id": self.channel_id,
        }
        self.service = FUGMHttpService(
            data_root=self.campaign_root,
            use_llm=True,
            test_llm_bundle=self.test_llm_bundle,
        )
        self.calls = calls
        previous_report: dict[str, Any] = {}
        if self.report_json_path.exists():
            previous_report = json.loads(
                self.report_json_path.read_text(encoding="utf-8")
            )
        self.notes = list(previous_report.get("notes") or [])
        self.errors = []
        self.tool_events = list(previous_report.get("tool_events") or [])
        self.player_simulation_metrics = list(
            previous_report.get("player_simulation_metrics") or []
        )
        self.player_legal_actions = LegalActionLayer()
        self.player_simulator = self._build_player_simulator()
        self._auto_followup_depth = 0
        self._rule_followup_depth = 0
        self.expected_rules_blocked_labels = {
            "第一章冲突与规则 14 白河",
        }
        self.test_fidelity = {
            "classification": "hybrid_component_integration",
            "production_e2e_verified": False,
            "direct_component_paths": [
                "session_gate",
                "chapter_package_registration",
                "conflict_fixture_injection",
                "core_rule_component_probes",
            ],
        }
        app = self._runtime().app
        self.pre_gate_world_ready = app.session_zero_manager.world_creation_ready()
        self.pre_gate_hero_status = app.session_zero_manager.hero_creation_status()
        self.pre_gate_snapshot = {}
        self.gate_body = {
            "blocked": False,
            "world_map": app.world_map_generation_status(),
        }
        return self

    @staticmethod
    def _build_test_llm_bundle(
        spool_root: Path | None,
    ) -> TestLLMClientBundle | None:
        if spool_root is None:
            return None
        os.environ["FU_GM_WORLD_MAP_RENDERER"] = "nortantis"
        client = CodexSubagentSpoolClient(
            spool_root,
            timeout_seconds=1800.0,
            poll_interval_seconds=0.2,
            test_only=True,
        )
        return TestLLMClientBundle.shared(client, model="gpt-5.6-terra")

    def _build_player_simulator(self) -> LunaPlayerAgent:
        """让玩家模拟与当前长测使用同一能力档位，避免低阶模型污染结论。"""

        player_model = (
            str(getattr(self.test_llm_bundle, "model", "") or "").strip()
            if self.test_llm_bundle is not None
            else str(
                os.environ.get("FU_GM_REPLAY_PLAYER_MODEL")
                or os.environ.get("FU_GM_ACTION_MODEL")
                or "gpt-5.6-terra"
            ).strip()
        )
        return LunaPlayerAgent(
            use_llm=True,
            client=(
                self.test_llm_bundle.player
                if self.test_llm_bundle is not None
                else None
            ),
            model=player_model,
            continue_on_invalid=True,
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

    def run_resume_after_arrival(self) -> int:
        """从财团抵达前后任一安全检查点继续第一章冲突。"""

        try:
            app = self._runtime().app
            # 团队先攻可能已完成所有玩家掷骰，只剩由GM处理的机会窗口。
            # 必须先恢复这个权威事务，不能因为 conflict.active 仍为 False
            # 就重复提交 start_conflict。
            self._resume_blocking_decision_if_needed()
            if not app.conflict_manager.state.active:
                # A failed arrival beat is itself a safe checkpoint. Session 0
                # and the social scene are durable, while the uncommitted beat
                # can be retried after repairing the runtime.
                if not self._prepare_conflict_state():
                    raise RuntimeError("恢复后仍未能通过公开GM流程启动冲突。")
            self._advance_enemy_turns_until_player("恢复后冲突开场敌方回合")
            existing_labels = {
                str(call.get("label") or "")
                for call in self.calls
                if call.get("ok") is True
            }
            if "冲突自由讨论静默 01" not in existing_labels:
                self.route_table_message(
                    "冲突自由讨论静默 01",
                    "时雨",
                    "我们要不要先开旧路，不然被包围就麻烦了？",
                    expected_target="silent",
                    expected_send_reply=False,
                )
            if "冲突自由讨论静默 02" not in existing_labels:
                self.route_table_message(
                    "冲突自由讨论静默 02",
                    "澄砚",
                    "我有点担心先开旧路会不会让守望会背锅，你们怎么看？",
                    expected_target="silent",
                    expected_send_reply=False,
                )
            self._finish_chapter_one_conflict()
            self._exercise_villain_conflict_tools()
            self.invoke(
                "第一章收团",
                "POST",
                "/v1/session/end",
                {**self.common, "title": "第一章：白花碑驿站的迟响"},
            )
            self._exercise_core_design_tools()
            self.audit = self.invoke(
                "读取审计仪表盘",
                "GET",
                self._audit_route(limit=320),
            )
            self._write_public_transcript_copy()
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

    def run_resume_after_session_zero(self) -> int:
        """从已持久化的第零章检查点继续，不重放成功的玩家发言。"""

        try:
            completed_labels = self._completed_labels()
            self._resume_missing_session_zero_character_turns(completed_labels)

            self._record_session_zero_completion_evidence()
            self._verify_no_direct_pc_injection()
            self._wait_for_async_map_if_any()
            self._enter_adventure_after_session_zero()
            if self.gate_body.get("blocked"):
                self.errors.append("冒险门控仍被阻挡。")
            else:
                self._run_chapter_one_from_opening(completed_labels)

            self._write_public_transcript_copy()
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

    def _completed_labels(self) -> set[str]:
        """返回已成功提交的长测步骤标签。"""

        return {
            str(call.get("label") or "")
            for call in self.calls
            if call.get("ok") is True
        }

    def _resume_missing_session_zero_character_turns(
        self,
        completed_labels: set[str],
    ) -> None:
        """只提交检查点之后尚未成功的角色创建发言。"""

        for index, (speaker, message) in enumerate(
            self._session_zero_character_turns(),
            start=1,
        ):
            label = f"第零章角色创建 {index:02d} {speaker}"
            if label in completed_labels:
                continue
            body = self.route_session_zero_message(label, speaker, message)
            if str(body.get("route") or "") == "deduplicated_incomplete":
                raise RuntimeError(
                    f"检查点中的步骤【{label}】存在未完成去重记录；"
                    "长测已停止，必须先核对权威草稿再决定是否以新消息重试。"
                )

    def _completed_combat_indices(self) -> set[int]:
        """只把已处理玩家原始意图的冲突调用视为完成。

        抢跑消息可能先触发当前 NPC 的回合。若该次调用只有
        ``run_current_npc_turn``，玩家行动既未执行也未进入回合外收件箱，
        即使 HTTP 请求成功也不能跳过该测试步骤。
        """

        completed: set[int] = set()
        non_committing_messages = {
            message
            for _speaker, message in self._chapter_one_combat_turns()
            if "暂时收住话头，等自己的回合" in message
        }
        for call in self.calls:
            if call.get("ok") is not True:
                continue
            match = re.match(
                r"第一章冲突与规则\s+(\d+)",
                str(call.get("label") or ""),
            )
            body = call.get("body") if isinstance(call.get("body"), dict) else {}
            receipts = [
                receipt
                for receipt in list(body.get("tool_receipts") or [])
                if isinstance(receipt, dict)
                and str(receipt.get("tool_name") or "") != "discover_capabilities"
            ]
            for receipt in receipts:
                result = receipt.get("result") if isinstance(receipt.get("result"), dict) else {}
                if result.get("rolled_back"):
                    continue
            if not match:
                continue
            if receipts and all(
                str(receipt.get("tool_name") or "") == "run_current_npc_turn"
                for receipt in receipts
            ):
                if str(call.get("message") or "") in non_committing_messages:
                    completed.add(int(match.group(1)))
                continue
            completed.add(int(match.group(1)))
        return completed

    def _resume_blocking_decision_if_needed(self) -> None:
        """恢复存档时，先让真正的窗口所有者完成阻塞选择。"""

        app = self._runtime().app
        speaker_by_hero = dict(zip(self.pc_names, self.participants))
        for _ in range(32):
            pending = [
                window
                for window in app.interceptor.decision_window_manager.pending()
                if bool(getattr(window, "blocking", False))
            ]
            if not pending:
                return
            window = pending[0]
            owner = str(getattr(window, "owner", "") or "").strip()
            kind = str(getattr(window, "kind", "") or "").strip()
            window_id = str(getattr(window, "window_id", "") or "")
            if owner == "__gm__" and kind in {
                "critical_opportunity",
                "fumble_opportunity",
            }:
                self.invoke(
                    "恢复后处理GM待决机会",
                    "POST",
                    "/v1/game/gm-beat",
                    {
                        **self.common,
                        "speaker": "系统节拍",
                        "message": (
                            f"处理当前GM拥有的【{kind}】窗口"
                            f"（window_id={window_id}）：结合当前局面选择一个合法机会效果，"
                            "通过 resolve_gm_opportunity 提交并关闭窗口；"
                            "不要替任何玩家角色行动，也不要推进额外回合。"
                        ),
                    },
                )
                if any(
                    str(getattr(item, "window_id", "") or "") == window_id
                    for item in app.interceptor.decision_window_manager.pending()
                ):
                    raise RuntimeError(f"恢复后未能关闭GM待决窗口【{kind}】。")
                continue

            speaker = speaker_by_hero.get(owner)
            if not speaker:
                raise RuntimeError(f"阻塞窗口【{kind}】没有可用玩家。")
            message = self._compose_decision_window_response(
                window=window,
                speaker=speaker,
                owner=owner,
            )
            self.invoke(
                f"恢复后处理待决窗口 {speaker}",
                "POST",
                "/v1/game/turn",
                {**self.common, "speaker": speaker, "message": message},
            )
            if any(
                str(getattr(item, "window_id", "") or "") == window_id
                for item in app.interceptor.decision_window_manager.pending()
            ):
                raise RuntimeError(f"恢复后未能关闭【{owner}】的待决窗口【{kind}】。")
        raise RuntimeError("恢复阻塞窗口超过32次，疑似出现了循环待决状态。")

    def _compose_decision_window_response(
        self,
        *,
        window: Any,
        speaker: str,
        owner: str,
    ) -> str:
        """让窗口所有者根据公开合法选项回答；模型失效时使用类型化回退。"""

        kind = str(getattr(window, "kind", "") or "").strip()
        if not all(
            hasattr(self, name)
            for name in (
                "campaign_id",
                "session_id",
                "channel_id",
                "player_legal_actions",
                "player_simulator",
            )
        ):
            return self._minimal_decision_window_fallback(kind, owner)
        step = ReplayStep(
            id=f"ultra-decision-{getattr(window, 'window_id', '')}",
            kind="player_message",
            speaker=speaker,
            actor=owner,
            stage_goal=(
                "主持人正在等待这个角色处理一个明确的规则选择。"
                "只回答当前窗口，使用公开列出的合法选项并补齐所需目标或参数；"
                "不要声明新的场景行动，也不要替主持人描述结算结果。"
            ),
        )
        scenario = ReplayScenario(
            name="第一章待决窗口",
            campaign_id=self.campaign_id,
            session_id=self.session_id,
            channel_id=self.channel_id,
            participants=list(self.participants),
            steps=[step],
        )
        recent_public_context = self._recent_public_dialogue(limit=10)
        legal_context = self.player_legal_actions.build(
            self.service,
            scenario,
            step,
            public_context=recent_public_context,
        )
        public_window = next(
            (
                item
                for item in legal_context.pending_decisions
                if str(item.get("window_id") or "")
                == str(getattr(window, "window_id", "") or "")
            ),
            legal_context.pending_decisions[0]
            if legal_context.pending_decisions
            else {},
        )
        fallback = ConstrainedPlayerSimulator._decision_window_fallback(
            public_window,
            legal_context,
        )
        utterance = self.player_simulator.compose(
            step=step,
            legal_context=legal_context,
            last_gm_reply=next(
                (
                    str(call.get("reply") or "")
                    for call in reversed(self.calls)
                    if str(call.get("reply") or "").strip()
                ),
                "",
            ),
            recent_public_context=recent_public_context,
        )
        message = str(utterance.text or "").strip()
        if (
            not message
            or utterance.used_fallback
            or not ConstrainedPlayerSimulator._answers_pending_decision(
                message,
                public_window,
            )
        ):
            message = fallback
        self.player_simulation_metrics.append(
            {
                "kind": "decision_window",
                "window_kind": kind,
                "window_id": str(getattr(window, "window_id", "") or ""),
                "speaker": speaker,
                "actor": owner,
                "current_actor": str(legal_context.current_actor or owner),
                "model": str(getattr(self.player_simulator, "model", "") or ""),
                "used_fallback": bool(utterance.used_fallback or message == fallback),
                "validation_errors": list(utterance.validation_errors or []),
                "model_attempts": list(utterance.model_attempts or []),
                "text": message,
            }
        )
        return message

    @staticmethod
    def _minimal_decision_window_fallback(kind: str, owner: str) -> str:
        """供离线夹具和灾难恢复使用，不猜测需要具体参数的窗口。"""

        if kind == "zero_hp":
            return f"{owner}选择放弃抵抗，不作牺牲，并接受当前局势带来的后果。"
        if kind in {"critical_opportunity", "fumble_opportunity"}:
            return f"{owner}把这次机会用于【优势】，让自己的下一次检定获得+4。"
        if kind in {"trait_invocation", "bond_invocation"}:
            return f"{owner}不援用特质或羁绊，接受当前检定结果。"
        raise RuntimeError(f"待决窗口【{kind}】需要读取公开合法选项，不能盲目回退。")

    def _advance_enemy_turns_until_player(self, label_prefix: str) -> None:
        """敌方回合由GM主动完成，不能借玩家闲聊充当触发器。"""

        app = self._runtime().app
        for index in range(1, 9):
            if not app.conflict_manager.state.active:
                return
            actor = str(app.conflict_manager.state.current_actor() or "").strip()
            if not actor or not app.character_manager.exists(actor):
                return
            traits = set(app.character_manager.get(actor).traits)
            if not {"enemy", "villain"}.intersection(traits):
                return
            self.invoke(
                f"{label_prefix} {index:02d} {actor}",
                "POST",
                "/v1/game/gm-beat",
                {
                    **self.common,
                    "speaker": "系统节拍",
                    "message": f"让当前敌方【{actor}】按其目标与战斗档案完成一个合法回合。",
                },
            )
        raise RuntimeError("敌方连续行动超过合法上限，无法把回合交给玩家。")

    def _resume_current_held_action_if_needed(self) -> None:
        app = self._runtime().app
        actor = str(app.conflict_manager.state.current_actor() or "").strip()
        if not actor:
            return
        held = list(app.conflict_manager.held_actions_for_actor(actor))
        if not held:
            return
        speaker_by_hero = dict(zip(self.pc_names, self.participants))
        speaker = speaker_by_hero.get(actor)
        if not speaker:
            return
        response = self.invoke(
            f"恢复后确认缓存行动 {speaker}",
            "POST",
            "/v1/game/turn",
            {
                **self.common,
                "speaker": speaker,
                "message": f"{actor}确认按刚才缓存的行动执行。",
            },
        )
        if not bool(response.get("ok", True)) or app.conflict_manager.held_actions_for_actor(actor):
            raise RuntimeError(f"恢复后未能执行【{actor}】的缓存行动。")

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
        """仅重试明确未提交的上游故障，避免重复执行状态写入。"""

        if method.upper() != "POST":
            return None
        receipts = [
            dict(item)
            for item in list(body.get("tool_receipts") or [])
            if isinstance(item, dict)
        ]
        if any(
            bool(receipt.get("ok")) and bool(receipt.get("state_changed"))
            for receipt in receipts
        ):
            return None
        route_name = str(body.get("route") or "").strip()
        fully_rolled_back = (
            route_name
            == "gm_agent_message_transaction_rolled_back"
            and any(
                bool((receipt.get("result") or {}).get("rolled_back"))
                for receipt in receipts
                if isinstance(receipt.get("result"), dict)
            )
        )
        category = str(body.get("provider_error_category") or "").strip()
        error = str(body.get("agent_error") or body.get("error") or "")
        timeout_markers = (
            "timeout",
            "timed out",
            "wall-clock budget",
            "deadline",
            "超时",
            "截止时间",
            "共享截止",
        )
        unknown_but_transient = category == "unknown" and any(
            marker in error.lower() for marker in timeout_markers
        )
        retry_safe = body.get("retry_safe") is True
        unavailable = route_name == "gm_agent_unavailable"
        explicit_provider_failure = (
            retry_safe
            and unavailable
            and (
                category
                in {
                    "transport",
                    "circuit_open",
                    "rate_limit",
                    "server",
                }
                or unknown_but_transient
            )
        )
        rolled_back_provider_failure = (
            fully_rolled_back and self._core_gm_has_transient_provider_failure()
        )
        if not explicit_provider_failure and not rolled_back_provider_failure:
            return None
        retry_limit = max(
            0,
            int(os.environ.get("FU_GM_LONG_TEST_PROVIDER_RETRY_LIMIT", "8")),
        )
        if attempt > retry_limit:
            return None
        match = re.search(
            r"retry\s+after\s+([0-9]+(?:\.[0-9]+)?)s",
            error,
            flags=re.IGNORECASE,
        )
        circuit_wait = float(match.group(1)) + 1.0 if match else 0.0
        base_delay = max(
            0.0,
            float(
                os.environ.get(
                    "FU_GM_LONG_TEST_PROVIDER_RETRY_BASE_SECONDS",
                    "10",
                )
            ),
        )
        maximum_delay = max(
            base_delay,
            float(
                os.environ.get(
                    "FU_GM_LONG_TEST_PROVIDER_RETRY_MAX_SECONDS",
                    "35",
                )
            ),
        )
        backoff = min(maximum_delay, base_delay * (1.5 ** max(0, attempt - 1)))
        return max(backoff, circuit_wait)

    def _core_gm_has_transient_provider_failure(self) -> bool:
        """读取本轮核心模型 telemetry，不从面向玩家的回复猜测故障。"""

        component = getattr(getattr(self, "service", None), "gm_tool_agent", None)
        if component is None:
            return False
        evidence = [str(getattr(component, "last_error", "") or "")]
        client = getattr(component, "client", None)
        evidence.extend(
            str(getattr(item, "reason", "") or "")
            for item in list(getattr(client, "last_recovery_attempts", []) or [])
        )
        text = "\n".join(evidence).lower()
        transient_markers = (
            "llm http 429",
            "llm http 500",
            "llm http 502",
            "llm http 503",
            "llm http 504",
            "bad gateway",
            "gateway timeout",
            "网站请求超时",
            "timed out",
            "timeout",
            "connection reset",
            "connection refused",
            "remote end closed connection",
            "rate limit",
            "too many requests",
        )
        return any(marker in text for marker in transient_markers)

    def invoke(self, label: str, method: str, route: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = self._attach_test_message_identity(
            label,
            method,
            route,
            payload or {},
        )
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
        agent_route = str(body.get("route") or "")
        failed_agent_route = agent_route.startswith(
            (
                "gm_agent_unavailable",
                "gm_agent_unresolved",
                "deduplicated_incomplete",
            )
        )
        record = {
            "index": len(self.calls) + 1,
            "label": label,
            "method": method,
            "route": route,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "ok": bool(body.get("ok", status < 400)) and not failed_agent_route,
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
        if agent_route.startswith("gm_agent_unavailable") and str(
            body.get("provider_error_category") or ""
        ) in {"authentication", "account_inactive", "forbidden"}:
            raise RuntimeError(
                "在线主持模型账号不可用，长测已在首个永久供应商错误处停止。"
            )
        if getattr(self, "_rule_followup_depth", 0) == 0:
            self._simulate_platform_rule_followups(record)
        if self._auto_followup_depth < 3:
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

    def _attach_test_message_identity(
        self,
        label: str,
        method: str,
        route: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Mirror AstrBot's stable per-message idempotency key in long tests."""

        result = dict(payload)
        message_routes = {
            "/v1/message/route",
            "/v1/session-zero/message",
            "/v1/game/turn",
        }
        if (
            method.upper() != "POST"
            or route not in message_routes
            or bool(result.get("is_private"))
            or not str(result.get("channel_id") or "").strip()
        ):
            return result
        sequence = len(getattr(self, "calls", [])) + 1
        identity_source = "\x1f".join(
            (
                str(result.get("campaign_id") or ""),
                str(result.get("session_id") or ""),
                str(result.get("channel_id") or ""),
                str(sequence),
                str(label or ""),
                str(result.get("speaker") or ""),
                str(result.get("message") or ""),
            )
        )
        digest = hashlib.sha256(identity_source.encode("utf-8")).hexdigest()[:24]
        result["message_id"] = f"longrun-{sequence:05d}-{digest}"
        result.pop("activity_token", None)
        return result

    def _simulate_platform_rule_followups(self, record: dict[str, Any]) -> None:
        """执行 AstrBot 在真实群聊中负责的延迟规则回执。"""

        pending = [
            dict(item)
            for item in list((record.get("body") or {}).get("scheduled_rule_followups") or [])
            if isinstance(item, dict)
            and str(item.get("kind") or "") == "failed_check_grace"
        ]
        processed = 0
        while pending and processed < 10:
            item = pending[0]
            try:
                delay_seconds = max(0.0, float(item.get("delay_seconds") or 0.0))
            except (TypeError, ValueError):
                delay_seconds = 15.0
            if delay_seconds > 0:
                time.sleep(delay_seconds + 0.1)
            heartbeat_payload = {
                "campaign_id": str(item.get("campaign_id") or self.campaign_id),
                "session_id": str(item.get("session_id") or self.session_id),
                "channel_id": str(item.get("channel_id") or self.channel_id),
                "speaker": "系统规则计时",
                "message": "",
                "mode": "failed_check_grace",
                "auto_respond": True,
                "defer_delivery_log": True,
                "rule_followup_kind": "failed_check_grace",
                "rule_followup_window_id": str(item.get("window_id") or ""),
                "rule_followup_token": str(item.get("token") or ""),
            }
            self._rule_followup_depth += 1
            try:
                heartbeat = self.invoke(
                    "AstrBot延迟结算失败检定",
                    "POST",
                    "/v1/session/heartbeat",
                    heartbeat_payload,
                )
                delivery_id = str(heartbeat.get("delivery_id") or "").strip()
                if delivery_id:
                    self.invoke(
                        "AstrBot确认规则消息送达",
                        "POST",
                        "/v1/session/heartbeat/delivered",
                        {
                            "campaign_id": heartbeat_payload["campaign_id"],
                            "session_id": heartbeat_payload["session_id"],
                            "channel_id": heartbeat_payload["channel_id"],
                            "delivery_id": delivery_id,
                        },
                    )
            finally:
                self._rule_followup_depth -= 1
            pending = [
                dict(next_item)
                for next_item in list(heartbeat.get("scheduled_rule_followups") or [])
                if isinstance(next_item, dict)
                and str(next_item.get("kind") or "") == "failed_check_grace"
            ]
            processed += 1

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

    def route_session_zero_message(
        self,
        label: str,
        speaker: str,
        message: str,
        *,
        directed_at_gm: bool = False,
    ) -> dict[str, Any]:
        """让第零章自然发言经过真实群聊路由，而非内部管理接口。

        第零章贡献可能在后台静默落盘，也可能因为确认共识、安全边界或
        玩家提问而公开回复。这里不预先规定 ``send_reply``，只验证消息
        没有被错误转交给其他机器人；最终是否应答由语义路由和工具回执
        共同决定。
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
        if target not in {"fu_gm", "silent"}:
            self.errors.append(
                f"{label} 第零章群消息被错误路由到 {target!r}。"
            )
        send_reply = bool(body.get("send_reply"))
        self._record_tool_event(
            "AstrBot/QQ 路由器",
            label,
            (
                "第零章群消息由 /v1/message/route 进入语义路由，"
                f"target={target!r}, send_reply={send_reply}"
            ),
            {
                "directed_at_gm": bool(directed_at_gm),
                "state_changed": any(
                    bool(item.get("ok")) and bool(item.get("state_changed"))
                    for item in list(body.get("tool_receipts") or [])
                    if isinstance(item, dict)
                ),
            },
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
        pending_target, pending_kind = self._pending_window_followup_target(
            record,
            hero_by_speaker=hero_by_speaker,
            speaker_by_hero=speaker_by_hero,
        )
        target_speaker = pending_target or self._followup_target_speaker(
            reply,
            speaker,
            hero_by_speaker,
            speaker_by_hero,
        )
        if target_speaker not in hero_by_speaker:
            return None
        hero, theme, drive = hero_by_speaker[target_speaker]
        if record["route"] == "/v1/game/turn":
            if any(
                marker in reply
                for marker in (
                    "要投吗",
                    "确认投骰",
                    "选择投骰、取消",
                    "选择：投骰、取消",
                    "请选择投骰、取消",
                )
            ):
                return target_speaker, f"{hero}确认投骰。"
            if pending_kind in {"critical_opportunity", "fumble_opportunity"}:
                opportunity_label = "大成功" if pending_kind == "critical_opportunity" else "对手大失败"
                return (
                    target_speaker,
                    f"{hero}把这次{opportunity_label}带来的机会用于【优势】，"
                    f"让{hero}自己的下一次检定获得+4。",
                )
            if pending_kind == "zero_hp":
                return (
                    target_speaker,
                    f"{hero}选择放弃抵抗，不作牺牲，并接受当前局势带来的后果。",
                )
            if (
                "大成功" in reply
                and "机会" in reply
                and any(token in reply for token in ("揭示", "进展", "纽带", "优势", "转折"))
            ):
                return (
                    target_speaker,
                    f"{hero}把这次大成功的机会用于【揭示】，目标是白花守望会会长；"
                    "想知道她此刻真正的目标或动机。",
                )
            if "要不要花 1 点物语点" in reply or "要不要花1点物语点" in reply:
                return target_speaker, f"{hero}暂不消耗物语点，接受这次失败。"
            cached_turn = re.search(
                r"轮到【(?P<actor>[^】]+)】了；刚才缓存的是",
                reply,
            )
            if cached_turn:
                cached_actor = cached_turn.group("actor").strip()
                cached_speaker = speaker_by_hero.get(cached_actor)
                if cached_speaker in hero_by_speaker:
                    return (
                        cached_speaker,
                        f"{cached_actor}确认按刚才缓存的行动执行。",
                    )
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

    @staticmethod
    def _pending_window_followup_target(
        record: dict[str, Any],
        *,
        hero_by_speaker: dict[str, tuple[str, str, str]],
        speaker_by_hero: dict[str, str],
    ) -> tuple[str, str]:
        """从权威工具回执确定待决窗口的实际回应玩家。"""

        receipts = list((record.get("body") or {}).get("tool_receipts") or [])
        current_windows: list[dict[str, Any]] | None = None
        for receipt in receipts:
            if not isinstance(receipt, dict) or receipt.get("ok") is not True:
                continue
            result = receipt.get("result") if isinstance(receipt.get("result"), dict) else {}
            windows: list[dict[str, Any]] = []
            carries_window_state = False
            for key in ("pending_decisions", "pending_windows"):
                if key not in result:
                    continue
                carries_window_state = True
                raw_windows = result.get(key)
                if isinstance(raw_windows, list):
                    windows.extend(item for item in raw_windows if isinstance(item, dict))
            if carries_window_state:
                # Later rule receipts replace earlier snapshots.  In
                # particular, resolve_rule_window(pending_decisions=[]) closes
                # the opportunity and must not let the harness answer the stale
                # window from an earlier get_gameplay_state receipt again.
                current_windows = windows
        for window in current_windows or []:
            status = str(window.get("status") or "open").strip().lower()
            if status not in {"", "open", "pending", "awaiting_player"}:
                continue
            kind = str(window.get("kind") or "").strip()
            identities: list[str] = []
            for key in ("allowed_responders", "allowed_speakers", "owner"):
                raw_value = window.get(key)
                values = raw_value if isinstance(raw_value, list) else [raw_value]
                identities.extend(
                    str(value or "").strip()
                    for value in values
                    if str(value or "").strip()
                )
            for identity in identities:
                if identity in hero_by_speaker:
                    return identity, kind
                owner = speaker_by_hero.get(identity)
                if owner:
                    return owner, kind
        return "", ""

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
            r"轮到【(?P<actor>[^】]+)】了",
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
            expected_target="silent",
            expected_send_reply=False,
        )

        for index, (speaker, message) in enumerate(self._session_zero_world_turns(), start=1):
            self.route_session_zero_message(
                f"第零章世界共创 {index:02d} {speaker}",
                speaker,
                message,
            )

        for index, (speaker, message) in enumerate(self._session_zero_completion_turns(), start=1):
            self.route_session_zero_message(
                f"第零章流程补齐 {index:02d} {speaker}",
                speaker,
                message,
            )

        for index, (speaker, message) in enumerate(self._session_zero_character_turns(), start=1):
            self.route_session_zero_message(
                f"第零章角色创建 {index:02d} {speaker}",
                speaker,
                message,
            )
        self._record_session_zero_completion_evidence()

        self._verify_no_direct_pc_injection()
        self._wait_for_async_map_if_any()

        self._enter_adventure_after_session_zero()
        if self.gate_body.get("blocked"):
            self.errors.append("冒险门控仍被阻挡。")
            return
        self._run_chapter_one_from_opening(set())
        self._write_public_transcript_copy()

    def _record_session_zero_completion_evidence(self) -> None:
        app = self._runtime().app
        self._record_tool_event(
            "第零章/角色创建管理",
            "第零章完成后",
            "SessionZeroManager 从玩家自然语言中抽取世界共识、小队原型、安全边界和五名 PC 角色草稿。",
            {
                "world_ready": app.session_zero_manager.world_creation_ready(),
                "hero_creation_status": app.session_zero_manager.hero_creation_status(),
                "world_profile": {
                    "continent_name": app.world_state.world_profile.continent_name,
                    "starting_region": app.world_state.world_profile.starting_region,
                    "group_concept": app.world_state.world_profile.group_concept,
                    "villain_seeds": list(app.world_state.world_profile.villain_seeds),
                    "safety_lines": list(app.world_state.world_profile.safety_lines),
                    "safety_veils": list(app.world_state.world_profile.safety_veils),
                },
            },
            public=True,
        )

    def _enter_adventure_after_session_zero(self) -> None:
        app = self._runtime().app
        self.pre_gate_snapshot = self._snapshot(include_private=True)
        self.pre_gate_hero_status = app.session_zero_manager.hero_creation_status()
        self.pre_gate_world_ready = app.session_zero_manager.world_creation_ready()
        if not self.pre_gate_hero_status.get("ready"):
            self.notes.append(
                f"冒险门控前角色仍未 ready：{self.pre_gate_hero_status}"
            )

        gate = self.service.session_gates.get(
            self.campaign_id,
            self.channel_id,
            self.session_id,
        )
        if str(gate.status or "") == "adventure":
            current_scene = app.scene_manager.current_scene
            if current_scene is None or current_scene.scene_type == SceneType.SESSION_ZERO:
                opening_body = self.route_table_message(
                    "第一章 GM 开场",
                    "阿凛",
                    "大家已经讨论完，也都同意现在开始第一章。时悠，请开场。",
                    expected_target="fu_gm",
                    expected_send_reply=True,
                    directed_at_gm=True,
                )
                current_scene = app.scene_manager.current_scene
                if current_scene is None or current_scene.scene_type == SceneType.SESSION_ZERO:
                    self.errors.append("冒险阶段已经开启，但正式start_scene工具没有建立第一章场景。")
            self.gate_body = {
                "ok": True,
                "blocked": current_scene is None or current_scene.scene_type == SceneType.SESSION_ZERO,
                "status": "adventure",
                "world_map": app.world_map_generation_status(),
            }
            return

        opening_body = self.route_table_message(
            "第一章 GM 开场",
            "阿凛",
            "大家已经讨论完，也都同意现在开始第一章。时悠，请开场。",
            expected_target="fu_gm",
            expected_send_reply=True,
            directed_at_gm=True,
        )
        receipts = [
            item
            for item in list(opening_body.get("tool_receipts") or [])
            if isinstance(item, dict)
        ]
        start_session_receipt = next(
            (
                item
                for item in receipts
                if str(item.get("tool_name") or "") == "start_session"
            ),
            None,
        )
        start_scene_receipt = next(
            (
                item
                for item in receipts
                if str(item.get("tool_name") or "") == "start_scene"
                and bool(item.get("ok"))
            ),
            None,
        )
        session_result = (
            dict(start_session_receipt.get("result") or {})
            if isinstance(start_session_receipt, dict)
            else {}
        )
        gate = self.service.session_gates.get(
            self.campaign_id,
            self.channel_id,
            self.session_id,
        )
        blocked = bool(
            start_session_receipt is None
            or not bool(start_session_receipt.get("ok"))
            or str(gate.status or "") != "adventure"
        )
        self.gate_body = {
            "ok": bool(opening_body.get("ok")),
            "blocked": blocked,
            "status": str(gate.status or ""),
            "gate": {
                "status": str(gate.status or ""),
                "reason": str(gate.reason or ""),
            },
            "blockers": dict(session_result.get("blockers") or {}),
            "hero_creation": dict(session_result.get("hero_creation") or {}),
            "world_map": app.world_map_generation_status(),
            "opening_tool_receipts": [
                str(item.get("tool_name") or "") for item in receipts
            ],
        }
        if not blocked and start_scene_receipt is None:
            self.gate_body["blocked"] = True
            self.errors.append(
                "start_session已进入冒险，但同一事务没有完成必需的start_scene开场。"
            )

    def _run_chapter_one_from_opening(self, completed_labels: set[str]) -> None:
        if "第一章收团" in completed_labels:
            self.audit = self.invoke(
                "读取审计仪表盘",
                "GET",
                self._audit_route(limit=320),
            )
            return

        self._record_chapter_opening_usage()
        if "第一章自由讨论静默 01" not in completed_labels:
            self.route_table_message(
                "第一章自由讨论静默 01",
                "南星",
                "你们觉得先问会长还是先看旅人？",
                expected_target="silent",
                expected_send_reply=False,
            )
        if "第一章自由讨论静默 02" not in completed_labels:
            self.route_table_message(
                "第一章自由讨论静默 02",
                "白河",
                "哈哈哈这个驿站好有日式RPG味。",
                expected_target="silent",
                expected_send_reply=False,
            )
        for index, (speaker, message) in enumerate(
            self._chapter_one_turns_before_combat(),
            start=1,
        ):
            label = f"第一章连贯场景 {index:02d} {speaker}"
            if label in completed_labels:
                continue
            self.invoke(
                label,
                "POST",
                "/v1/game/turn",
                {**self.common, "speaker": speaker, "message": message},
            )
        self._record_chapter_scene_tools_after_social_phase()

        app = self._runtime().app
        conflict_has_history = any(
            label.startswith(
                (
                    "第一章冲突",
                    "第一章敌方",
                    "第一章目标完成后收束",
                    "第一章冲突自然收束",
                    "恢复后冲突",
                )
            )
            for label in completed_labels
        )
        if not app.conflict_manager.state.active and not conflict_has_history:
            if not self._prepare_conflict_state():
                raise RuntimeError(
                    "冲突没有通过公开 GM 流程启动，已停止后续战斗测试。"
                )
        if app.conflict_manager.state.active:
            self._advance_enemy_turns_until_player("第一章冲突开场敌方回合")
            if "冲突自由讨论静默 01" not in completed_labels:
                self.route_table_message(
                    "冲突自由讨论静默 01",
                    "时雨",
                    "我们要不要先开旧路，不然被包围就麻烦了？",
                    expected_target="silent",
                    expected_send_reply=False,
                )
            if "冲突自由讨论静默 02" not in completed_labels:
                self.route_table_message(
                    "冲突自由讨论静默 02",
                    "澄砚",
                    "我有点担心先开旧路会不会让守望会背锅，你们怎么看？",
                    expected_target="silent",
                    expected_send_reply=False,
                )
            self._finish_chapter_one_conflict()
            self._exercise_villain_conflict_tools()

        if "第一章收团" not in completed_labels:
            self.invoke(
                "第一章收团",
                "POST",
                "/v1/session/end",
                {**self.common, "title": "第一章：白花碑驿站的迟响"},
            )
        self._exercise_core_design_tools()
        self.audit = self.invoke(
            "读取审计仪表盘",
            "GET",
            self._audit_route(limit=320),
        )

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
                "我希望整体有史诗奇幻的希望感，但别一上来就是拯救世界。先从边境上一件会影响普通人的小事开始，真相到中期再慢慢掀开。",
            ),
            (
                "时雨",
                "先说安全边界。界限：不详细描写性暴力、酷刑和现实仇恨煽动。帷幕：儿童遇险、身体病变和亲密内容都淡出处理。",
            ),
            (
                "白河",
                "我先丢一个还没定的地图想法：大陆叫白钟大陆，西边有山，中央有内海，南边是海岸和驿站，东南是群岛。大家觉得这个轮廓合适吗？",
            ),
            (
                "南星",
                "我赞成白河刚才的轮廓，就按白钟大陆来：西侧叫鸦羽山脉，中央是镜线内海，南岸放雾潮海岸和白花碑驿站，东南是潮鸢群岛。它就是普通的类地球大陆，不用异形世界。",
            ),
            (
                "阿凛",
                "魔法和科技可以并存。灵魂晶炉驱动车辆、工坊和财团机器，古老的御魂术与元素仪式则负责安抚灵魂之河。",
            ),
            (
                "阿凛",
                "我贡献钟鸣公国，放在镜线内海北岸。正午大钟能安抚灵魂，也让贵族控制谁的哀悼能被听见。历史事件是碎月坠落当夜，全大陆的钟都慢了一拍。奥秘是姐姐的名字为何刻在白花风铃内侧，却无人记得她死亡。威胁是辉钢财团正把灰晶病患者的记忆当成可买卖燃料。",
            ),
            (
                "南星",
                "国家这一项我先跳过。我补潮鸢群岛这个地区：飞翼船追着季风迁徙。三十年前碎月坠落，赤羽旧王都一夜消失；我想查的奥秘是每年归潮祭后都会少一座岛，所有人的公开记忆还会跟着改写。苍白司教团则把灰晶病包装成灵魂升格，这是我贡献的威胁。",
            ),
            (
                "白河",
                "国家我也先跳过。我补西北的第七采掘城，它受辉钢财团控制。记忆炉第一次启动时吞掉了一整条矿道工人的姓名；紧急停机协议为何只回应赤羽遗民的歌，是我想追的奥秘。财团正在向雾潮海岸扩张，这是眼下的威胁。监察官艾蕾娜相信集中管理记忆能阻止世界再次遗忘灾难。",
            ),
            (
                "时雨",
                "我的国家是东部海岸的奥涅里亚，灯塔舰队维持贸易，王室却和港口行会互不信任。老国王病倒后，摄政王把王室海图抵押给财团；灯塔为什么能照见已经消失的岛，是我想留下的奥秘。我的威胁贡献是：若港口行会与王室决裂，财团就会拿走失踪群岛调查权。",
            ),
            (
                "澄砚",
                "我贡献东南内陆的沉默森林，以及森林南侧的树誓村社。村社不认王权，只和奥灵立约。碎月之夜后，森林第一次拒绝所有人类祈祷；树皮写下的名字里为何有人仍活着，是这里的奥秘。苍白司教团想把森林变成灰晶病圣地。",
            ),
            (
                "白河",
                "小队我先提个还没定的方向：大家是在白花碑驿站临时结成的守护者，护送失忆旅人和碎月遗物去钟鸣公国。你们觉得合适吗？",
            ),
            (
                "时雨",
                "我希望第一章至少有一场冲突不靠战斗解决，要靠证据、承诺和情感去改变别人的决定。",
            ),
            (
                "澄砚",
                "我赞成白河的小队方向。我们就是在白花碑驿站临时结成的守护者，护送失忆旅人和碎月遗物前往钟鸣公国；如果只抢线索、不保护普通人，奥灵会沉默。",
            ),
            (
                "南星",
                "第一幕我提议从白花碑驿站开始：先争取白花守望会开放旧路，再处理远处正在接近的财团巡逻队。",
            ),
            (
                "阿凛",
                "从白花碑驿站开始我也同意。先看看守望会愿不愿意帮忙，再决定怎么应付巡逻队。",
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
                "洛岚的便携装置选择魔导装置。",
            ],
            "洛岚初始装备：铁锤、旅行装束。羁绊：伊莉雅：钦佩；赛璃：信赖。"
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
        blockers = self.gate_body.get("blockers", {})
        status = self.gate_body.get("hero_creation")
        if not isinstance(status, dict) and isinstance(blockers, dict):
            status = blockers.get("hero_creation")
        if not isinstance(status, dict):
            status = self._runtime().app.session_zero_manager.hero_creation_status()
        missing = status.get("missing_by_player", {}) if isinstance(status, dict) else {}
        if not missing:
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
                "赛璃施放御魂仪式【风铃回声】：学科御魂，效力轻微，范围小范围，使用洞察+意志。"
                "她把旅人的名字写在白花纸上挂到风铃下，想让风铃回放昨夜经过这里的脚步和名字。",
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
                "苍祈走到院中无名碑旁，查看缠在碑上的白花藤枝叶与根部，"
                "想判断昨夜是否有沉重机兵从旁经过；他只调查眼前已经出现的痕迹。",
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
                "伊莉雅尝试带队结束冲突：她护着旅人和碎月遗物撤入旧路，请时悠根据旧路闸门与艾蕾娜记忆集中协议的局势判断能否收束。",
            ),
            (
                "澄砚",
                "下一段我这个玩家会缺席半小时，苍祈选择淡出场景去和沉默森林奥灵交涉，之后再回来。"
                "请按缺席玩家流程记录，不要让他替队伍做关键选择。",
            ),
        ]

    def _finish_chapter_one_conflict(self) -> None:
        """由当前行动者逐回合回应公开局面，直到冲突得到真实结局。"""

        app = self._runtime().app
        speaker_by_hero = dict(zip(self.pc_names, self.participants))
        objective_name = "旧路闸门开启"
        closure_attempts = 0
        player_turns = 0
        for step in range(1, 97):
            self._resume_blocking_decision_if_needed()
            self._resume_current_held_action_if_needed()
            if not app.conflict_manager.state.active:
                break

            resolution_status = app.conflict_manager.resolution_status()
            if bool(resolution_status.get("ready_for_natural_end")):
                self.invoke(
                    f"第一章冲突自然收束 {step:02d}",
                    "POST",
                    "/v1/game/gm-beat",
                    {
                        **self.common,
                        "speaker": "系统节拍",
                        "message": "按当前已经成立的胜负或离场结果结束冲突，不追加新的角色行动。",
                    },
                )
                continue

            actor = str(app.conflict_manager.state.current_actor() or "").strip()
            if not actor:
                raise RuntimeError("冲突仍活动，但回合表没有当前行动者。")
            if app.character_manager.exists(actor) and {
                "enemy",
                "villain",
            }.intersection(app.character_manager.get(actor).traits):
                self.invoke(
                    f"第一章敌方自然回合 {step:02d} {actor}",
                    "POST",
                    "/v1/game/gm-beat",
                    {
                        **self.common,
                        "speaker": "系统节拍",
                        "message": f"让当前敌方【{actor}】按其目标与战斗档案完成一个合法回合。",
                    },
                )
                continue

            speaker = speaker_by_hero.get(actor)
            if not speaker:
                raise RuntimeError(f"当前行动者【{actor}】没有对应的测试玩家。")
            objective_complete = self._clock_is_complete(
                app.clock_manager,
                objective_name,
            )
            if objective_complete:
                closure_attempts += 1
                message = (
                    f"{actor}示意同伴护住旅人，从已经打开的旧路撤离；"
                    "他们不再恋战，只阻止追兵越过门槛。"
                )
                label = f"第一章目标完成后收束 {closure_attempts:02d} {speaker}"
            else:
                player_turns += 1
                message = self._compose_live_combat_action(
                    speaker=speaker,
                    actor=actor,
                    turn_number=player_turns,
                    must_consume_turn=self._current_actor_already_used_free_speech(
                        actor
                    ),
                )
                label = f"第一章实时玩家回合 {player_turns:02d} {speaker}"
            self.invoke(
                label,
                "POST",
                "/v1/game/turn",
                {**self.common, "speaker": speaker, "message": message},
            )
            if closure_attempts >= 4 and app.conflict_manager.state.active:
                raise RuntimeError("目标已经完成，但GM连续四次没有提交冲突收束。")

        if app.conflict_manager.state.active:
            clock_text = ""
            if app.clock_manager.exists(objective_name):
                clock = app.clock_manager.get(objective_name)
                clock_text = f"，{objective_name}={clock.current}/{clock.max_segments}"
            elif app.clock_manager.is_retired(objective_name):
                clock_text = f"，{objective_name}=已完成并归档"
            raise RuntimeError(f"第一章冲突在96个续接步骤后仍未结束{clock_text}。")
        objective_complete = self._clock_is_complete(
            app.clock_manager,
            objective_name,
        )
        surviving_pcs = [
            name
            for name in self.pc_names
            if app.character_manager.exists(name)
            and int(app.character_manager.get(name).hp or 0) > 0
        ]
        if objective_complete:
            outcome_summary = "队伍完成旧路目标，并带着旅人从已经打开的闸门撤离。"
        elif surviving_pcs:
            outcome_summary = (
                "冲突已经结束，但队伍未完成旧路目标；"
                f"仍能行动的英雄为：{'、'.join(surviving_pcs)}。"
            )
        else:
            outcome_summary = "队伍未能完成旧路目标，所有仍在场的英雄均失去战斗能力。"
        app.hero_log_manager.record_chapter_beat(
            "白花碑驿站的迟响",
            title="冲突：旧路闸门与巡逻队",
            beat_type="conflict",
            status="done",
            expected_minutes=45,
            summary=outcome_summary,
        )
        self.notes.append(f"第一章冲突真实结局：{outcome_summary}")

    def _compose_live_combat_action(
        self,
        *,
        speaker: str,
        actor: str,
        turn_number: int,
        must_consume_turn: bool = False,
    ) -> str:
        """只向当前行动者提供公开桌面信息，让 FU-PL 自己决定行动。"""

        step = ReplayStep(
            id=f"ultra-ch1-conflict-{turn_number:02d}",
            kind="player_message",
            speaker=speaker,
            actor=actor,
            payload={"must_consume_turn": bool(must_consume_turn)},
            stage_goal=(
                "这是白花碑驿站伏击中的真实玩家回合。队伍想保护失忆旅人并取得安全退路，"
                "但你仍应根据角色当前伤势、已公开命刻、敌人和同伴行动自行决定这一回合做什么。"
                "只声明一个合法行动，不替GM宣布结果；若有人重伤，可以救援或掩护，"
                "若局势允许，也可以推进旧路、妨碍敌人或发动攻击。"
            ),
        )
        scenario = ReplayScenario(
            name="第一章白花碑驿站实时冲突",
            campaign_id=self.campaign_id,
            session_id=self.session_id,
            channel_id=self.channel_id,
            participants=list(self.participants),
            steps=[step],
        )
        recent_public_context = self._recent_public_dialogue(limit=10)
        legal_context = self.player_legal_actions.build(
            self.service,
            scenario,
            step,
            public_context=recent_public_context,
        )
        last_gm_reply = next(
            (
                str(call.get("reply") or "")
                for call in reversed(self.calls)
                if str(call.get("reply") or "").strip()
            ),
            "",
        )
        utterance = self.player_simulator.compose(
            step=step,
            legal_context=legal_context,
            last_gm_reply=last_gm_reply,
            recent_public_context=recent_public_context,
        )
        message = str(utterance.text or "").strip()
        for prefix in (f"{speaker}:", f"{speaker}："):
            if message.startswith(prefix):
                message = message[len(prefix) :].strip()
        if not message or utterance.used_fallback:
            message = self._combat_action_fallback(actor)
        self.player_simulation_metrics.append(
            {
                "kind": "combat_turn",
                "turn": turn_number,
                "speaker": speaker,
                "actor": actor,
                "current_actor": str(legal_context.current_actor or ""),
                "model": str(getattr(self.player_simulator, "model", "") or ""),
                "used_fallback": bool(utterance.used_fallback),
                "validation_errors": list(utterance.validation_errors or []),
                "fallback_kind": str(utterance.fallback_kind or ""),
                "model_attempts": list(utterance.model_attempts or []),
                "text": message,
            }
        )
        return message

    def _current_actor_already_used_free_speech(self, actor: str) -> bool:
        """同一回合允许一次自由问答，之后必须落到规则行动。"""

        speaker_by_hero = dict(zip(self.pc_names, self.participants))
        expected_speaker = speaker_by_hero.get(actor, "")
        for call in reversed(self.calls):
            label = str(call.get("label") or "")
            if label.startswith("第一章敌方自然回合") or "冲突开场敌方回合" in label:
                return False
            if not label.startswith("第一章实时玩家回合"):
                continue
            return str(call.get("speaker") or "") == expected_speaker
        return False

    def _combat_action_fallback(self, actor: str) -> str:
        """上游不可用时保持规则合法；不修改骰子、难度或敌方决策。"""

        app = self._runtime().app
        if actor == "赛璃" and app.character_manager.exists(actor):
            healer = app.character_manager.get(actor)
            injured = [
                app.character_manager.get(name)
                for name in self.pc_names
                if app.character_manager.exists(name)
                and 0 < int(app.character_manager.get(name).hp or 0)
                < int(app.character_manager.get(name).max_hp or 0)
            ]
            if injured and int(healer.mp or 0) >= 10 and "治愈术" in healer.spells:
                target = min(injured, key=lambda item: int(item.hp or 0))
                return f"赛璃对{target.name}施放治愈术，先把伤势稳住。"
        return {
            "伊莉雅": "伊莉雅把盾抵住旧闸门的反冲，尝试为旅人撑开一条能撤离的缝隙。",
            "赛璃": "赛璃辨认闸门上的白花祷纹，尝试校正机关的开启顺序。",
            "洛岚": "洛岚沿着齿轮咬合声拆解旧锁，尝试打开旧路闸门。",
            "艾薇娅": "艾薇娅要求守望会巡守解除最后一道保险，尝试为众人打开旧路。",
            "苍祈": "苍祈让奥灵低语穿过门缝，尝试稳定正在反冲的古老机关。",
        }.get(actor, f"{actor}采取防御，观察眼前最迫近的危险。")

    def _recent_public_dialogue(self, *, limit: int = 10) -> str:
        """为 FU-PL 提供最近公开群聊，不暴露控制接口和幕后提示。"""

        public_routes = {
            "/v1/game/turn",
            "/v1/session-zero/message",
            "/v1/session/heartbeat",
            "/v1/message/route",
        }
        lines: list[str] = []
        for call in reversed(self.calls):
            if str(call.get("route") or "") not in public_routes:
                continue
            speaker = str(call.get("speaker") or "").strip()
            message = " ".join(str(call.get("message") or "").split())
            reply = " ".join(str(call.get("reply") or "").split())
            if reply:
                lines.append(f"时悠：{reply[-700:]}")
            if speaker and message and str(call.get("route") or "") != "/v1/session/heartbeat":
                lines.append(f"{speaker}：{message[-500:]}")
            if len(lines) >= max(2, limit * 2):
                break
        return "\n".join(reversed(lines[: max(2, limit * 2)]))

    @staticmethod
    def _clock_is_complete(clock_manager: Any, name: str) -> bool:
        """Treat fulfilled active clocks and retired tombstones identically."""

        if clock_manager.exists(name):
            clock = clock_manager.get(name)
            return bool(
                int(clock.max_segments or 0) > 0
                and int(clock.current or 0) >= int(clock.max_segments or 0)
            )
        return bool(clock_manager.is_retired(name))

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

    def _record_chapter_opening_usage(self) -> None:
        app = self._runtime().app
        if app.scene_manager.current_scene is None:
            raise RuntimeError("正式第一章开场尚未建立场景，不能伪造章节运行记录。")
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
            summary="赛璃在非冲突场景施放御魂仪式【风铃回声】，章节包仪式场景已实际使用。",
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
        """Run destructive villain mechanics in a transactionally restored sandbox."""

        app = self._runtime().app
        snapshot = CampaignStateTransaction.capture(app, self.campaign_id)
        try:
            self._exercise_villain_conflict_tools_in_sandbox()
        finally:
            CampaignStateTransaction.restore(app, snapshot)

    def _exercise_villain_conflict_tools_in_sandbox(self) -> None:
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

    def _prepare_conflict_state(self) -> bool:
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
        existing_arrival = next(
            (
                call
                for call in reversed(self.calls)
                if call.get("label") == "第一章GM主动兑现财团抵达"
            ),
            None,
        )
        if existing_arrival and self._gm_beat_committed_conflict_arrival():
            self.conflict_arrival_beat = dict(existing_arrival.get("body") or {})
        else:
            self.conflict_arrival_beat = self.invoke(
                "第一章GM主动兑现财团抵达",
                "POST",
                "/v1/session/heartbeat",
                {
                    **self.common,
                    "speaker": "系统主动节拍",
                    "message": "",
                    "auto_respond": True,
                    "force": True,
                    "cooldown_seconds": 0,
                    "adventure_idle_seconds": 0,
                    "instruction": (
                        "【局势提交】外部的车轮与靴跟声已经持续逼近，当前场景需要兑现这项既有压力。"
                        "让监察官艾蕾娜带领财团机兵和财团狙击手现在抵达白花碑驿站并封住旧路，"
                        "把这一NPC行动作为已经发生的公开变化提交；若存在【财团巡逻队逼近】命刻，"
                        "同步把它作为已经兑现的威胁收束。不要替任何玩家角色行动。"
                    ),
                },
            )
        arrival_reply = str(self.conflict_arrival_beat.get("reply") or "")
        arrival_committed = self._gm_beat_committed_conflict_arrival()
        if not (
            any(token in arrival_reply for token in ("监察官", "艾蕾娜"))
            and self._reply_describes_conflict_arrival(arrival_reply)
            and arrival_committed
        ):
            self.errors.append(
                "GM 主动节拍没有把持续逼近的财团威胁同时兑现为公开叙述和权威状态，不能无缝进入冲突。"
            )
            return False
        if not app.clock_manager.exists("旧路闸门开启"):
            app.clock_manager.add(
                Clock(
                    name="旧路闸门开启",
                    max_segments=6,
                    current=0,
                    clock_type="objective",
                    stakes="旧路开启后队伍可撤离冲突。",
                    completion_consequence="旧路闸门已经开启，队伍获得了撤离现场的通路。",
                )
            )
        if not app.clock_manager.exists("艾蕾娜启动记忆集中协议"):
            app.clock_manager.add(
                Clock(
                    name="艾蕾娜启动记忆集中协议",
                    max_segments=8,
                    current=0,
                    clock_type="villain",
                    stakes="记忆集中协议距离完成的进度。",
                    completion_consequence=(
                        "艾蕾娜已经把失忆旅人的记忆上传到第七采掘城，"
                        "旅人的现存记忆因此遭到财团封存。"
                    ),
                    auto_advance="每轮结束推进1格",
                    scope="scene",
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
        if not app.conflict_manager.state.active:
            self.invoke(
                "第一章冲突启动",
                "POST",
                "/v1/game/turn",
                {
                    **self.common,
                    "speaker": "阿凛",
                    "message": (
                        "伊莉雅看见已经抵达并封住旧路的监察官艾蕾娜、财团机兵和财团狙击手，立刻举盾喊出警戒。"
                        "玩家方是伊莉雅、赛璃、洛岚、艾薇娅、苍祈；伊莉雅请求进入冲突场景【白花碑驿站伏击】，"
                        "并按敏捷+洞察发起团队先攻检定；其他玩家会分别决定是否支援。"
                    ),
                },
            )
        if not app.conflict_manager.state.active:
            self._resume_blocking_decision_if_needed()
        if not app.conflict_manager.state.active:
            self.errors.append(
                "玩家已经根据公开抵达局势请求进入冲突，但 GM 没有调用 start_conflict；测试不会暗中强开冲突。"
            )
            return False
        return True

    def _exercise_core_design_tools(self) -> None:
        """Cover GM-facing design helpers that are hard to trigger organically in one scene."""

        app = self._runtime().app
        snapshot = CampaignStateTransaction.capture(app, self.campaign_id)
        try:
            self._exercise_core_design_tools_in_sandbox()
        finally:
            CampaignStateTransaction.restore(app, snapshot)

    def _exercise_core_design_tools_in_sandbox(self) -> None:
        """Execute coverage probes without modifying the campaign under test."""

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

        rest_threat_clocks = [
            clock.name
            for clock in app.clock_manager.all()
            if bool(clock.advance_on_rest)
            and str(clock.scope or "").strip().lower() in {"session", "campaign"}
            and str(clock.status or "active").strip().lower() == "active"
            and int(clock.current or 0) < int(clock.max_segments or 0)
        ]
        rest_result = app.take_rest(
            RestType.SETTLEMENT,
            safe_source="白花碑驿站安全厢房",
            threat_clocks=rest_threat_clocks,
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
            "覆盖旅行、休息、地下城、工程、章节结算、经验奖励、故事弧压力推进、稀有装备审批和法术别名规范。",
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
            "combat_uses_current_actor_player_simulation": bool(
                [
                    item
                    for item in self.player_simulation_metrics
                    if str(item.get("kind") or "combat_turn") == "combat_turn"
                ]
            )
            and all(
                str(item.get("actor") or "")
                == str(item.get("current_actor") or "")
                for item in self.player_simulation_metrics
                if str(item.get("kind") or "combat_turn") == "combat_turn"
            ),
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
            "group_concept_preserved": "临时守护者" in world.group_concept,
            "no_healing_misroute": not any(
                "治愈术" in call["message"] and "援用特质" in call["reply"] for call in self.calls
            ),
            "no_sticky_opportunity_preference": not self._has_sticky_opportunity_preference(),
            "no_repeated_out_of_turn_deadlock": not self._has_repeated_out_of_turn_deadlock(),
            "chapter_opening_described_scene": self._chapter_opening_described_scene(),
            "chapter_opening_uses_prepared_required_npc": self._chapter_opening_uses_prepared_required_npc(),
            "npc_answer_only_on_request": self._npc_answer_only_on_request(),
            "direct_npc_request_received_answer": self._direct_npc_request_received_answer(),
            "gm_beat_committed_conflict_arrival": self._gm_beat_committed_conflict_arrival(),
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
        if not checks["combat_uses_current_actor_player_simulation"]:
            self.errors.append("第一章冲突没有由当前行动者的公开视角 FU-PL 驱动。")
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
        if not checks["chapter_opening_uses_prepared_required_npc"]:
            self.errors.append("第一章开场没有使用章节契约要求的关键 NPC，或另造了功能重叠的替代人物。")
        if not checks["npc_answer_only_on_request"]:
            self.errors.append("NPC 明确答复在非询问回合重复出现。")
        if not checks["direct_npc_request_received_answer"]:
            self.errors.append("玩家直接要求守望会会长明确答复时，NPC 没有通过权威答复工具作出决定。")
        if not checks["gm_beat_committed_conflict_arrival"]:
            self.errors.append("进入战斗前的 GM 主动节拍没有用权威工具提交财团抵达。")
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
            blockers = getattr(self, "gate_body", {}).get("blockers", {})
            reason = str(blockers.get("reason") or "") if isinstance(blockers, dict) else ""
            missing_world = (
                blockers.get("session_zero", {}).get("missing", [])
                if isinstance(blockers, dict)
                else []
            )
            missing_heroes = (
                blockers.get("hero_creation", {}).get("missing_by_player", {})
                if isinstance(blockers, dict)
                else {}
            )
            detail = missing_world or list(missing_heroes) or ([reason] if reason else [])
            suffix = "：" + "、".join(str(item) for item in detail) if detail else ""
            self.errors.append(f"进入第一章时仍被第零章门控阻挡{suffix}。")
        if elapsed_values:
            avg_ms = int(mean(elapsed_values))
        else:
            avg_ms = 0
        return {
            "ok": not self.errors,
            "test_fidelity": dict(self.test_fidelity),
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
            "player_simulator_telemetry": self._player_simulator_telemetry(),
            "codex_subagent_telemetry": (
                self.test_llm_bundle.core.telemetry_payload()
                if self.test_llm_bundle is not None
                else {}
            ),
            "player_simulation_metrics": self.player_simulation_metrics,
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

    def _player_simulator_telemetry(self) -> dict[str, Any]:
        telemetry_getter = getattr(self.player_simulator, "telemetry_payload", None)
        telemetry = (
            dict(telemetry_getter() or {})
            if callable(telemetry_getter)
            else {}
        )
        return {
            "engine": str(getattr(self.player_simulator, "engine_name", "") or ""),
            "model": str(getattr(self.player_simulator, "model", "") or ""),
            "llm_active": bool(getattr(self.player_simulator, "use_llm", False)),
            "total_calls": int(telemetry.get("total_calls") or 0),
            "failed_calls": int(telemetry.get("failed_calls") or 0),
            "latency": dict(telemetry.get("latency") or {}),
            "prompt_cache": dict(telemetry.get("prompt_cache") or {}),
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
        has_location = "白花碑驿站" in reply
        has_present_character = any(token in reply for token in ("玛蕾娅", "旅人", "守望会", "巡守", "驿卒"))
        has_immediate_pressure = any(token in reply for token in ("官道", "车轮", "靴", "财团", "逼近", "靠近"))
        return has_location and has_present_character and has_immediate_pressure

    def _chapter_opening_uses_prepared_required_npc(self) -> bool:
        opening = next(
            (call for call in self.calls if call.get("label") == "第一章 GM 开场"),
            None,
        )
        if not opening:
            return False
        reply = str(opening.get("reply") or "")
        if "白花守望会会长" not in reply:
            return False
        created_names = {
            str((receipt.get("result") or {}).get("npc", {}).get("name") or "").strip()
            for receipt in list((opening.get("body") or {}).get("tool_receipts") or [])
            if isinstance(receipt, dict)
            and str(receipt.get("tool_name") or "") == "create_npc_profile"
        }
        return "玛蕾娅" not in created_names

    def _npc_answer_only_on_request(self) -> bool:
        answer_receipts: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for call in self.calls:
            for receipt in list((call.get("body") or {}).get("tool_receipts") or []):
                if (
                    isinstance(receipt, dict)
                    and receipt.get("ok") is True
                    and str(receipt.get("tool_name") or "")
                    in {"decide_npc_response", "decide_collective_response"}
                ):
                    answer_receipts.append((call, receipt))
        if not answer_receipts:
            return False
        for call, receipt in answer_receipts:
            source_event = dict((receipt.get("result") or {}).get("source_event") or {})
            if (
                not str(source_event.get("event_id") or "").strip()
                or str(source_event.get("text") or "").strip()
                != str(call.get("message") or "").strip()
                or str(source_event.get("speaker") or "").strip()
                != str(call.get("speaker") or "").strip()
            ):
                return False
        return True

    def _direct_npc_request_received_answer(self) -> bool:
        requested = next(
            (
                call
                for call in self.calls
                if str(call.get("speaker") or "") == "时雨"
                and "旧路能不能借" in str(call.get("message") or "")
                and "明确答复" in str(call.get("message") or "")
            ),
            None,
        )
        if not requested:
            return False
        receipts = list((requested.get("body") or {}).get("tool_receipts") or [])
        return any(
            isinstance(receipt, dict)
            and receipt.get("ok") is True
            and str(receipt.get("tool_name") or "")
            in {"decide_npc_response", "decide_collective_response"}
            for receipt in receipts
        )

    def _gm_beat_committed_conflict_arrival(self) -> bool:
        """确认财团抵达同时存在公开叙述和权威状态回执。"""

        arrival = next(
            (
                call
                for call in reversed(self.calls)
                if call.get("label") == "第一章GM主动兑现财团抵达"
            ),
            None,
        )
        if not arrival:
            return False
        reply = str(arrival.get("reply") or "")
        if not any(token in reply for token in ("监察官", "艾蕾娜")) or not (
            self._reply_describes_conflict_arrival(reply)
        ):
            return False
        accepted_tools = {
            "introduce_npc",
            "decide_npc_action",
            "decide_collective_action",
            "update_npc_state",
            "commit_scene_response",
            "move_group_within_scene",
            "move_scene_group",
        }
        receipts = list((arrival.get("body") or {}).get("tool_receipts") or [])
        named_actor_committed = any(
            token in reply for token in ("监察官艾蕾娜", "监察官")
        ) or any(
            isinstance(receipt, dict)
            and receipt.get("ok") is True
            and self._receipt_names_npc(receipt, "监察官艾蕾娜")
            for receipt in receipts
        )
        material_change_committed = any(
            isinstance(receipt, dict)
            and receipt.get("ok") is True
            and receipt.get("state_changed") is True
            and str(receipt.get("tool_name") or "") in accepted_tools
            for receipt in receipts
        )
        return named_actor_committed and material_change_committed

    @staticmethod
    def _reply_describes_conflict_arrival(reply: str) -> bool:
        """接受自然叙述中等价的抵达或封锁表达。"""

        text = str(reply or "")
        return any(
            token in text
            for token in (
                "抵达",
                "来到",
                "踏进",
                "进入碑群",
                "走来",
                "现身",
                "封住",
                "封死",
                "封锁",
                "拦住",
                "堵住",
            )
        )

    @staticmethod
    def _receipt_names_npc(receipt: dict[str, Any], expected: str) -> bool:
        result = receipt.get("result") or {}
        if not isinstance(result, dict):
            return False
        values = [result.get("npc"), result.get("actor"), result.get("name")]
        for value in values:
            if isinstance(value, dict):
                value = value.get("name")
            if str(value or "").strip() == expected:
                return True
        return False

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
            "=== 测试真实性边界 ===",
            f"分类: {report.get('test_fidelity', {}).get('classification', 'unknown')}",
            "已验证生产端到端: "
            f"{report.get('test_fidelity', {}).get('production_e2e_verified', False)}",
            "直接注入路径:",
            *(
                [
                    f"- {item}"
                    for item in report.get("test_fidelity", {}).get(
                        "direct_component_paths",
                        [],
                    )
                ]
                or ["- 无"]
            ),
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="运行FU-GM第零章到第一章完整在线长测。",
    )
    parser.add_argument(
        "--resume-after-arrival",
        type=Path,
        metavar="RUN_ROOT",
        help="从已有长测目录中财团抵达前后任一安全检查点继续冲突段。",
    )
    parser.add_argument(
        "--resume-after-session-zero",
        type=Path,
        metavar="RUN_ROOT",
        help="从已有长测目录中的第零章检查点继续，只重放尚未成功的步骤。",
    )
    parser.add_argument(
        "--codex-subagent-spool",
        type=Path,
        metavar="SPOOL_ROOT",
        help="显式测试模式：把全部语言模型请求写入本地 Codex 子智能体队列。",
    )
    args = parser.parse_args(argv)
    if args.resume_after_arrival:
        return FromScratchUltraHarness.from_run_root(
            args.resume_after_arrival,
            codex_spool_root=args.codex_subagent_spool,
        ).run_resume_after_arrival()
    if args.resume_after_session_zero:
        return FromScratchUltraHarness.from_run_root(
            args.resume_after_session_zero,
            codex_spool_root=args.codex_subagent_spool,
        ).run_resume_after_session_zero()
    return FromScratchUltraHarness(
        codex_spool_root=args.codex_subagent_spool,
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
