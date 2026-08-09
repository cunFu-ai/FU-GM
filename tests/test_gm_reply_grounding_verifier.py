from __future__ import annotations

import json

from fu_gm.components.gm_reply_grounding_verifier import (
    GMReplyGroundingReview,
    GMReplyGroundingVerifier,
    TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT,
)
from fu_gm.gm_tool_agent import LLMGMToolAgent
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
)


class ScriptedClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []
        self.config = type("Config", (), {"response_format_enabled": True})()

    def create_chat_completion(self, **kwargs) -> str:
        self.calls.append(dict(kwargs))
        return self.responses.pop(0)


def _context() -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="grounding-test",
        session_id="s1",
        channel_id="group-1",
        speaker="阿凛",
        gate_status="adventure",
        directly_addressed=False,
    )


def test_semantic_grounding_verifier_parses_unsupported_claims() -> None:
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "valid": False,
                    "category": "unsupported_external_result",
                    "unsupported_claims": ["守卫已经倒下并被缴械"],
                    "correction_hint": "先使用冲突或场景工具提交结果。",
                },
                ensure_ascii=False,
            )
        ]
    )
    verifier = GMReplyGroundingVerifier(client, model="semantic-model")

    review = verifier.verify(
        current_message="我在对方倒下后收走他的武器。",
        recent_context="双方还在交涉。",
        observed_state={"conflict": {"active": False}},
        receipts=[],
        proposed_reply="守卫倒在地上，武器已经被你收走。",
        message_kind="performed_action",
        decision_reason="接受玩家行动。",
        deadline=999999999.0,
    )

    assert review.valid is False
    assert review.category == "unsupported_external_result"
    assert review.unsupported_claims == ("守卫已经倒下并被缴械",)
    assert client.calls[0]["operation"] == "gm_reply_grounding_verification"
    assert client.calls[0]["messages"][0].cache_family == "ground-reply"
    assert client.calls[0]["messages"][0].cache_breakpoint is True
    assert client.calls[0]["messages"][1].cache_breakpoint is True


def test_semantic_grounding_verifier_reviews_tool_before_write() -> None:
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "valid": False,
                    "category": "false_premise",
                    "unsupported_claims": ["会长此前提到过庄园"],
                    "correction_hint": "澄清当前公开对话中没人提到庄园。",
                },
                ensure_ascii=False,
            )
        ]
    )
    verifier = GMReplyGroundingVerifier(client, model="semantic-model")

    review = verifier.verify_tool_proposal(
        current_message="刚才是谁提到了庄园？",
        recent_context="会长只说了东侧堤脊。",
        observed_state={"scene": {"public_facts": ["东侧堤脊可以绕行"]}},
        tool_name="decide_npc_response",
        arguments={
            "name": "守望会会长",
            "public_segments": [{"text": "庄园是我刚才提到的。"}],
        },
        deadline=999999999.0,
    )

    assert review.valid is False
    assert review.category == "false_premise"
    assert client.calls[0]["operation"] == "gm_tool_proposal_grounding_verification"
    assert client.calls[0]["messages"][0].cache_family == "ground-tool"
    assert client.calls[0]["messages"][0].cache_breakpoint is True
    assert client.calls[0]["messages"][1].cache_breakpoint is True


def test_tool_grounding_prompt_blocks_false_premise_leaks_and_vague_check_answers() -> None:
    assert "不能借这个错误前提" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "一件能派上用场的物件" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "closing_image" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "不能只提交移动后静默结束" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "只提交acquire属于半截意图" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    assert "不等于该PC已经接住或取得" in TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT


