#!/usr/bin/env python3
"""A/B probe for selective DeepSeek V4 role-immersion thinking."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Any, Callable

from fu_gm.app_factory import _component_llm_config
from fu_gm.components.npc_voice_renderer import NPCVoiceRenderer
from fu_gm.components.scene_creative_writer import SceneCreativeWriter
from fu_gm.config import LLMConfig
from fu_gm.expressor import LLMExpressor
from fu_gm.llm_client import OpenAICompatibleClient


ROOT = Path(__file__).resolve().parents[1]
LEAK_MARKERS = (
    "<think>",
    "</think>",
    "（心想",
    "(心想",
    "内心OS",
    "内心 OS",
    "内心独白",
    "思考过程",
)


def _persona() -> SimpleNamespace:
    return SimpleNamespace(
        name="白花守望会会长",
        public_identity="白花守望会的负责人",
        role_in_story="旧路守护者",
        manner="克制、警惕，但并不故意刁难人",
        speech_style="自然完整的短句；先正面回答，再说明边界",
        traits=["谨慎", "负责", "念旧"],
        voice_examples=["门能开，不过今晚得让我的人带路。"],
        current_mood="仍有戒心",
        current_stance="愿意听取请求",
        core_drive="保护驿站与受庇护者",
        active_goal="确认英雄不会把财团引进旧路",
        authority_scope="可决定旧路是否开放",
        knowledge_scope="熟悉驿站与旧路",
        refusal_move="拒绝交出钥匙",
        taboos=["不拿平民冒险"],
    )


def _leaks(*texts: str) -> list[str]:
    joined = "\n".join(str(text or "") for text in texts)
    return [marker for marker in LEAK_MARKERS if marker.lower() in joined.lower()]


def _telemetry(client: OpenAICompatibleClient) -> dict[str, object]:
    payload = client.telemetry_payload()
    cache = dict(payload.get("prompt_cache") or {})
    return {
        "total_calls": payload.get("total_calls"),
        "successful_calls": payload.get("successful_calls"),
        "failed_calls": payload.get("failed_calls"),
        "latency": payload.get("latency"),
        "prompt_cache": {
            key: cache.get(key)
            for key in (
                "eligible_calls",
                "usage_reported_calls",
                "hit_calls",
                "known_miss_calls",
                "prompt_tokens",
                "cached_tokens",
                "read_ratio",
                "by_family",
            )
        },
        "recent_calls": list(client.recent_calls),
    }


def _run_case(
    *,
    label: str,
    mode: str,
    repeat: int,
    callback: Callable[[], tuple[str, str, dict[str, object]]],
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for sample in range(1, repeat + 1):
        started = time.monotonic()
        record: dict[str, object] = {
            "case": label,
            "mode": mode,
            "sample": sample,
            "ok": False,
        }
        try:
            raw, final, metadata = callback()
            record.update(
                {
                    "ok": True,
                    "raw_output": raw,
                    "final_output": final,
                    "metadata": metadata,
                    "leak_markers": _leaks(raw, final),
                    "final_chars": len(final),
                }
            )
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
        record["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        records.append(record)
    return records


def _gm_case(
    client: OpenAICompatibleClient,
    *,
    mode: str,
) -> Callable[[], tuple[str, str, dict[str, object]]]:
    expressor = LLMExpressor(
        client=client,
        model=client.config.expressor_model,
        allow_fallback=False,
        gm_personality_prompt=(
            "你是时悠，像群里的真人主持人一样自然、轻快、敏锐。"
            "不复述玩家，不说后台术语，也不机械教学。"
        ),
        deepseek_roleplay_mode=mode,
        rule_result_prose_enabled=False,
    )

    def call() -> tuple[str, str, dict[str, object]]:
        try:
            parts = expressor.render_agent_message(
                [
                    "可以进行单人跑团，不必等待其他玩家。",
                    "询问玩家想沿用已有角色，还是新建角色；也可以先说题材或开场画面。",
                ],
                current_message="悠老师，我想一个人跑团。",
                recent_context="群里其他玩家今天不在线。",
                gate_status="inactive",
                route_mode="gm_agent_reply",
                expression_style="immersive" if mode == "inner_os" else "plain",
            )
        except Exception as exc:
            raise RuntimeError(
                f"{exc}; detail={expressor.last_error}; raw={expressor.last_raw_content}"
            ) from exc
        return (
            expressor.last_raw_content,
            "\n".join(parts),
            dict(expressor.last_agent_message_metadata),
        )

    return call


def _npc_case(
    client: OpenAICompatibleClient,
    *,
    mode: str,
) -> Callable[[], tuple[str, str, dict[str, object]]]:
    renderer = NPCVoiceRenderer(
        client=client,
        model=client.config.action_model,
        audit_mode="off",
        deepseek_roleplay_mode=mode,
    )

    def call() -> tuple[str, str, dict[str, object]]:
        result = renderer.render(
            persona=_persona(),
            public_segments=[
                {
                    "id": "answer",
                    "text": "东侧旧路今晚可以通行。",
                    "tags": ["direct_answer"],
                },
                {
                    "id": "limit",
                    "text": "钥匙不会交给英雄，但会由巡守带队。",
                    "tags": ["gate_requirement"],
                },
            ],
            speech_plan={
                "speech_act": "answer",
                "proposal_outcome": "accepted_with_condition",
                "condition_outcome": "required",
                "commitment_outcome": "offered",
            },
            current_message="东侧旧路今晚能走吗？钥匙能给我们吗？",
            recent_context="英雄正在风铃廊与会长谈判。",
            scene=SimpleNamespace(name="白花碑驿站", location="风铃廊"),
        )
        return renderer.last_raw_content, result.text, result.telemetry()

    return call


def _scene_case(
    client: OpenAICompatibleClient,
    *,
    mode: str,
) -> Callable[[], tuple[str, str, dict[str, object]]]:
    writer = SceneCreativeWriter(
        client=client,
        model=client.config.action_model,
        deepseek_roleplay_mode=mode,
    )

    def call() -> tuple[str, str, dict[str, object]]:
        try:
            result = writer.compose_scene_opening(
                scene_request={
                    "name": "卡里巴村监狱越狱",
                    "location": "卡里巴村监狱牢区",
                    "participants": ["诺艾尔", "艾丽妮"],
                    "public_premise": "两名英雄被关在相邻牢房。",
                },
                session_contract={
                    "dramatic_question": "两人能否在封锁恢复前逃出牢区？",
                    "signature_image": "雨水沿窄窗流下，像铁栏投在石墙上的影子。",
                    "opposition_goal": "值夜守卫要重新封锁牢区。",
                },
                opening_contract={
                    "opening_disruption": "相邻牢房的蓝色门符同时熄灭。",
                    "forbidden_private_facts": ["封印失灵的幕后原因"],
                },
                current_message="重新开始第一章。",
                recent_public_messages=[],
            )
        except Exception as exc:
            raise RuntimeError(
                f"{exc}; detail={writer.last_error}; raw={writer.last_raw_content}"
            ) from exc
        final = f"{result.public_opening}\n{result.player_handoff}"
        return (
            writer.last_raw_content,
            final,
            {
                "used_model": result.used_model,
                "model": result.model,
                "audit_status": writer.last_audit_status,
                "private_situation_keys": sorted(result.private_situation),
            },
        )

    return call


def run(*, output: Path, repeat: int) -> dict[str, object]:
    base = LLMConfig.from_env()
    expressor_config = _component_llm_config(base, "EXPRESSOR")
    model = str(expressor_config.expressor_model or expressor_config.action_model)
    if not model.lower().startswith("deepseek-v4"):
        raise RuntimeError(f"Expressor不是DeepSeek V4：{model}")
    baseline_config = replace(expressor_config, thinking_enabled=False)
    immersive_config = replace(expressor_config, thinking_enabled=True)
    baseline_client = OpenAICompatibleClient(baseline_config)
    immersive_client = OpenAICompatibleClient(immersive_config)
    records: list[dict[str, object]] = []
    for label, factory in (
        ("ordinary_gm", _gm_case),
        ("npc_voice", _npc_case),
        ("scene_opening", _scene_case),
    ):
        records.extend(
            _run_case(
                label=label,
                mode="default",
                repeat=repeat,
                callback=factory(baseline_client, mode="default"),
            )
        )
        records.extend(
            _run_case(
                label=label,
                mode="inner_os",
                repeat=repeat,
                callback=factory(immersive_client, mode="inner_os"),
            )
        )
    successful = [item for item in records if item.get("ok")]
    leaked = [item for item in successful if item.get("leak_markers")]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "endpoint": expressor_config.api_base_url,
        "repeat_per_case": repeat,
        "summary": {
            "total": len(records),
            "successful": len(successful),
            "failed": len(records) - len(successful),
            "leaked": len(leaked),
            "baseline_average_ms": _average_ms(records, "default"),
            "immersive_average_ms": _average_ms(records, "inner_os"),
        },
        "records": records,
        "telemetry": {
            "baseline": _telemetry(baseline_client),
            "immersive": _telemetry(immersive_client),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _average_ms(records: list[dict[str, object]], mode: str) -> float:
    values = [
        int(item.get("elapsed_ms") or 0)
        for item in records
        if item.get("mode") == mode and item.get("ok")
    ]
    return round(sum(values) / len(values), 1) if values else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = args.output or (
        ROOT / "reports" / f"deepseek_roleplay_ab_{timestamp}.json"
    )
    try:
        report = run(output=output, repeat=max(1, int(args.repeat)))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "ok": report["summary"]["failed"] == 0,
                "model": report["model"],
                "summary": report["summary"],
                "report": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
