#!/usr/bin/env python3
"""在线验证场次创作由 DeepSeek 完成、语义复核由核心模型完成。"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from fu_gm.app_factory import _component_llm_config
from fu_gm.components.scene_creative_writer import SceneCreativeWriter
from fu_gm.components.session_prep_concretizer import SessionPrepConcretizer
from fu_gm.config import LLMConfig
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.models import (
    SessionClueRoute,
    SessionDramaticContract,
    SessionNPCRole,
    SessionSceneOpportunity,
)


def _brief() -> SessionDramaticContract:
    return SessionDramaticContract(
        session_number=1,
        title="第01场·卡里巴村越狱",
        location="卡里巴村监狱",
        dramatic_question="诺艾尔与艾丽妮能否逃出监狱，并决定如何面对追捕？",
        opening_disruption="监狱封印在夜雨中突然熄灭，走廊深处传来重物拖行声。",
        signature_image="蓝色牢门符文逐盏熄灭，雨水沿窄窗流成铁栏般的影子。",
        opposition_goal="监狱守卫要在异变扩大前重新封锁牢区。",
        reversal="这次封印失灵并非偶然故障。",
        important_npcs=[
            SessionNPCRole(
                name="值夜狱卒",
                public_role="值夜狱卒",
                goal_now="重新封锁牢区",
            )
        ],
        clue_routes=[
            SessionClueRoute(route_id=f"kariba-{index}", approach=approach)
            for index, approach in enumerate(
                ("观察符文", "询问狱卒", "检查值班记录"),
                start=1,
            )
        ],
        potential_scenes=[
            SessionSceneOpportunity(
                scene_key="kariba-open",
                scene_role="strong_start",
                title="熄灭的符文",
                location="牢房走廊",
            )
        ],
    )


def _world() -> dict[str, object]:
    return {
        "setting_summary": (
            "宁姆格福大陆的魔法与蒸汽科技彼此对立。"
            "两百年前的禁忌仪式让藤蔓赋予钢铁生命。"
        ),
        "location": "卡里巴村监狱",
        "allowed_locations": ["卡里巴村监狱", "卡里巴村"],
        "heroes": [
            {
                "name": "诺艾尔",
                "identity": "猫耳娘秘宝猎人",
                "equipment": ["细剑", "匕首（钢匕首模板）", "丝质衬衫"],
            },
            {
                "name": "艾丽妮",
                "identity": "被放逐的学徒",
                "equipment": ["元素使装备"],
            },
        ],
        "first_act_setup": {
            "summary": "第一幕从诺艾尔与艾丽妮在卡里巴村监狱越狱开始。",
            "answers": {"opening": "越狱"},
        },
    }


def _telemetry_summary(client: OpenAICompatibleClient) -> dict[str, object]:
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
    }


def run(output: Path) -> dict[str, object]:
    base = LLMConfig.from_env()
    action_config = _component_llm_config(base, "ACTION")
    expressor_config = _component_llm_config(base, "EXPRESSOR")
    creative_config = _component_llm_config(expressor_config, "CREATIVE")
    semantic_client = OpenAICompatibleClient(action_config)
    creative_client = OpenAICompatibleClient(creative_config)
    report: dict[str, object] = {
        "ok": False,
        "routes": {
            "semantic_reviewer": {
                "model": action_config.action_model,
                "endpoint": action_config.api_base_url,
            },
            "creative_author": {
                "model": creative_config.action_model,
                "endpoint": creative_config.api_base_url,
            },
        },
    }
    try:
        prep = SessionPrepConcretizer(
            client=creative_client,
            model=creative_config.action_model,
            review_client=semantic_client,
            review_model=action_config.action_model,
        )
        concrete = prep.concretize(
            _brief(),
            world_context=_world(),
            recent_contracts=[],
        )
        report["session"] = {
            key: getattr(concrete, key)
            for key in (
                "title",
                "dramatic_question",
                "signature_image",
                "opposition_goal",
                "dilemma",
                "reversal",
                "irreversible_change",
                "ending_echo",
            )
        }
        report["session_review"] = {
            "author_error": prep.last_error,
            "review_model": prep.reachability_reviewer.model,
            "review_status": prep.reachability_reviewer.last_status,
            "review_error": prep.reachability_reviewer.last_error,
        }

        writer = SceneCreativeWriter(
            client=creative_client,
            model=creative_config.action_model,
            audit_client=semantic_client,
            audit_model=action_config.action_model,
        )
        opening = writer.compose_scene_opening(
            scene_request={
                "name": "卡里巴村监狱越狱",
                "location": "卡里巴村监狱牢房走廊",
                "participants": ["诺艾尔", "艾丽妮"],
                "public_premise": "两名英雄被关在相邻牢房，封印刚刚失灵。",
            },
            session_contract=asdict(concrete),
            opening_contract={
                "required_public_facts": [
                    "诺艾尔与艾丽妮身处相邻牢房。",
                    "牢门符文刚刚熄灭。",
                ],
                "forbidden_private_facts": ["封印失灵的幕后原因"],
                "handoff": "把行动权交给两名玩家，不替她们越狱。",
            },
            current_message="从越狱开始吧。",
            recent_public_messages=[
                {"speaker": "测试玩家甲", "text": "从越狱开始吧。"}
            ],
        )
        report["opening"] = {
            "public_opening": opening.public_opening,
            "player_handoff": opening.player_handoff,
            "private_situation": opening.private_situation,
            "writer": writer.diagnostics(),
        }
        report["ok"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        report["telemetry"] = {
            "creative": _telemetry_summary(creative_client),
            "semantic": _telemetry_summary(semantic_client),
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/deepseek_creative_routing_probe.json"),
    )
    args = parser.parse_args()
    report = run(args.output)
    print(
        json.dumps(
            {
                "ok": report.get("ok"),
                "error": report.get("error", ""),
                "routes": report.get("routes"),
                "session_title": dict(report.get("session") or {}).get("title"),
                "opening": dict(report.get("opening") or {}).get("public_opening"),
                "handoff": dict(report.get("opening") or {}).get("player_handoff"),
                "report": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
