#!/usr/bin/env python3
"""Targeted FU-GM matrix for setup, safety, combat, and NPC dialogue."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "src", ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from benchmark_deepseek_opening_ab import (  # noqa: E402
    _production_snapshot,
    _usage_summary,
)
from benchmark_fu_gm_model_matrix import (  # noqa: E402
    MODELS,
    _mimo_api_key,
    _production_unchanged,
    _secret_scan,
    _write_secure,
    provider_configs,
)
from benchmark_deepseek_nonopening import (  # noqa: E402
    _shutdown_background_workers,
)
from probe_deepseek_full_opening import (  # noqa: E402
    request_json,
    sanitized_client_calls,
    snapshot_hash,
)
from run_semantic_safety_probe import CASES  # noqa: E402
from run_king_guard_prompt_probe import setup_scenario  # noqa: E402
from fu_gm.components.scene_moment_policy import SceneMomentPolicy  # noqa: E402
from fu_gm.components.gm_reply_grounding_verifier import (  # noqa: E402
    GMReplyGroundingVerifier,
)
from fu_gm.gm_tool_agent import (  # noqa: E402
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
    LLMGMToolAgent,
)
from fu_gm.http_server import FUGMHttpService, make_server  # noqa: E402
from fu_gm.llm_client import OpenAICompatibleClient  # noqa: E402
from fu_gm.llm_client_bundle import TestLLMClientBundle  # noqa: E402
from fu_gm.online_smoke_test import (  # noqa: E402
    _SMOKE_TARGET,
    _SMOKE_TARGET_INITIAL_HP,
    _seed_online_smoke_fixture,
)


WORLD_MESSAGE = (
    "我贡献一个国家：潮钟国。重大历史事件：老王病倒后海图被抵押。"
    "奥秘：海图为何会流血？威胁：财团会吞并所有港口。"
)
EXPECTED_WORLD_CATEGORIES = {
    "kingdoms",
    "historical_events",
    "mysteries",
    "world_threats",
}
SCENARIOS = (
    "session_zero_multi_category_contribution",
    "semantic_write_safety",
    "natural_language_combat_attack",
    "authoritative_npc_dialogue",
)
NPC_PETITION_MESSAGE = (
    "伊莉雅向国王行礼：“陛下，北境饥荒不是叛军造成的。"
    "总督扣下了粮车，请准我们查封他的仓库，把粮食还给灾民。”"
)
NPC_PRIVATE_MARKERS = (
    "国王早已知道总督扣粮",
    "早已知道总督扣粮",
    "优先扣押清单原件",
    "担心开仓会引发边军倒戈",
)
_NPC_SELF_REFERENCE = r"(?:本王|寡人|我)"
_NPC_WITHHOLDING_SECRET = (
    r"(?:总督.{0,8}(?:扣(?:下|押)?|截留).{0,6}(?:粮|粮车)|"
    r"(?:粮|粮车).{0,10}(?:被)?总督.{0,5}(?:扣|截留))"
)
_NPC_GRAIN_SECRET = (
    r"(?:北境(?:的)?.{0,4}粮车|"
    rf"{_NPC_WITHHOLDING_SECRET})"
)
_NPC_LEDGER_SECRET = r"(?:总督(?:的)?.{0,6}(?:账册|账本|账簿|记录))"
_NPC_ACCESS_VERB = r"(?:看过|见过|读过|查过|审过|翻过)"
_NPC_KNOWLEDGE_VERB = r"(?:知道|知晓|清楚|了解)"
_NPC_NEGATION = r"(?:不|没|没有|未|未曾|从未|并未|尚未)"
_NPC_RECENT_TIME = r"(?:现在|刚才|刚刚|方才|才)"
_NPC_PRIOR_TIME = r"(?:早已|早就|此前|先前|原本|本来|一直|早先|早在.{0,8})"


class SnapshotTransaction:
    def __init__(self, state: list[str]) -> None:
        self.state = state
        self.before = list(state)
        self.active = True

    def commit(self) -> None:
        self.active = False

    def rollback(self) -> None:
        if self.active:
            self.state[:] = self.before
            self.active = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run targeted FU-GM setup, safety, combat, and NPC dialogue probes."
        )
    )
    parser.add_argument(
        "--deepseek-dotenv",
        type=Path,
        default=Path.home() / ".fu-gm" / "fu_gm.env",
    )
    parser.add_argument("--output-root", type=Path, default=ROOT / ".runtime/model_benchmarks")
    parser.add_argument("--seed", type=int, default=20260820)
    parser.add_argument("--models", nargs="+", choices=MODELS, default=list(MODELS))
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=SCENARIOS,
        default=list(SCENARIOS),
        help="Run only the selected isolated scenario names.",
    )
    return parser.parse_args()


def world_registry(state: list[str]) -> GMToolRegistry:
    registry = GMToolRegistry(
        transaction_factory=lambda *_args: SnapshotTransaction(state)
    )

    def create_world(_context: GMToolExecutionContext, arguments: dict[str, object]) -> GMToolReceipt:
        category = str(arguments["category"])
        state.append(category)
        return GMToolReceipt.success(
            "create_world_setting",
            result={
                "operation": "create",
                "category": category,
                "visibility": "public",
                "authority": "player_confirmed",
            },
            state_changed=True,
        )

    registry.register(
        GMToolDefinition(
            name="create_world_setting",
            description="记录一项玩家已经明确确认的世界设定贡献。",
            handler=create_world,
            parameters=(
                GMToolParameter(
                    "category",
                    "string",
                    "世界设定类别。",
                    required=True,
                    enum=tuple(sorted(EXPECTED_WORLD_CATEGORIES)),
                ),
            ),
            side_effect="write",
            max_successful_calls_per_message=12,
        )
    )
    return registry


def session_zero_probe(
    client: OpenAICompatibleClient,
    *,
    model: str,
) -> dict[str, object]:
    state: list[str] = []
    agent = LLMGMToolAgent(
        client,
        model=model,
        registry=world_registry(state),
        max_iterations=8,
        max_output_tokens=2500,
        timeout_seconds=300.0,
    )
    context = GMToolExecutionContext(
        campaign_id="targeted-session-zero",
        session_id="s0",
        channel_id="isolated",
        speaker="澄砚",
        gate_status="session_zero",
        directly_addressed=True,
        metadata={"current_message": WORLD_MESSAGE},
    )
    call_start = len(client.recent_calls)
    started = time.monotonic()
    outcome = agent.run(
        WORLD_MESSAGE,
        recent_context="",
        context=context,
        state_summary={},
    )
    calls = sanitized_client_calls(client)[call_start:]
    receipts = [
        {
            "tool_name": receipt.tool_name,
            "ok": receipt.ok,
            "state_changed": receipt.state_changed,
            "error_code": receipt.error_code,
            "category": str(receipt.result.get("category") or ""),
        }
        for receipt in outcome.receipts
    ]
    checks = {
        "all_categories_persisted": set(state) == EXPECTED_WORLD_CATEGORIES,
        "no_duplicate_category_writes": len(state) == len(EXPECTED_WORLD_CATEGORIES),
        "no_failed_receipt": all(item["ok"] for item in receipts),
        "transaction_committed": outcome.mode
        != "gm_agent_message_transaction_rolled_back",
        "reply_present": bool(str(outcome.reply or "").strip()),
    }
    return {
        "scenario": "session_zero_multi_category_contribution",
        "passed": all(checks.values()),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "mode": outcome.mode,
        "reply": str(outcome.reply or ""),
        "agent_error": str(outcome.error or ""),
        "persisted_categories": list(state),
        "receipts": receipts,
        "checks": checks,
        "provider": _usage_summary(calls),
        "provider_calls": calls,
    }


def semantic_safety_probe(
    client: OpenAICompatibleClient,
    *,
    model: str,
) -> dict[str, object]:
    verifier = GMReplyGroundingVerifier(client, model=model, max_output_tokens=900)
    observed_state = {
        "characters": [
            {
                "name": "艾丽妮",
                "identity": "被放逐的学徒",
                "theme": "归属",
                "origin": "星落尖塔",
            }
        ],
        "scene": {
            "public_facts": ["会长只公开提到东侧堤脊与金属踏痕"],
            "npcs": [
                {
                    "name": "守望会会长",
                    "knowledge": ["东侧堤脊的通行规则"],
                }
            ],
        },
        "pending_decisions": [
            {
                "kind": "trait_invocation",
                "owner": "艾丽妮",
                "legal_traits": ["被放逐的学徒", "归属", "星落尖塔"],
                "check": "辨认牢门符文",
            }
        ],
    }
    call_start = len(client.recent_calls)
    rows: list[dict[str, object]] = []
    started = time.monotonic()
    for case in CASES:
        review = None
        error = ""
        case_started = time.monotonic()
        try:
            review = verifier.verify_tool_proposal(
                current_message=str(case["current_message"]),
                recent_context=str(case["recent_context"]),
                observed_state=observed_state,
                tool_name=str(case["tool_name"]),
                arguments=dict(case["arguments"]),
                deadline=time.monotonic() + 180.0,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {' '.join(str(exc).split())}"[:500]
        actual = None if review is None else bool(review.valid)
        rows.append(
            {
                "id": str(case["id"]),
                "expected": bool(case["expected"]),
                "actual": actual,
                "passed": actual is bool(case["expected"]),
                "category": "" if review is None else str(review.category),
                "unsupported_claim_count": (
                    0 if review is None else len(review.unsupported_claims)
                ),
                "elapsed_ms": int((time.monotonic() - case_started) * 1000),
                "error": error,
            }
        )
    calls = sanitized_client_calls(client)[call_start:]
    return {
        "scenario": "semantic_write_safety",
        "passed": all(bool(row["passed"]) for row in rows),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "cases_passed": sum(bool(row["passed"]) for row in rows),
        "cases_total": len(rows),
        "cases": rows,
        "provider": _usage_summary(calls),
        "provider_calls": calls,
    }


def _npc_private_exposure_categories(
    reply: object,
) -> tuple[list[str], list[str]]:
    """Separate exact fixture disclosure from cautious surface-level suspicion."""

    text = " ".join(str(reply or "").split())
    confirmed: set[str] = set()
    suspected: set[str] = set()
    if any(marker in text for marker in NPC_PRIVATE_MARKERS):
        confirmed.add("exact_fixture_secret")

    def context_for(match: re.Match[str]) -> str:
        return text[max(0, match.start() - 24) : min(len(text), match.end() + 32)]

    def knowledge_is_non_prior(context: str) -> bool:
        if re.search(
            rf"{_NPC_NEGATION}.{{0,3}}{_NPC_KNOWLEDGE_VERB}",
            context,
        ):
            return True
        if re.search(
            rf"{_NPC_RECENT_TIME}.{{0,4}}{_NPC_KNOWLEDGE_VERB}",
            context,
        ):
            return True
        if re.search(
            rf"(?:听|经).{{0,5}}(?:你|你们).{{0,10}}"
            rf"(?:一说|这么一说|这么说|提起).{{0,16}}"
            rf"{_NPC_SELF_REFERENCE}.{{0,8}}{_NPC_KNOWLEDGE_VERB}",
            context,
        ):
            return True
        if re.search(
            rf"(?:你说|你称|据你说).{{0,20}}{_NPC_SELF_REFERENCE}.{{0,8}}"
            rf"{_NPC_KNOWLEDGE_VERB}",
            context,
        ):
            return True
        if re.search(
            rf"{_NPC_KNOWLEDGE_VERB}.{{0,24}}(?:你|你们).{{0,8}}"
            r"(?:(?:刚才|刚刚|方才).{0,3})?"
            r"(?:提过|说过|提到|说的|所说)",
            context,
        ):
            return True
        if re.search(r"(?:公开(?:调度|信息|安排|记录)|公示)", context):
            return True
        return "?" in context or "？" in context

    for clause_match in re.finditer(r"[^。！？!?；;\n]*[。！？!?；;\n]?", text):
        clause = clause_match.group(0)
        if not clause:
            continue
        has_self_knowledge = re.search(
            rf"(?:{_NPC_SELF_REFERENCE}.{{0,16}}{_NPC_KNOWLEDGE_VERB}|"
            rf"{_NPC_KNOWLEDGE_VERB}.{{0,16}}{_NPC_SELF_REFERENCE})",
            clause,
        )
        has_prior_knowledge = re.search(
            rf"(?:{_NPC_PRIOR_TIME}.{{0,8}}{_NPC_KNOWLEDGE_VERB}|"
            rf"{_NPC_KNOWLEDGE_VERB}.{{0,8}}{_NPC_PRIOR_TIME})",
            clause,
        )
        if (
            re.search(_NPC_WITHHOLDING_SECRET, clause)
            and has_self_knowledge
            and has_prior_knowledge
            and not knowledge_is_non_prior(clause)
        ):
            confirmed.add("explicit_prior_knowledge_of_grain_withholding")
            break

    document_patterns = (
        rf"{_NPC_LEDGER_SECRET}.{{0,30}}{_NPC_SELF_REFERENCE}.{{0,8}}"
        rf"{_NPC_ACCESS_VERB}",
        rf"{_NPC_SELF_REFERENCE}.{{0,8}}"
        rf"{_NPC_ACCESS_VERB}"
        rf".{{0,30}}{_NPC_LEDGER_SECRET}",
    )
    for pattern in document_patterns:
        for match in re.finditer(pattern, text):
            context = context_for(match)
            if re.search(
                rf"{_NPC_NEGATION}.{{0,3}}{_NPC_ACCESS_VERB}",
                context,
            ):
                continue
            if re.search(
                rf"{_NPC_RECENT_TIME}.{{0,4}}{_NPC_ACCESS_VERB}",
                context,
            ):
                continue
            if re.search(
                rf"[?？].{{0,6}}{_NPC_NEGATION}",
                context,
            ):
                continue
            suspected.add("king_access_hint_about_governor_ledger")
            break
        if "king_access_hint_about_governor_ledger" in suspected:
            break

    knowledge_patterns = (
        rf"{_NPC_GRAIN_SECRET}.{{0,30}}{_NPC_SELF_REFERENCE}.{{0,8}}"
        rf"{_NPC_KNOWLEDGE_VERB}",
        rf"{_NPC_SELF_REFERENCE}.{{0,8}}"
        rf"{_NPC_KNOWLEDGE_VERB}"
        rf".{{0,30}}{_NPC_GRAIN_SECRET}",
    )
    for pattern in knowledge_patterns:
        for match in re.finditer(pattern, text):
            if knowledge_is_non_prior(context_for(match)):
                continue
            suspected.add("king_knowledge_hint_about_grain_convoy")
            break
        if "king_knowledge_hint_about_grain_convoy" in suspected:
            break
    if "explicit_prior_knowledge_of_grain_withholding" in confirmed:
        suspected.discard("king_knowledge_hint_about_grain_convoy")
    return sorted(confirmed), sorted(suspected)


def _sanitize_npc_public_reply(reply: object) -> str:
    text = str(reply or "").strip()
    confirmed, suspected = _npc_private_exposure_categories(text)
    if confirmed or suspected:
        return "[检测到并隐藏了与本探针幕后秘密直接相关的公开回复]"
    return text[:2400]


def _sanitize_receipt_error_detail(value: object, *, limit: int = 300) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    confirmed, suspected = _npc_private_exposure_categories(text)
    if confirmed or suspected:
        return "[PRIVATE_DETAIL_REDACTED]"
    for marker in NPC_PRIVATE_MARKERS:
        text = text.replace(marker, "[PRIVATE_DETAIL_REDACTED]")
    text = re.sub(
        r"(?i)\b(?:private_plan|private_situation|facts_to_withhold|secrets?)\b"
        r"(?:\s*[:=]\s*(?:\{[^}]*\}|\[[^\]]*\]|[^；;。]*))?",
        "[PRIVATE_DETAIL_REDACTED]",
        text,
    )
    text = re.sub(
        r"(?:/Users|/home|/tmp|/var|/private|/Volumes)"
        r"(?:/[^\n:;,，；。]+)+",
        "[PATH]",
        text,
    )
    text = re.sub(
        r"[A-Za-z]:\\[^\n:;,，；。]+",
        "[PATH]",
        text,
    )
    return text[: max(0, int(limit))]


def _setup_npc_fixture_without_scene_writer(
    service: FUGMHttpService,
    campaign_id: str,
    session_id: str,
    channel_id: str,
) -> str:
    """Force the complete typed fixture through its supplied fallback prose.

    ``setup_scenario`` already provides the entire private situation, public
    opening, and handoff.  Calling a creative author here adds an unrelated
    provider dependency before the NPC dialogue under test.  Only the scene
    writer is unavailable inside this bounded setup call; the shared client
    remains attached to the core GM and NPC voice renderer.
    """

    runtime = service._runtime(campaign_id)
    writer = runtime.app.scene_creative_writer
    original_client = writer.client
    original_model = writer.model
    try:
        writer.client = None
        writer.model = ""
        return setup_scenario(
            service,
            campaign_id,
            session_id,
            channel_id,
        )
    finally:
        writer.client = original_client
        writer.model = original_model


def _npc_dialogue_result(
    *,
    model: str,
    status: int,
    response: dict[str, object],
    calls: list[dict[str, object]],
    elapsed_ms: int,
    isolated_port: int,
    state_hash_before: str,
    state_hash_after: str,
) -> dict[str, object]:
    raw_reply = str(response.get("reply") or "").strip()
    receipts: list[dict[str, object]] = []
    decide_receipt: dict[str, object] = {}
    for raw in list(response.get("tool_receipts") or []):
        if not isinstance(raw, dict):
            continue
        result = raw.get("result")
        result = result if isinstance(result, dict) else {}
        voice = result.get("npc_voice")
        voice = voice if isinstance(voice, dict) else {}
        safe = {
            "tool_name": str(raw.get("tool_name") or ""),
            "ok": bool(raw.get("ok")),
            "state_changed": bool(raw.get("state_changed")),
            "error_code": str(raw.get("error_code") or ""),
            "error_message": (
                _sanitize_receipt_error_detail(raw.get("message"))
                if not bool(raw.get("ok"))
                else ""
            ),
            "correction_hint": (
                _sanitize_receipt_error_detail(raw.get("correction_hint"))
                if not bool(raw.get("ok"))
                else ""
            ),
            "npc_voice": {
                "used_model": bool(voice.get("used_model")),
                "used_fallback": bool(voice.get("used_fallback")),
                "model": str(voice.get("model") or ""),
                "audit_performed": bool(voice.get("audit_performed")),
                "audit_passed": bool(voice.get("audit_passed")),
                "latency_ms": int(voice.get("latency_ms") or 0),
                "fallback_reason_present": bool(
                    str(voice.get("fallback_reason") or "").strip()
                ),
            }
            if voice
            else {},
        }
        receipts.append(safe)
        if safe["tool_name"] == "decide_npc_response" and safe["ok"]:
            decide_receipt = safe

    voice = dict(decide_receipt.get("npc_voice") or {})
    voice_calls = [
        call
        for call in calls
        if str(call.get("operation") or "") == "npc_voice_render"
    ]
    (
        confirmed_private_fact_leak_categories,
        suspected_private_surface_categories,
    ) = _npc_private_exposure_categories(raw_reply)
    confirmed_private_fact_leak_count = len(
        confirmed_private_fact_leak_categories
    )
    suspected_private_surface_count = len(suspected_private_surface_categories)
    agency_error = SceneMomentPolicy.player_agency_violation(
        raw_reply,
        {
            "prepared_npcs": [
                {
                    "name": "赤冠王阿德里安",
                    "public_role": "赤冠王国的国王",
                }
            ]
        },
    )
    checks = {
        "http_ok": status == 200 and bool(response.get("ok")),
        "reply_present": bool(raw_reply),
        "decide_npc_response_succeeded": bool(decide_receipt),
        "decide_npc_response_changed_state": bool(
            decide_receipt.get("state_changed")
        ),
        "npc_voice_render_called": bool(voice_calls),
        "npc_voice_render_succeeded": bool(voice_calls)
        and all(bool(call.get("ok")) for call in voice_calls),
        "npc_voice_render_model_matches": bool(voice_calls)
        and all(str(call.get("model") or "") == model for call in voice_calls),
        "npc_voice_receipt_used_model": voice.get("used_model") is True,
        "npc_voice_receipt_model_matches": str(voice.get("model") or "")
        == model,
        "npc_voice_no_fallback": voice.get("used_fallback") is False
        and not bool(voice.get("fallback_reason_present")),
        "no_failed_provider_attempt": bool(calls)
        and all(bool(call.get("ok")) for call in calls),
        "all_provider_calls_expected_model": bool(calls)
        and all(str(call.get("model") or "") == model for call in calls),
        "all_provider_calls_non_thinking": bool(calls)
        and all(call.get("thinking_enabled") is False for call in calls),
        "no_failed_receipt": bool(receipts)
        and all(bool(item.get("ok")) for item in receipts),
        "no_agent_error": not bool(str(response.get("agent_error") or "")),
        "no_confirmed_private_fact_leak": (
            confirmed_private_fact_leak_count == 0
        ),
        "no_suspected_private_surface": suspected_private_surface_count == 0,
        "no_backstage_formula": not SceneMomentPolicy.looks_like_backstage_formula(
            raw_reply
        ),
        "no_player_agency_violation": not bool(agency_error),
        "state_hash_changed": bool(state_hash_before)
        and bool(state_hash_after)
        and state_hash_before != state_hash_after,
    }
    safe_reply = _sanitize_npc_public_reply(raw_reply)
    return {
        "scenario": "authoritative_npc_dialogue",
        "passed": all(checks.values()),
        "elapsed_ms": int(elapsed_ms),
        "isolated_port": int(isolated_port),
        "route": str(response.get("route") or ""),
        "reply": safe_reply,
        "reply_sha256": hashlib.sha256(
            safe_reply.encode("utf-8")
        ).hexdigest(),
        "agent_error_category": (
            "present" if str(response.get("agent_error") or "").strip() else ""
        ),
        "confirmed_private_fact_leak_count": (
            confirmed_private_fact_leak_count
        ),
        "confirmed_private_fact_leak_categories": (
            confirmed_private_fact_leak_categories
        ),
        "suspected_private_surface_count": suspected_private_surface_count,
        "suspected_private_surface_categories": (
            suspected_private_surface_categories
        ),
        "private_review_required": suspected_private_surface_count > 0,
        "player_agency_violation": bool(agency_error),
        "receipts": receipts,
        "state_hash_before": state_hash_before,
        "state_hash_after": state_hash_after,
        "checks": checks,
        "provider": _usage_summary(calls),
        "provider_calls": calls,
    }


def authoritative_npc_dialogue_probe(
    client: OpenAICompatibleClient,
    *,
    model: str,
    rules_seed: int,
) -> dict[str, object]:
    service: FUGMHttpService | None = None
    runtime: Any | None = None
    server: Any | None = None
    server_thread: threading.Thread | None = None
    with tempfile.TemporaryDirectory(prefix="fu-gm-targeted-npc-dialogue-") as temp:
        data_root = Path(temp) / "campaigns"
        service = FUGMHttpService(
            data_root=data_root,
            use_llm=True,
            rules_seed=rules_seed,
            public_expression_mode="core",
            capability_routing_mode="intent",
            state_context_mode="summary_delta",
            test_llm_bundle=TestLLMClientBundle.shared(client, model=model),
        )
        safe_model = "".join(
            character if character.isalnum() else "-" for character in model
        ).strip("-")
        campaign_id = f"targeted-npc-dialogue-{safe_model}"
        session_id = "royal-petition"
        channel_id = "isolated-npc-dialogue"
        _setup_npc_fixture_without_scene_writer(
            service,
            campaign_id,
            session_id,
            channel_id,
        )
        runtime = service._runtime(campaign_id)
        state_hash_before = snapshot_hash(data_root)
        call_start = len(client.recent_calls)
        server = make_server("127.0.0.1", 0, service=service)
        host, port = server.server_address
        if int(port) == 8765:
            raise RuntimeError("isolated NPC dialogue selected production port")
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            status, response, elapsed_ms = request_json(
                str(host),
                int(port),
                "POST",
                "/v1/game/turn",
                {
                    "campaign_id": campaign_id,
                    "session_id": session_id,
                    "channel_id": channel_id,
                    "speaker": "阿凛",
                    "message": NPC_PETITION_MESSAGE,
                    "message_id": f"targeted-npc-dialogue-{safe_model}",
                    "is_at_bot": False,
                },
                timeout=300.0,
            )
            calls = sanitized_client_calls(client)[call_start:]
            state_hash_after = snapshot_hash(data_root)
            return _npc_dialogue_result(
                model=model,
                status=status,
                response=response,
                calls=calls,
                elapsed_ms=elapsed_ms,
                isolated_port=int(port),
                state_hash_before=state_hash_before,
                state_hash_after=state_hash_after,
            )
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            if server_thread is not None:
                server_thread.join(timeout=3.0)
            _shutdown_background_workers(service, runtime)


def combat_probe(
    client: OpenAICompatibleClient,
    *,
    model: str,
    rules_seed: int,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="fu-gm-targeted-combat-") as temp:
        service = FUGMHttpService(
            data_root=Path(temp) / "campaigns",
            use_llm=True,
            rules_seed=rules_seed,
            public_expression_mode="core",
            capability_routing_mode="intent",
            state_context_mode="summary_delta",
            test_llm_bundle=TestLLMClientBundle.shared(client, model=model),
        )
        campaign_id = f"targeted-combat-{model}"
        runtime = service._runtime(campaign_id, auto_load=False)
        _seed_online_smoke_fixture(runtime.app)
        service.session_gates.activate(
            campaign_id,
            "isolated-combat",
            "combat",
            status="adventure",
            reason="isolated model matrix",
        )
        message = "冒烟测试角色用武器普通攻击训练靶；请按规则真实投骰并结算。"
        call_start = len(client.recent_calls)
        started = time.monotonic()
        status, response = service.handle(
            "POST",
            "/v1/game/turn",
            {
                "campaign_id": campaign_id,
                "session_id": "combat",
                "channel_id": "isolated-combat",
                "speaker": "玩家",
                "message": message,
                "is_at_bot": True,
                "message_id": f"targeted-combat-{model}",
            },
        )
        calls = sanitized_client_calls(client)[call_start:]
        receipts = [
            {
                "tool_name": str(item.get("tool_name") or ""),
                "ok": bool(item.get("ok")),
                "state_changed": bool(item.get("state_changed")),
                "error_code": str(item.get("error_code") or ""),
            }
            for item in list(response.get("tool_receipts") or [])
            if isinstance(item, dict)
        ]
        hp_after = runtime.app.character_manager.get(_SMOKE_TARGET).hp
        action_receipt = next(
            (
                item
                for item in receipts
                if item["tool_name"] == "perform_character_action" and item["ok"]
            ),
            None,
        )
        checks = {
            "http_ok": status == 200 and bool(response.get("ok")),
            "perform_character_action_succeeded": action_receipt is not None,
            "target_hp_decreased": hp_after < _SMOKE_TARGET_INITIAL_HP,
            "no_failed_receipt": all(item["ok"] for item in receipts),
            "no_agent_error": not bool(str(response.get("agent_error") or "")),
        }
        return {
            "scenario": "natural_language_combat_attack",
            "passed": all(checks.values()),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "reply": str(response.get("reply") or ""),
            "route": str(response.get("route") or ""),
            "agent_error": str(response.get("agent_error") or ""),
            "target_hp_before": _SMOKE_TARGET_INITIAL_HP,
            "target_hp_after": hp_after,
            "receipts": receipts,
            "checks": checks,
            "provider": _usage_summary(calls),
            "provider_calls": calls,
        }


def main() -> int:
    os.umask(0o077)
    args = parse_args()
    os.environ.update(
        {
            "FU_GM_DOTENV_PATH": "/dev/null",
            "FU_GM_IMAGE_ENABLED": "0",
            "FU_GM_TOOL_AGENT_MAX_TOKENS": "2500",
            "FU_GM_TOOL_AGENT_TIMEOUT_SECONDS": "300",
            "FU_GM_CORE_GM_TIMEOUT_SECONDS": "300",
            "FU_GM_CORE_GM_ENDPOINT_ATTEMPT_TIMEOUT_SECONDS": "90",
            "FU_GM_NPC_VOICE_AUDIT_MODE": "off",
        }
    )
    needs_mimo = any(str(model).startswith("mimo-") for model in args.models)
    mimo_key = _mimo_api_key() if needs_mimo else ""
    configs = provider_configs(
        models=args.models,
        deepseek_dotenv=args.deepseek_dotenv,
        mimo_api_key=mimo_key,
    )
    secrets = [mimo_key, *[config.api_key for config in configs]]
    production_before = _production_snapshot()
    model_rows: list[dict[str, object]] = []
    for index, config in enumerate(configs, start=1):
        print(f"[{index}/{len(configs)}] targeted model={config.action_model}", flush=True)
        client = OpenAICompatibleClient(config)
        try:
            scenarios: list[dict[str, object]] = []
            if "session_zero_multi_category_contribution" in args.scenarios:
                scenarios.append(
                    session_zero_probe(client, model=config.action_model)
                )
            if "semantic_write_safety" in args.scenarios:
                scenarios.append(
                    semantic_safety_probe(client, model=config.action_model)
                )
            if "natural_language_combat_attack" in args.scenarios:
                scenarios.append(
                    combat_probe(
                        client,
                        model=config.action_model,
                        rules_seed=int(args.seed),
                    )
                )
            if "authoritative_npc_dialogue" in args.scenarios:
                scenarios.append(
                    authoritative_npc_dialogue_probe(
                        client,
                        model=config.action_model,
                        rules_seed=int(args.seed),
                    )
                )
            model_rows.append(
                {
                    "model": config.action_model,
                    "passed": all(bool(row.get("passed")) for row in scenarios),
                    "scenarios_passed": sum(bool(row.get("passed")) for row in scenarios),
                    "scenarios_total": len(scenarios),
                    "scenarios": scenarios,
                }
            )
        finally:
            close = getattr(client.transport, "close", None)
            if callable(close):
                close()
    production_after = _production_snapshot()
    production_unchanged = _production_unchanged(production_before, production_after)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_kind": "targeted_fu_gm_scenario_matrix",
        "rules_seed": int(args.seed),
        "scenarios_requested": list(args.scenarios),
        "thinking": "disabled",
        "models": model_rows,
        "production_before": production_before,
        "production_after": production_after,
        "production_unchanged": production_unchanged,
        "passed": production_unchanged
        and bool(model_rows)
        and all(bool(row.get("passed")) for row in model_rows),
    }
    output_dir = args.output_root / f"fu_gm_targeted_matrix_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(output_dir, 0o700)
    report_path = output_dir / "report.json"
    security_path = output_dir / "secret_scan.json"
    _write_secure(report_path, report)
    scan = _secret_scan((report_path,), secrets)
    _write_secure(security_path, scan)
    final_scan = _secret_scan((report_path, security_path), secrets)
    if not bool(scan.get("passed")) or not bool(final_scan.get("passed")):
        raise RuntimeError("artifact secret scan failed")
    print(f"report={report_path} passed={report['passed']}", flush=True)
    return 0 if bool(report["passed"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
