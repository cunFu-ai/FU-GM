from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median
from typing import Iterator
from urllib.parse import urlparse

from fu_gm.config import parse_api_base_urls, resolve_model_api_key
from fu_gm.http_server import FUGMHttpService
from fu_gm.testing.conversation_quality import ConversationQualityAuditor
from fu_gm.testing.kariba_fixture import (
    KARIBA_HEROES,
    KaribaReplayMessage,
    kariba_opening_probe_messages,
    seed_kariba_ready_campaign,
)


@dataclass(frozen=True)
class ModelProviderSpec:
    name: str
    model: str
    api_base_url: str
    api_key: str = field(repr=False)
    response_format_enabled: bool = True
    backup_api_base_urls: tuple[str, ...] = ()

    @property
    def endpoint_host(self) -> str:
        return urlparse(self.api_base_url).netloc or self.api_base_url


@dataclass
class ModelProbeTurn:
    index: int
    speaker: str
    message: str
    expected: str
    status: int
    elapsed_ms: int
    target: str
    route: str
    send_reply: bool
    reply: str
    agent_error: str = ""
    model_call_count: int = 0
    successful_model_call_count: int = 0
    failed_model_call_count: int = 0
    trace_decisions: list[str] = field(default_factory=list)
    receipts: list[dict[str, object]] = field(default_factory=list)


def load_provider_from_dotenv(
    path: str | Path,
    *,
    name: str,
    model: str,
    base_url_key: str = "FU_GM_API_BASE_URL",
) -> ModelProviderSpec:
    values = _read_dotenv(path)
    base_url = str(values.get(base_url_key) or "").strip().rstrip("/")
    primary_url = str(values.get("FU_GM_API_BASE_URL") or "").strip().rstrip("/")
    configured_backups = parse_api_base_urls(
        str(
            values.get("FU_GM_BACKUP_API_BASE_URLS")
            or values.get("FU_GM_BACKUP_API_BASE_URL")
            or ""
        )
    )
    selected_model = str(model or values.get("FU_GM_ACTION_MODEL") or "").strip()
    api_key = resolve_model_api_key(
        selected_model,
        str(values.get("FU_GM_API_KEY") or "").strip(),
        values=values,
    )
    if not base_url:
        raise ValueError(f"{name} 配置缺少 {base_url_key}：{path}")
    if not api_key:
        raise ValueError(f"{name} 配置缺少 FU_GM_API_KEY：{path}")
    return ModelProviderSpec(
        name=name,
        model=selected_model,
        api_base_url=base_url,
        api_key=api_key,
        response_format_enabled=_env_flag(
            values.get("FU_GM_RESPONSE_FORMAT_ENABLED", "1")
        ),
        backup_api_base_urls=(
            tuple(url for url in (primary_url,) if url and url != base_url)
            if base_url_key == "FU_GM_BACKUP_API_BASE_URL"
            else tuple(url for url in configured_backups if url != base_url)
        ),
    )


