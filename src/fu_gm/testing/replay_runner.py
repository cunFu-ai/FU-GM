from __future__ import annotations

import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fu_gm.http_server import FUGMHttpService
from fu_gm.testing.legal_actions import LegalActionLayer
from fu_gm.testing.player_simulator import ConstrainedPlayerSimulator
from fu_gm.testing.replay_models import ReplayCallRecord, ReplayScenario, ReplayStep
from fu_gm.testing.replay_validators import ReplayValidators
from fu_gm.testing.transcript_recorder import TranscriptRecorder


class HumanLikeReplayRunner:
    def __init__(
        self,
        scenario: ReplayScenario,
        *,
        service: FUGMHttpService | None = None,
        output_root: str | Path = ".runtime/replay_tests",
        use_llm_gm: bool = False,
        use_llm_player: bool = False,
    ) -> None:
        self.scenario = scenario
        self.stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_root = Path(output_root) / f"{self._safe_name(scenario.name)}_{self.stamp}"
        self.service = service or FUGMHttpService(data_root=self.run_root / "campaigns", use_llm=use_llm_gm)
        self.legal_actions = LegalActionLayer()
        self.player = ConstrainedPlayerSimulator(use_llm=use_llm_player)
        self.validators = ReplayValidators()
        self.recorder = TranscriptRecorder(self.run_root, scenario)
        self.records: list[ReplayCallRecord] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.last_gm_reply = ""

    def run(self) -> dict[str, Any]:
        for step in self.scenario.steps:
            record = self._run_step(step)
            self.records.append(record)
            self.recorder.append(record)
            self.errors.extend(record.validation_errors)
            self.last_gm_reply = record.reply
        telemetry = self._telemetry()
        self.recorder.write_report(
            records=self.records,
            errors=self.errors,
            warnings=self.warnings,
            telemetry=telemetry,
        )
        return {
            "ok": not self.errors,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "run_root": str(self.run_root),
            "conversation_txt": str(self.recorder.conversation_path),
            "records_jsonl": str(self.recorder.records_path),
            "report_md": str(self.recorder.report_path),
            "telemetry_json": str(self.recorder.telemetry_path),
            "telemetry": telemetry,
        }

    def _run_step(self, step: ReplayStep) -> ReplayCallRecord:
        legal_context = self.legal_actions.build(self.service, self.scenario, step)
        message = step.message
        simulator_errors: list[str] = []
        if self._step_needs_player_message(step) and not message:
            simulated = self.player.compose(
                step=step,
                legal_context=legal_context,
                last_gm_reply=self.last_gm_reply,
            )
            message = simulated.text
            simulator_errors = list(simulated.validation_errors or [])
            if simulated.used_fallback and self.player.use_llm:
                self.warnings.append(f"{step.label or step.id}: player_simulator_used_fallback")
        method, endpoint, payload = self._route_for_step(step, message)
        started = time.perf_counter()
        status, raw_body = self.service.handle(method, endpoint, payload)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        body = raw_body if isinstance(raw_body, dict) else {"ok": status < 400, "raw": str(raw_body)}
        reply = str(body.get("reply") or body.get("message") or body.get("raw") or "")
        record = ReplayCallRecord(
            index=len(self.records) + 1,
            step_id=step.id,
            label=step.label or step.id,
            method=method,
            endpoint=endpoint,
            speaker=step.speaker,
            message=message,
            status=status,
            elapsed_ms=elapsed_ms,
            ok=bool(body.get("ok", status < 400)),
            reply=reply,
            body=body,
            legal_context=asdict(legal_context),
            validation_errors=[],
        )
        validation = self.validators.validate_record(record, step)
        record.validation_errors = [*simulator_errors, *validation.errors]
        self.warnings.extend(validation.warnings)
        return record

    def _route_for_step(self, step: ReplayStep, message: str) -> tuple[str, str, dict[str, Any]]:
        if step.endpoint:
            payload = {**self.scenario.common_payload, **step.payload}
            if message:
                payload.setdefault("message", message)
            if step.speaker:
                payload.setdefault("speaker", step.speaker)
            return step.method, step.endpoint, payload

        common = self.scenario.common_payload
        if step.kind == "new_campaign":
            return "POST", "/v1/campaigns/new", {"campaign_id": self.scenario.campaign_id, **step.payload}
        if step.kind == "session_gate":
            return "POST", "/v1/session/gate", {**common, **step.payload}
        if step.kind == "session_zero_start":
            return "POST", "/v1/session-zero/start", {**common, "participants": self.scenario.participants, **step.payload}
        if step.kind == "session_zero_message":
            return "POST", "/v1/session-zero/message", {**common, "speaker": step.speaker, "message": message, **step.payload}
        if step.kind == "game_turn":
            return "POST", "/v1/game/turn", {**common, "speaker": step.speaker, "message": message, **step.payload}
        if step.kind == "session_end":
            return "POST", "/v1/session/end", {**common, **step.payload}
        if step.kind == "audit":
            query = {
                "campaign_id": self.scenario.campaign_id,
                "session_id": self.scenario.session_id,
                "channel_id": self.scenario.channel_id,
                "limit": str(step.payload.get("limit", 80)),
                "include_private": str(step.payload.get("include_private", False)).lower(),
            }
            return "GET", "/v1/audit/dashboard?" + urlencode(query), {}
        raise ValueError(f"未知回放步骤类型：{step.kind}")

    def _step_needs_player_message(self, step: ReplayStep) -> bool:
        return step.kind in {"session_zero_message", "game_turn"}

    def _telemetry(self) -> dict[str, Any]:
        http_spans = list(getattr(self.service, "recent_http_spans", []))
        runtime = self.service.runtimes.get(self.scenario.campaign_id)
        app_telemetry = runtime.app.pipeline_telemetry() if runtime else {}
        return {
            "http": http_spans[-50:],
            "pipeline": app_telemetry,
            "records": [
                {
                    "index": record.index,
                    "label": record.label,
                    "elapsed_ms": record.elapsed_ms,
                    "status": record.status,
                    "ok": record.ok,
                }
                for record in self.records
            ],
        }

    def _safe_name(self, value: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip())
        return safe or "replay"
