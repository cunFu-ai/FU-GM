from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

from fu_gm.components.session_prep_concretizer import SessionPrepConcretizer
from fu_gm.config import LLMConfig, resolve_model_api_key
from fu_gm.http_server import FUGMHttpService
from fu_gm.llm_client import ChatMessage, OpenAICompatibleClient
from fu_gm.llm_utils import extract_json_object
from fu_gm.testing.kariba_fixture import seed_kariba_ready_campaign


OFFICIAL_BASE_URL = "https://api.deepseek.com"
EXPECTED_MODEL = "deepseek-v4-flash"


class CaptureComplete(RuntimeError):
    pass


class RequestCaptureClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_chat_completion(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        raise CaptureComplete("session-prep request captured before transport")


class PayloadCaptureTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "url": url,
                "payload": payload,
                "timeout": timeout,
                "authorization_present": bool(headers.get("Authorization")),
            }
        )
        return {
            "id": "local-preflight",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "{}"},
                }
            ],
        }


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def provider_config(values: dict[str, str]) -> LLMConfig:
    model = str(
        values.get("FU_GM_CREATIVE_MODEL")
        or values.get("FU_GM_EXPRESSOR_MODEL")
        or values.get("FU_GM_ACTION_MODEL")
        or ""
    ).strip()
    base_url = str(
        values.get("FU_GM_CREATIVE_API_BASE_URL")
        or values.get("FU_GM_EXPRESSOR_API_BASE_URL")
        or values.get("FU_GM_API_BASE_URL")
        or ""
    ).strip().rstrip("/")
    fallback_key = str(
        values.get("FU_GM_CREATIVE_API_KEY")
        or values.get("FU_GM_EXPRESSOR_API_KEY")
        or values.get("FU_GM_API_KEY")
        or ""
    ).strip()
    api_key = resolve_model_api_key(
        model,
        fallback_key,
        values=values,
    )
    if base_url != OFFICIAL_BASE_URL:
        raise ValueError(
            "probe refuses non-official endpoint: "
            f"expected {OFFICIAL_BASE_URL}, got {base_url or '<empty>'}"
        )
    if model != EXPECTED_MODEL:
        raise ValueError(
            "probe refuses unexpected model: "
            f"expected {EXPECTED_MODEL}, got {model or '<empty>'}"
        )
    if not api_key:
        raise ValueError("DeepSeek API key is missing")
    return LLMConfig(
        api_base_url=base_url,
        api_key=api_key,
        action_model=model,
        expressor_model=model,
        backup_api_base_urls=(),
        timeout_seconds=60.0,
        endpoint_attempt_timeout_seconds=45.0,
        reasoning_effort="",
        thinking_enabled=False,
        response_format_enabled=True,
        prompt_cache_enabled=True,
        prompt_cache_mode=str(
            values.get("FU_GM_PROMPT_CACHE_MODE") or "key"
        ).strip().lower(),
        prompt_cache_key_prefix=str(
            values.get("FU_GM_PROMPT_CACHE_KEY_PREFIX") or "fugm-probe"
        ).strip(),
        prompt_cache_ttl="30m",
        reactive_recovery_enabled=True,
        reactive_recovery_max_retries=1,
        reactive_recovery_target_chars=48000,
        allow_heuristic_fallback=False,
    )


def capture_current_request(model: str) -> dict[str, Any]:
    capture = RequestCaptureClient()
    with tempfile.TemporaryDirectory(prefix="fu-gm-deepseek-probe-") as data_root:
        os.environ["FU_GM_IMAGE_ENABLED"] = "0"
        os.environ["FU_GM_IMAGE_OUTPUT_DIR"] = str(
            Path(data_root) / "generated-images"
        )
        os.environ["FU_GM_NORTANTIS_OUTPUT_DIR"] = str(
            Path(data_root) / "nortantis-maps"
        )
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = seed_kariba_ready_campaign(
            service,
            campaign_id="deepseek-session-prep-probe",
            session_id="session-one",
            channel_id="isolated-probe",
            skip_map_render=True,
        )
        concretizer = (
            runtime.app.campaign_pacing_manager.contract_planner.concretizer
        )
        concretizer.client = capture
        concretizer.model = model
        runtime.app.campaign_pacing_manager.refresh_plan(
            force_session_number=1,
            deadline=time.monotonic() + 300.0,
        )
    if len(capture.calls) != 1:
        raise RuntimeError(
            "expected exactly one captured primary session-prep call, "
            f"got {len(capture.calls)}"
        )
    request = capture.calls[0]
    messages = request.get("messages")
    if not isinstance(messages, list) or not all(
        isinstance(item, ChatMessage) for item in messages
    ):
        raise RuntimeError("captured request did not contain ChatMessage objects")
    return request