def run_kariba_provider_probe(
    spec: ModelProviderSpec,
    *,
    output_root: str | Path,
    messages: list[KaribaReplayMessage] | None = None,
) -> dict[str, object]:
    run_root = Path(output_root) / _safe_segment(spec.name)
    run_root.mkdir(parents=True, exist_ok=True)
    campaign_id = f"kariba_model_probe_{_safe_segment(spec.name)}"
    session_id = "chapter-one-opening"
    channel_id = "model-probe-group"
    turns: list[ModelProbeTurn] = []
    with _provider_environment(spec):
        service = FUGMHttpService(
            data_root=run_root / "campaigns",
            use_llm=True,
        )
        runtime = seed_kariba_ready_campaign(
            service,
            campaign_id=campaign_id,
            session_id=session_id,
            channel_id=channel_id,
        )
        attempted_windows: set[str] = set()

        def send(item: KaribaReplayMessage) -> None:
            index = len(turns) + 1
            payload: dict[str, object] = {
                "campaign_id": campaign_id,
                "session_id": session_id,
                "channel_id": channel_id,
                "speaker": item.speaker,
                "speaker_id": "player-noel" if item.speaker == "测试玩家甲" else "player-elinie",
                "message": item.text,
                "message_id": f"{_safe_segment(spec.name)}-{index}",
                "is_at_bot": item.addressed,
                "is_reply_to_bot": item.reply_to_gm,
            }
            if item.quoted_text:
                payload["quoted_message"] = {
                    "message_id": "kariba-invitation",
                    "sender_id": "gm-shiyou",
                    "text": item.quoted_text,
                    "source": "astrbot",
                }
            client = service.gm_agent_runtime.llm_client
            call_start = len(client.recent_calls) if client is not None else 0
            started = time.perf_counter()
            status, raw = service.handle("POST", "/v1/message/route", payload)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            body = raw if isinstance(raw, dict) else {"reply": str(raw)}
            recent_calls = (
                list(client.recent_calls[call_start:]) if client is not None else []
            )
            successful_calls = sum(
                1
                for call in recent_calls
                if bool(call.get("ok")) and not bool(call.get("response_empty"))
            )
            failed_calls = len(recent_calls) - successful_calls
            receipts = [
                dict(receipt)
                for receipt in list(body.get("tool_receipts") or [])
                if isinstance(receipt, dict)
            ]
            turns.append(
                ModelProbeTurn(
                    index=index,
                    speaker=item.speaker,
                    message=item.text,
                    expected=item.expectation,
                    status=status,
                    elapsed_ms=elapsed_ms,
                    target=str(body.get("target") or ""),
                    route=str(body.get("route") or ""),
                    send_reply=bool(body.get("send_reply")),
                    reply=str(body.get("reply") or ""),
                    agent_error=str(body.get("agent_error") or ""),
                    model_call_count=len(recent_calls),
                    successful_model_call_count=successful_calls,
                    failed_model_call_count=failed_calls,
                    trace_decisions=[
                        str(step.get("decision") or step.get("phase") or "")
                        for step in list(body.get("agent_trace") or [])
                        if isinstance(step, dict)
                    ],
                    receipts=receipts,
                )
            )

        for item in messages or kariba_opening_probe_messages():
            send(item)
            for _ in range(6):
                followup = _next_player_window_followup(
                    runtime,
                    attempted_window_ids=attempted_windows,
                )
                if followup is None:
                    break
                attempted_windows.add(followup[0])
                send(followup[1])

        frame = runtime.app.scene_frame_manager.current_frame
        brief = dict(getattr(frame, "working_brief", {}) or {})
        gate = service.session_gates.get(campaign_id, channel_id, session_id)
        scene = runtime.app.scene_manager.current_scene
        unresolved_blocking = runtime.app.interceptor.decision_window_manager.pending(
            blocking_only=True
        )
        equipment_access_state = _equipment_access_state(runtime)
        result = _probe_result(
            spec,
            turns,
            gate_status=gate.status,
            scene_name=str(scene.name if scene is not None else ""),
            working_brief=brief,
            unresolved_blocking_count=len(unresolved_blocking),
            equipment_access_state=equipment_access_state,
        )

    (run_root / "probe.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_root / "conversation.txt").write_text(
        _render_conversation(spec, turns),
        encoding="utf-8",
    )
    return result


def compare_probe_results(results: list[dict[str, object]]) -> dict[str, object]:
    ranking = sorted(
        results,
        key=lambda item: (
            not bool(item.get("provider_available")),
            -float(
                item.get("behavior_quality_score")
                or item.get("quality_score")
                or 0.0
            ),
            -float(item.get("end_to_end_score") or 0.0),
            int(item.get("p50_latency_ms") or 0),
        ),
    )
    available = [item for item in ranking if bool(item.get("provider_available"))]
    return {
        "providers": results,
        "ranking": [str(item.get("provider") or "") for item in ranking],
        "recommendation": (
            f"本轮综合表现较好的是 {available[0].get('provider')}。"
            if available
            else "没有模型完成可比较的核心 GM 调用。"
        ),
    }


