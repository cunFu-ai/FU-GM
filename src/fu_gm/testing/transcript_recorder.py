from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from fu_gm.testing.replay_models import ReplayCallRecord, ReplayScenario


class TranscriptRecorder:
    def __init__(self, run_root: str | Path, scenario: ReplayScenario) -> None:
        self.run_root = Path(run_root)
        self.scenario = scenario
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.records_path = self.run_root / "replay_records.jsonl"
        self.conversation_path = self.run_root / "full_conversation.txt"
        self.report_path = self.run_root / "replay_report.md"
        self.telemetry_path = self.run_root / "telemetry.json"
        self.conversation_path.write_text(self._header(), encoding="utf-8")

    def append(self, record: ReplayCallRecord) -> None:
        with self.records_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(record), ensure_ascii=False, default=str) + "\n")
        with self.conversation_path.open("a", encoding="utf-8") as fh:
            fh.write(self._format_record(record))

    def write_report(
        self,
        *,
        records: list[ReplayCallRecord],
        errors: list[str],
        warnings: list[str],
        telemetry: dict[str, Any],
    ) -> None:
        elapsed = [record.elapsed_ms for record in records]
        avg = int(sum(elapsed) / len(elapsed)) if elapsed else 0
        max_elapsed = max(elapsed) if elapsed else 0
        lines = [
            f"# Replay Report: {self.scenario.name}",
            "",
            f"- result: {'FAIL' if errors else 'PASS'}",
            f"- records: {len(records)}",
            f"- avg_elapsed_ms: {avg}",
            f"- max_elapsed_ms: {max_elapsed}",
            f"- conversation_txt: `{self.conversation_path}`",
            f"- records_jsonl: `{self.records_path}`",
            "",
            "## Errors",
        ]
        lines.extend(f"- {error}" for error in errors) if errors else lines.append("- none")
        lines.append("")
        lines.append("## Warnings")
        lines.extend(f"- {warning}" for warning in warnings) if warnings else lines.append("- none")
        lines.append("")
        lines.append("## Slowest Calls")
        slowest = sorted(records, key=lambda item: item.elapsed_ms, reverse=True)[:10]
        lines.extend(
            f"- #{record.index} {record.label}: {record.elapsed_ms}ms status={record.status}"
            for record in slowest
        )
        self.report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.telemetry_path.write_text(json.dumps(telemetry, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def _header(self) -> str:
        return "\n".join(
            [
                f"FU-GM Human-like Replay Transcript",
                f"scenario: {self.scenario.name}",
                f"campaign_id: {self.scenario.campaign_id}",
                f"session_id: {self.scenario.session_id}",
                f"started_at: {datetime.now().isoformat(timespec='seconds')}",
                "",
            ]
        )

    def _format_record(self, record: ReplayCallRecord) -> str:
        parts = [
            f"--- {record.index}. {record.label or record.step_id} | {record.elapsed_ms}ms | status={record.status} ok={record.ok} ---",
        ]
        if record.speaker or record.message:
            parts.append(f"{record.speaker}: {record.message}".strip())
        if record.reply:
            parts.append(f"GM: {record.reply}")
        if record.validation_errors:
            parts.append("VALIDATION ERRORS: " + " | ".join(record.validation_errors))
        parts.append("")
        return "\n".join(parts)