def prompt_fingerprint(messages: list[ChatMessage]) -> tuple[str, int]:
    serialized = json.dumps(
        [
            {"role": message.role, "content": message.content}
            for message in messages
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest(), sum(
        len(message.content) for message in messages
    )


def preflight_wire_payload(
    config: LLMConfig,
    request: dict[str, Any],
) -> dict[str, Any]:
    transport = PayloadCaptureTransport()
    client = OpenAICompatibleClient(config, transport=transport)
    client.create_chat_completion(
        model=config.action_model,
        messages=list(request["messages"]),
        temperature=float(request["temperature"]),
        response_format=dict(request["response_format"]),
        max_tokens=int(request["max_tokens"]),
        deadline=time.monotonic() + 5.0,
        operation="session_prep_probe.preflight",
        thinking_enabled=False,
        max_recovery_retries=1,
        retry_without_response_format_on_empty=True,
    )
    if len(transport.calls) != 1:
        raise RuntimeError("wire preflight produced an unexpected call count")
    call = transport.calls[0]
    payload = dict(call["payload"])
    if call["url"] != f"{OFFICIAL_BASE_URL}/chat/completions":
        raise RuntimeError("wire preflight endpoint mismatch")
    if payload.get("model") != EXPECTED_MODEL:
        raise RuntimeError("wire preflight model mismatch")
    if payload.get("thinking") != {"type": "disabled"}:
        raise RuntimeError("wire preflight did not explicitly disable thinking")
    if payload.get("response_format") != {"type": "json_object"}:
        raise RuntimeError("wire preflight did not enable JSON Output")
    if int(payload.get("max_tokens") or 0) <= 0:
        raise RuntimeError("wire preflight did not set max_tokens")
    return {
        "endpoint": call["url"],
        "model": payload["model"],
        "thinking": payload["thinking"],
        "response_format": payload["response_format"],
        "max_tokens": payload["max_tokens"],
        "prompt_cache_key_present": bool(payload.get("prompt_cache_key")),
        "authorization_present": bool(call["authorization_present"]),
    }


def percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * max(0.0, min(1.0, fraction)))
    return ordered[index]


def run_probe(
    *,
    config: LLMConfig,
    request: dict[str, Any],
    run_count: int,
    output_dir: Path,
) -> dict[str, Any]:
    messages = list(request["messages"])
    prompt_sha256, prompt_chars = prompt_fingerprint(messages)
    client = OpenAICompatibleClient(config)
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    runs_path = output_dir / "runs.jsonl"
    rows: list[dict[str, Any]] = []
    with runs_path.open("x", encoding="utf-8") as handle:
        os.chmod(runs_path, 0o600)
        for index in range(1, run_count + 1):
            operation = f"session_prep_probe.run_{index}"
            started = time.monotonic()
            content = ""
            error_type = ""
            error_message = ""
            try:
                content = client.create_chat_completion(
                    model=config.action_model,
                    messages=messages,
                    temperature=float(request["temperature"]),
                    response_format=dict(request["response_format"]),
                    max_tokens=int(request["max_tokens"]),
                    deadline=time.monotonic() + 60.0,
                    operation=operation,
                    thinking_enabled=False,
                    max_recovery_retries=1,
                    retry_without_response_format_on_empty=True,
                )
            except Exception as exc:  # Probe records provider failures as data.
                error_type = type(exc).__name__
                error_message = " ".join(str(exc).split())[:300]
            elapsed_ms = int((time.monotonic() - started) * 1000)
            records = [
                dict(item)
                for item in client.recent_calls
                if item.get("operation") == operation
            ]
            last_record = records[-1] if records else {}
            strict_object = False
            project_parse = False
            schema_valid = False
            required_coverage = 0.0
            if content.strip():
                try:
                    strict_payload = json.loads(content)
                    strict_object = isinstance(strict_payload, dict)
                except (TypeError, ValueError):
                    strict_payload = None
                try:
                    project_payload = extract_json_object(content)
                    project_parse = isinstance(project_payload, dict)
                except Exception:
                    project_payload = None
                if isinstance(project_payload, dict):
                    required = SessionPrepConcretizer._REQUIRED_OUTPUT_FIELDS
                    required_coverage = round(
                        len(required.intersection(project_payload))
                        / max(1, len(required)),
                        4,
                    )
                    try:
                        SessionPrepConcretizer._validate_main_payload(
                            project_payload
                        )
                        schema_valid = True
                    except (TypeError, ValueError):
                        schema_valid = False
            row = {
                "run": index,
                "elapsed_ms": elapsed_ms,
                "logical_success": not error_type and schema_valid,
                "error_type": error_type,
                "error_message": error_message,
                "physical_attempts": len(records),
                "recovered": len(records) > 1 and not error_type,
                "response_format_sequence": [
                    bool(item.get("response_format")) for item in records
                ],
                "thinking_enabled_sequence": [
                    bool(item.get("thinking_enabled")) for item in records
                ],
                "finish_reason": str(last_record.get("finish_reason") or ""),
                "content_chars": len(content),
                "reasoning_chars": int(last_record.get("reasoning_chars") or 0),
                "empty_content": not bool(content.strip()),
                "strict_json_object": strict_object,
                "project_json_parse": project_parse,
                "schema_valid": schema_valid,
                "required_field_coverage": required_coverage,
                "usage": dict(last_record.get("usage") or {}),
                "prompt_sha256": prompt_sha256,
                "prompt_chars": prompt_chars,
            }
            rows.append(row)
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            print(
                f"run={index}/{run_count} elapsed_ms={elapsed_ms} "
                f"attempts={row['physical_attempts']} empty={row['empty_content']} "
                f"json={row['strict_json_object']} schema={row['schema_valid']} "
                f"finish={row['finish_reason'] or '<none>'} "
                f"error={row['error_type'] or '<none>'}",
                flush=True,
            )
    latencies = [int(row["elapsed_ms"]) for row in rows]
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": f"{OFFICIAL_BASE_URL}/chat/completions",
        "model": config.action_model,
        "runs": len(rows),
        "prompt_sha256": prompt_sha256,
        "prompt_chars": prompt_chars,
        "max_tokens": int(request["max_tokens"]),
        "thinking": "disabled",
        "logical_success_count": sum(
            1 for row in rows if row["logical_success"]
        ),
        "empty_content_count": sum(1 for row in rows if row["empty_content"]),
        "strict_json_object_count": sum(
            1 for row in rows if row["strict_json_object"]
        ),
        "schema_valid_count": sum(1 for row in rows if row["schema_valid"]),
        "recovered_count": sum(1 for row in rows if row["recovered"]),
        "physical_attempt_count": sum(
            int(row["physical_attempts"]) for row in rows
        ),
        "reasoning_nonempty_count": sum(
            1 for row in rows if int(row["reasoning_chars"]) > 0
        ),
        "finish_reason_counts": {
            reason: sum(1 for row in rows if row["finish_reason"] == reason)
            for reason in sorted({str(row["finish_reason"]) for row in rows})
        },
        "latency_ms": {
            "min": min(latencies) if latencies else 0,
            "mean": round(mean(latencies), 1) if latencies else 0,
            "median": round(median(latencies), 1) if latencies else 0,
            "p90": percentile(latencies, 0.9),
            "max": max(latencies) if latencies else 0,
        },
        "completion_tokens_total": sum(
            int(dict(row["usage"]).get("completion_tokens") or 0)
            for row in rows
        ),
        "reasoning_tokens_total": sum(
            int(dict(row["usage"]).get("reasoning_tokens") or 0)
            for row in rows
        ),
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(summary_path, 0o600)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe hardened DeepSeek JSON session preparation safely."
    )
    parser.add_argument(
        "--dotenv",
        type=Path,
        default=Path.home() / ".fu-gm" / "fu_gm.env",
    )
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("artifacts"),
    )
    parser.add_argument("--preflight-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    os.umask(0o077)
    args = parse_args()
    if args.runs < 1 or args.runs > 100:
        raise ValueError("--runs must be between 1 and 100")
    values = read_dotenv(args.dotenv)
    config = provider_config(values)
    request = capture_current_request(config.action_model)
    wire = preflight_wire_payload(config, request)
    messages = list(request["messages"])
    prompt_sha256, prompt_chars = prompt_fingerprint(messages)
    print(
        "preflight "
        f"endpoint={wire['endpoint']} model={wire['model']} "
        f"thinking={wire['thinking']['type']} max_tokens={wire['max_tokens']} "
        f"messages={len(messages)} prompt_chars={prompt_chars} "
        f"prompt_sha256={prompt_sha256}",
        flush=True,
    )
    if args.preflight_only:
        return 0
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = args.output_root / f"deepseek_session_prep_probe_{timestamp}"
    summary = run_probe(
        config=config,
        request=request,
        run_count=args.runs,
        output_dir=output_dir,
    )
    print(
        f"summary={output_dir / 'summary.json'} "
        f"success={summary['logical_success_count']}/{summary['runs']} "
        f"empty={summary['empty_content_count']} "
        f"schema_valid={summary['schema_valid_count']} "
        f"median_ms={summary['latency_ms']['median']} "
        f"p90_ms={summary['latency_ms']['p90']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