def _equipment_access_state(runtime) -> dict[str, dict[str, object]]:
    state: dict[str, dict[str, object]] = {}
    for hero_name in KARIBA_HEROES:
        if not runtime.app.character_manager.exists(hero_name):
            continue
        character = runtime.app.character_manager.get(hero_name)
        state[hero_name] = {
            "unavailable": sorted(character.unavailable_equipment),
            "equipped_main_hand": character.equipped_main_hand,
            "equipped_off_hand": character.equipped_off_hand,
        }
    return state


def _probe_result(
    spec: ModelProviderSpec,
    turns: list[ModelProbeTurn],
    *,
    gate_status: str,
    scene_name: str,
    working_brief: dict[str, object],
    unresolved_blocking_count: int = 0,
    equipment_access_state: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    expected_silence = [turn for turn in turns if turn.expected == "silent"]
    expected_reply = [turn for turn in turns if turn.expected != "silent"]
    silent_state_writes = sum(
        1
        for turn in expected_silence
        if any(
            receipt.get("ok") is True
            and receipt.get("state_changed") is True
            for receipt in turn.receipts
        )
    )
    silence_correct = sum(
        1
        for turn in expected_silence
        if (
            not turn.send_reply or turn.target not in {"fu_gm", "gm"}
        )
        and not any(
            receipt.get("ok") is True
            and receipt.get("state_changed") is True
            for receipt in turn.receipts
        )
    )
    reply_correct = sum(
        1
        for turn in expected_reply
        if turn.send_reply and bool(turn.reply.strip())
    )
    receipts = [receipt for turn in turns for receipt in turn.receipts]
    total_model_calls = sum(turn.model_call_count for turn in turns)
    successful_model_calls = sum(
        turn.successful_model_call_count for turn in turns
    )
    failed_model_calls = sum(turn.failed_model_call_count for turn in turns)
    unavailable_turns = [
        turn
        for turn in turns
        if turn.route.startswith("gm_agent_unavailable")
        or bool(turn.agent_error.strip())
    ]
    provider_available = bool(successful_model_calls) and any(
        turn.route in {"gm_agent_tool", "gm_agent_reply", "gm_agent_silent"}
        and not turn.agent_error.strip()
        for turn in turns
    )
    availability_error = ""
    if not total_model_calls:
        availability_error = "核心 GM 未发起任何模型调用。"
    elif not successful_model_calls:
        availability_error = "核心 GM 的模型调用全部失败或返回空正文。"
    elif unavailable_turns:
        availability_error = "至少一个核心 GM 回合进入不可用降级。"
    successful_writes = sum(
        1
        for receipt in receipts
        if receipt.get("ok") is True and receipt.get("state_changed") is True
    )
    failed_receipts = sum(1 for receipt in receipts if receipt.get("ok") is False)
    opening_turn = next(
        (
            turn
            for turn in turns
            if any(
                receipt.get("tool_name") == "start_scene"
                and receipt.get("ok") is True
                for receipt in turn.receipts
            )
        ),
        None,
    )
    opening = opening_turn.reply if opening_turn is not None else ""
    opening_handoff = bool(re.search(r"[？?]|(?:怎么做|做什么|先动)", opening))
    opening_contract_adherence = bool(
        re.search(r"卡里巴|监狱|牢房|越狱", f"{scene_name}\n{opening}")
    )
    backstage_tokens = (
        "private_situation",
        "public_opening",
        "player_handoff",
        "场景框架",
        "互动焦点",
        "当前目标",
    )
    backstage_leaks = sum(
        1
        for turn in turns
        if any(token in turn.reply for token in backstage_tokens)
    )
    public_rows = [
        {
            "label": f"probe-{turn.index}",
            "message": turn.message,
            "reply": turn.reply,
            "elapsed_ms": turn.elapsed_ms,
            "expected_send_reply": turn.expected != "silent",
            "body": {
                "tool_receipts": turn.receipts,
                "route": turn.route,
                "agent_error": turn.agent_error,
            },
        }
        for turn in turns
    ]
    quality = ConversationQualityAuditor().audit(public_rows).as_dict()
    source_events = [
        item
        for item in list(working_brief.get("source_events") or [])
        if isinstance(item, dict)
    ]
    known_messages = {(turn.speaker, turn.message) for turn in turns}
    source_binding_errors = sum(
        1
        for item in source_events
        if (str(item.get("speaker") or ""), str(item.get("text") or ""))
        not in known_messages
    )
    latencies = sorted(turn.elapsed_ms for turn in turns)
    infrastructure_checks = {
        "provider_available": provider_available,
        "no_unavailable_turns": not unavailable_turns,
        "no_failed_model_calls": failed_model_calls == 0,
    }
    behavior_checks = {
        "entered_adventure": gate_status == "adventure",
        "scene_started": bool(scene_name),
        "opening_handoff": opening_handoff,
        "opening_contract_adherence": opening_contract_adherence,
        "reply_accuracy": reply_correct == len(expected_reply),
        "silence_accuracy": silence_correct == len(expected_silence),
        "no_state_writes_on_expected_silence": silent_state_writes == 0,
        "no_backstage_leaks": backstage_leaks == 0,
        "source_binding_correct": source_binding_errors == 0,
        "no_failed_tool_receipts": failed_receipts == 0,
        "no_unresolved_blocking_windows": unresolved_blocking_count == 0,
    }
    if equipment_access_state is not None:
        expected_prison_weapons = {
            "诺艾尔": {"钢匕首", "细剑"},
            "艾丽妮": {"法杖", "魔典"},
        }
        behavior_checks["opening_equipment_access_synced"] = all(
            expected.issubset(
                set(equipment_access_state.get(hero_name, {}).get("unavailable", []))
            )
            and equipment_access_state.get(hero_name, {}).get(
                "equipped_main_hand"
            )
            not in expected
            and equipment_access_state.get(hero_name, {}).get(
                "equipped_off_hand"
            )
            not in expected
            for hero_name, expected in expected_prison_weapons.items()
        )
    behavior_quality_score = (
        round(
            100.0
            * sum(bool(value) for value in behavior_checks.values())
            / len(behavior_checks),
            1,
        )
        if provider_available
        else 0.0
    )
    infrastructure_score = round(
        100.0
        * sum(bool(value) for value in infrastructure_checks.values())
        / len(infrastructure_checks),
        1,
    )
    end_to_end_checks = {**infrastructure_checks, **behavior_checks}
    end_to_end_score = round(
        100.0
        * sum(bool(value) for value in end_to_end_checks.values())
        / len(end_to_end_checks),
        1,
    )
    return {
        "provider": spec.name,
        "model": spec.model,
        "endpoint_host": spec.endpoint_host,
        "turn_count": len(turns),
        "http_success_count": sum(1 for turn in turns if turn.status < 400),
        "provider_available": provider_available,
        "availability_error": availability_error,
        "model_call_count": total_model_calls,
        "successful_model_call_count": successful_model_calls,
        "failed_model_call_count": failed_model_calls,
        "unavailable_turn_count": len(unavailable_turns),
        "gate_status": gate_status,
        "scene_name": scene_name,
        "opening_handoff": opening_handoff,
        "opening_contract_adherence": opening_contract_adherence,
        "expected_reply_accuracy": (
            reply_correct / len(expected_reply) if expected_reply else 1.0
        ),
        "expected_silence_accuracy": (
            silence_correct / len(expected_silence) if expected_silence else 1.0
        ),
        "successful_state_writes": successful_writes,
        "failed_tool_receipts": failed_receipts,
        "backstage_leaks": backstage_leaks,
        "source_binding_errors": source_binding_errors,
        "unresolved_blocking_windows": unresolved_blocking_count,
        "silent_state_writes": silent_state_writes,
        "equipment_access_state": equipment_access_state or {},
        "p50_latency_ms": int(median(latencies)) if latencies else 0,
        "p95_latency_ms": _percentile(latencies, 0.95),
        "max_latency_ms": max(latencies, default=0),
        # Keep the legacy field for report consumers while making it explicit
        # that prose/tool behavior and provider transport health are different
        # measurements. A transient 502 must not masquerade as a GM choosing
        # silence, nor should good prose hide an unusable endpoint.
        "quality_score": behavior_quality_score,
        "behavior_quality_score": behavior_quality_score,
        "infrastructure_score": infrastructure_score,
        "end_to_end_score": end_to_end_score,
        "quality_checks": behavior_checks,
        "behavior_quality_checks": behavior_checks,
        "infrastructure_checks": infrastructure_checks,
        "conversation_quality": quality,
        "turns": [asdict(turn) for turn in turns],
    }


def _next_player_window_followup(
    runtime,
    *,
    attempted_window_ids: set[str],
) -> tuple[str, KaribaReplayMessage] | None:
    """Let the simulated player answer a real pending rules question.

    This is intentionally state-driven. A static transcript that ignores a
    check or reroll prompt measures deadlock tolerance rather than GM quality.
    Unsupported high-consequence choices remain untouched instead of being
    decided by an omniscient test player.
    """

    decisions = runtime.app.interceptor.decision_window_manager
    for window in decisions.awaiting_player_response():
        if window.window_id in attempted_window_ids:
            continue
        speaker = _speaker_for_hero(runtime, window.owner)
        if not speaker:
            continue
        if window.kind == "check_roll_confirmation":
            return window.window_id, KaribaReplayMessage(
                speaker=speaker,
                text="投。",
                expectation="rule_window",
                reply_to_gm=True,
            )
        if window.kind in {"trait_invocation", "bond_invocation"}:
            # Failed-check invocation is a silent player option.  A public-test
            # player cannot inspect this backend window and should not emit a
            # mechanical accept/decline message merely to close it.
            continue
        if (
            window.kind == "skill_judgement"
            and str(window.payload.get("label") or "") == "幸运七"
        ):
            return window.window_id, KaribaReplayMessage(
                speaker=speaker,
                text="我接受这次检定结果，不重掷。",
                expectation="rule_window",
                reply_to_gm=True,
            )
        if window.kind == "critical_opportunity":
            return window.window_id, KaribaReplayMessage(
                speaker=speaker,
                text=(
                    f"我把这次大成功的机会用于【优势】，目标是【{window.owner}】。"
                ),
                expectation="rule_window",
                reply_to_gm=True,
            )
        if window.kind == "opportunity_parameter":
            return window.window_id, KaribaReplayMessage(
                speaker=speaker,
                text=f"这次机会的目标是【{window.owner}】。",
                expectation="rule_window",
                reply_to_gm=True,
            )
    return None


def _speaker_for_hero(runtime, hero_name: str) -> str:
    clean_hero = str(hero_name or "").strip()
    for key, draft in runtime.app.world_state.world_profile.hero_drafts.items():
        if str(draft.hero_name or "").strip() != clean_hero:
            continue
        return str(draft.player_name or key or "").strip()
    return ""


@contextmanager
def _provider_environment(
    spec: ModelProviderSpec,
    *,
    include_backups: bool = False,
) -> Iterator[None]:
    # Offline comparison prioritizes a complete semantic/tool transaction over
    # realtime latency. Both providers receive the same wide budget so a model
    # is not scored down merely because its multi-tool opening crossed the
    # production chat deadline. Production defaults are not changed here.
    transaction_timeout = "900"
    endpoint_timeout = "360"
    overrides = {
        "FU_GM_API_BASE_URL": spec.api_base_url,
        "FU_GM_API_KEY": spec.api_key,
        "FU_GM_ACTION_MODEL": spec.model,
        "FU_GM_EXPRESSOR_MODEL": spec.model,
        "FU_GM_ROUTER_MODEL": spec.model,
        "FU_GM_SESSION_ZERO_MODEL": spec.model,
        "FU_GM_CORE_GM_MODEL": spec.model,
        "FU_GM_TOOL_AGENT_MODEL": spec.model,
        "FU_GM_TOOL_PROTOCOL_REPAIR_MODEL": spec.model,
        "FU_GM_RESPONSE_FORMAT_ENABLED": (
            "1" if spec.response_format_enabled else "0"
        ),
        "FU_GM_CORE_GM_RESPONSE_FORMAT_ENABLED": (
            "1" if spec.response_format_enabled else "0"
        ),
        "FU_GM_TIMEOUT_SECONDS": transaction_timeout,
        "FU_GM_ENDPOINT_ATTEMPT_TIMEOUT_SECONDS": endpoint_timeout,
        "FU_GM_CORE_GM_TIMEOUT_SECONDS": transaction_timeout,
        "FU_GM_CORE_GM_ENDPOINT_ATTEMPT_TIMEOUT_SECONDS": endpoint_timeout,
        "FU_GM_TOOL_AGENT_TIMEOUT_SECONDS": transaction_timeout,
        "FU_GM_TOOL_AGENT_MAX_TOKENS": "8192",
        "FU_GM_EXPRESSOR_API_BASE_URL": spec.api_base_url,
        "FU_GM_EXPRESSOR_API_KEY": spec.api_key,
        "FU_GM_BACKUP_API_BASE_URL": (
            spec.backup_api_base_urls[0]
            if include_backups and spec.backup_api_base_urls
            else ""
        ),
        "FU_GM_BACKUP_API_BASE_URLS": (
            ",".join(spec.backup_api_base_urls) if include_backups else ""
        ),
        "FU_GM_ACTION_BACKUP_API_BASE_URL": "",
        "FU_GM_EXPRESSOR_BACKUP_API_BASE_URL": "",
        # This probe compares model behavior over a compact scripted exchange.
        # Production keeps its circuit breaker, but carrying one transient
        # gateway failure into several immediately-following probe messages
        # would score transport cooldown as intentional GM silence.
        "FU_GM_CORE_GM_CIRCUIT_BREAKER_ENABLED": "0",
        "FU_GM_CORE_GM_RECOVERY_MAX_RETRIES": "1",
        "FU_GM_IMAGE_ENABLED": "0",
    }
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _read_dotenv(path: str | Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _env_flag(value: object) -> bool:
    return str(value or "").strip().lower() not in {
        "0",
        "false",
        "no",
        "disabled",
        "off",
    }


def _render_conversation(
    spec: ModelProviderSpec,
    turns: list[ModelProbeTurn],
) -> str:
    lines = [
        f"FU-GM 卡里巴村模型探针：{spec.name}",
        f"model: {spec.model}",
        f"endpoint: {spec.endpoint_host}",
        "",
    ]
    for turn in turns:
        lines.extend(
            [
                f"--- {turn.index} | {turn.elapsed_ms}ms | expected={turn.expected} ---",
                (
                    f"route={turn.route or '<none>'}; model_calls={turn.model_call_count}; "
                    f"successful_calls={turn.successful_model_call_count}"
                ),
                f"{turn.speaker}: {turn.message}",
                f"时悠: {turn.reply}" if turn.reply else "时悠: <静默>",
                (
                    f"agent_error: {turn.agent_error[:500]}"
                    if turn.agent_error
                    else ""
                ),
                "",
            ]
        )
    return "\n".join(line for line in lines if line != "") + "\n"


def _percentile(values: list[int], ratio: float) -> int:
    if not values:
        return 0
    index = max(0, min(len(values) - 1, int(round((len(values) - 1) * ratio))))
    return int(values[index])


def _safe_segment(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return clean or "provider"


__all__ = [
    "ModelProviderSpec",
    "compare_probe_results",
    "load_provider_from_dotenv",
    "run_kariba_provider_probe",
]