def test_agent_returns_unsupported_reply_to_itself_then_uses_scene_tool() -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="commit_scene_response",
            description="提交场景回应。",
            handler=lambda _context, _arguments: GMToolReceipt.success(
                "commit_scene_response",
                state_changed=True,
                public_reply="守卫仍站在盾后，双方还没有交手。",
                lock_public_reply=True,
            ),
            side_effect="write",
        )
    )
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "final",
                    "message_kind": "performed_action",
                    "audience": "table",
                    "reply": "守卫已经倒下，武器也被收走了。",
                    "reason": "接受玩家声明。",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "decision": "call_tool",
                    "message_kind": "performed_action",
                    "audience": "table",
                    "tool_name": "commit_scene_response",
                    "arguments": {},
                    "reason": "澄清当前权威局面。",
                },
                ensure_ascii=False,
            ),
        ]
    )

    class RejectFirstReply:
        def verify(self, **_kwargs) -> GMReplyGroundingReview:
            return GMReplyGroundingReview(
                valid=False,
                category="unsupported_external_result",
                unsupported_claims=("守卫已经倒下",),
                correction_hint="通过场景工具澄清当前状态。",
            )

    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=registry,
        reply_grounding_verifier=RejectFirstReply(),
    )

    outcome = agent.run(
        "诺艾尔在对方倒下后收走武器。",
        recent_context="守卫仍举盾挡路。",
        context=_context(),
        state_summary={"conflict": {"active": False}},
    )

    assert outcome.reply == "守卫仍站在盾后，双方还没有交手。"
    assert len(client.calls) == 2
    assert any(
        step.get("reply_grounding", {}).get("valid") is False
        for step in outcome.trace
    )


def test_locked_public_tool_reply_bypasses_semantic_review() -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="resolve_action",
            description="结算行动。",
            handler=lambda _context, _arguments: GMToolReceipt.success(
                "resolve_action",
                state_changed=True,
                public_reply="检定失败，守卫仍守在门前。",
                lock_public_reply=True,
            ),
            side_effect="write",
        )
    )
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "call_tool",
                    "message_kind": "performed_action",
                    "audience": "table",
                    "tool_name": "resolve_action",
                    "arguments": {},
                },
                ensure_ascii=False,
            )
        ]
    )

    class MustNotRun:
        def verify(self, **_kwargs):
            raise AssertionError("锁定工具回执不应再次接受语义审计。")

    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=registry,
        reply_grounding_verifier=MustNotRun(),
    )
    outcome = agent.run(
        "诺艾尔试图撞开守卫。",
        recent_context="",
        context=_context(),
        state_summary={},
    )

    assert outcome.reply == "检定失败，守卫仍守在门前。"


def test_agent_rejects_false_premise_npc_write_before_handler_runs() -> None:
    registry = GMToolRegistry()
    handler_calls: list[dict[str, object]] = []

    def handle(_context, arguments):
        handler_calls.append(dict(arguments))
        return GMToolReceipt.success(
            "decide_npc_response",
            state_changed=True,
            public_reply="没人提到庄园。刚才说的是东侧堤脊。",
            lock_public_reply=True,
        )

    registry.register(
        GMToolDefinition(
            name="decide_npc_response",
            description="提交NPC回应。",
            handler=handle,
            parameters=(
                GMToolParameter("name", "string", "NPC名。", required=True),
                GMToolParameter(
                    "public_segments",
                    "array",
                    "公开回应片段。",
                    required=True,
                ),
            ),
            side_effect="write",
        )
    )
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "call_tool",
                    "message_kind": "npc_or_world_interaction",
                    "audience": "table",
                    "tool_name": "decide_npc_response",
                    "arguments": {
                        "name": "守望会会长",
                        "public_segments": [{"text": "庄园是我刚才提到的。"}],
                    },
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "decision": "call_tool",
                    "message_kind": "npc_or_world_interaction",
                    "audience": "table",
                    "tool_name": "decide_npc_response",
                    "arguments": {
                        "name": "守望会会长",
                        "public_segments": [
                            {"text": "没人提到庄园。刚才说的是东侧堤脊。"}
                        ],
                    },
                },
                ensure_ascii=False,
            ),
        ]
    )

    class RejectThenAccept:
        def __init__(self) -> None:
            self.calls = 0

        def verify_tool_proposal(self, **_kwargs) -> GMReplyGroundingReview:
            self.calls += 1
            if self.calls == 1:
                return GMReplyGroundingReview(
                    valid=False,
                    category="false_premise",
                    unsupported_claims=("会长此前提到过庄园",),
                    correction_hint="澄清没人提过庄园，不要虚构说话者。",
                )
            return GMReplyGroundingReview(valid=True)

    verifier = RejectThenAccept()
    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=registry,
        reply_grounding_verifier=verifier,
    )

    outcome = agent.run(
        "刚才是谁提到了庄园？",
        recent_context="会长只说了东侧堤脊。",
        context=_context(),
        state_summary={"scene": {"public_facts": ["东侧堤脊可以绕行"]}},
    )

    assert outcome.reply == "没人提到庄园。刚才说的是东侧堤脊。"
    assert len(handler_calls) == 1
    assert handler_calls[0]["public_segments"][0]["text"].startswith("没人")
    assert any(
        step.get("tool_proposal_grounding", [{}])[0].get("valid") is False
        for step in outcome.trace
        if step.get("tool_proposal_grounding")
    )


def test_agent_rejects_partial_story_item_acquire_then_commits_final_place_silently() -> None:
    registry = GMToolRegistry()
    handler_calls: list[dict[str, object]] = []

    def handler(_context, arguments):
        handler_calls.append(dict(arguments))
        return GMToolReceipt.success(
            "commit_story_item_action",
            result={"silent_commit_allowed": True},
            state_changed=True,
        )

    registry.register(
        GMToolDefinition(
            name="commit_story_item_action",
            description="原子提交剧情物件的最终状态。",
            handler=handler,
            parameters=(
                GMToolParameter("operation", "string", "最终操作。", required=True),
                GMToolParameter("to_location", "string", "最终地点。"),
            ),
            side_effect="write",
        )
    )
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "call_tool",
                    "message_kind": "performed_action",
                    "audience": "table",
                    "tool_name": "commit_story_item_action",
                    "arguments": {"operation": "acquire"},
                    "reason": "先登记取得。",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "decision": "call_tool",
                    "message_kind": "performed_action",
                    "audience": "table",
                    "tool_name": "commit_story_item_action",
                    "arguments": {
                        "operation": "place",
                        "to_location": "艾丽妮牢房一侧",
                    },
                    "reason": "一次提交动作结束时的最终落点。",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "decision": "final",
                    "audience": "table",
                    "reply": "诺艾尔把铁片抛到了艾丽妮那边。",
                    "reason": "确认动作。",
                },
                ensure_ascii=False,
            ),
        ]
    )

    class RejectPartialAcquire:
        def verify_tool_proposal(self, **kwargs) -> GMReplyGroundingReview:
            arguments = kwargs.get("arguments") or {}
            if arguments.get("operation") == "acquire":
                return GMReplyGroundingReview(
                    valid=False,
                    category="needs_clarification",
                    unsupported_claims=("只登记取得，遗漏随后抛出的最终落点",),
                    correction_hint="使用place一次提交物件的最终落点，不设置接收者。",
                )
            return GMReplyGroundingReview(valid=True)

    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=registry,
        reply_grounding_verifier=RejectPartialAcquire(),
    )
    message = "诺艾尔捡起细长铁片，和艾丽妮说完后，把铁片从铁栏缝隙抛了过去。"

    outcome = agent.run(
        message,
        recent_context="两人在相邻石牢，中间隔着铁栏。",
        context=_context(),
        state_summary={"scene": {"location": "卡里巴村监狱"}},
    )

    assert handler_calls == [
        {"operation": "place", "to_location": "艾丽妮牢房一侧"}
    ]
    assert outcome.target == "silent"
    assert outcome.reply == ""
    assert outcome.mode == "gm_agent_silent_commit"
    assert any(
        row.get("valid") is False
        for step in outcome.trace
        for row in step.get("tool_proposal_grounding", [])
    )
