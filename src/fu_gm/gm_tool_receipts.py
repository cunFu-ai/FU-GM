from __future__ import annotations

from copy import deepcopy

from fu_gm.gm_tool_contracts import GMToolExecutionContext, GMToolReceipt


class GMToolReceiptPolicy:
    """Normalize post-tool state, recovery and player-facing reply selection."""

    COMMITTED_ACTION_ACTORS_KEY = "_gm_agent_committed_action_actors"
    COMMITTED_ACTION_ROUNDS_KEY = "_gm_agent_committed_action_rounds"
    REQUIRED_FOLLOWUP_CONTEXT_KEY = "_gm_agent_required_followup_context"
    _FOLLOWUP_IDENTITY_KEYS = frozenset(
        {
            "campaign_id",
            "slot",
            "name",
            "npc",
            "actor",
            "window_id",
            "condition_id",
            "commitment_id",
            "pending_question_id",
            "question_id",
            "placement_context_id",
            "location_name",
            "clock_name",
            "scene_id",
            "expected_actor",
            "item_id",
        }
    )

    _FALLBACK_SUPERSEDES = {
        "confirm_hero_draft": {"update_hero_draft"},
        "confirm_session_zero_proposal": {"propose_session_zero_update"},
        "load_campaign": {"list_saves"},
    }

    @classmethod
    def apply_context(
        cls,
        context: GMToolExecutionContext,
        state_summary: dict[str, object],
        receipt: GMToolReceipt,
        *,
        tool_arguments: dict[str, object] | None = None,
    ) -> None:
        if not receipt.ok or not isinstance(receipt.result, dict):
            return
        result = receipt.result
        gate = result.get("gate")
        if isinstance(gate, dict):
            status = str(gate.get("status") or "").strip()
            if status:
                context.gate_status = status
            gate_campaign_id = str(gate.get("campaign_id") or "").strip()
            if gate_campaign_id:
                context.campaign_id = gate_campaign_id
            runtime_summary = state_summary.get("runtime")
            if isinstance(runtime_summary, dict):
                runtime_summary["gate"] = deepcopy(gate)
        if receipt.tool_name in {"create_campaign", "load_campaign"}:
            active_campaign_id = str(
                result.get("active_campaign_id") or result.get("campaign_id") or ""
            ).strip()
            if active_campaign_id:
                context.campaign_id = active_campaign_id
        cls._remember_committed_actions(context, result)
        cls._remember_required_followup(
            context,
            receipt,
            tool_arguments=tool_arguments,
        )

    @classmethod
    def _remember_required_followup(
        cls,
        context: GMToolExecutionContext,
        receipt: GMToolReceipt,
        *,
        tool_arguments: dict[str, object] | None = None,
    ) -> None:
        result = receipt.result
        declared = [
            str(item or "").strip()
            for item in list(result.get("required_followup_tools") or [])
            if str(item or "").strip()
        ]
        active = context.metadata.get(cls.REQUIRED_FOLLOWUP_CONTEXT_KEY)
        active = deepcopy(active) if isinstance(active, dict) else {}
        active_tools = [
            str(item or "").strip()
            for item in list(active.get("required_tools") or [])
            if str(item or "").strip()
        ]
        active_calls = [
            deepcopy(item)
            for item in list(active.get("required_calls") or [])
            if isinstance(item, dict)
            and str(item.get("tool_name") or "").strip()
        ]
        active_mode = str(active.get("mode") or "any").strip().lower()

        if receipt.tool_name in active_tools:
            matching_calls = [
                index
                for index, call in enumerate(active_calls)
                if str(call.get("tool_name") or "").strip()
                == receipt.tool_name
            ]
            matching_call = cls._matching_followup_call_index(
                active_calls,
                receipt.tool_name,
                tool_arguments,
            )
            if active_mode == "all":
                if matching_call is not None:
                    active_calls.pop(matching_call)
                    active_tools = list(
                        dict.fromkeys(
                            [
                                str(call.get("tool_name") or "").strip()
                                for call in active_calls
                                if str(call.get("tool_name") or "").strip()
                            ]
                            + [
                                name
                                for name in active_tools
                                if name != receipt.tool_name
                            ]
                        )
                    )
                elif not matching_calls:
                    active_tools = [
                        name for name in active_tools if name != receipt.tool_name
                    ]
            else:
                if matching_call is not None or not matching_calls:
                    active_tools = []
                    active_calls = []

        declared_calls = [
            deepcopy(item)
            for item in list(result.get("required_followup_calls") or [])
            if isinstance(item, dict)
            and str(item.get("tool_name") or "").strip() in declared
        ]
        declared_mode = str(
            result.get("required_followup_mode") or "any"
        ).strip().lower()
        if declared:
            if active_tools:
                active_tools = list(dict.fromkeys([*active_tools, *declared]))
                active_calls.extend(declared_calls)
                active_mode = "all"
            else:
                active_tools = list(dict.fromkeys(declared))
                active_calls = declared_calls
                active_mode = "all" if declared_mode == "all" else "any"
            active.update(
                {
                    "source_tool": receipt.tool_name,
                    "fulfilled_condition": deepcopy(
                        result.get("fulfilled_condition")
                        if isinstance(result.get("fulfilled_condition"), dict)
                        else {}
                    ),
                    "condition_payoff_due_from": str(
                        result.get("condition_payoff_due_from") or ""
                    ).strip(),
                    "triggered_commitment": deepcopy(
                        result.get("triggered_commitment")
                        if isinstance(result.get("triggered_commitment"), dict)
                        else {}
                    ),
                    "commitment_payoff_due_from": str(
                        result.get("commitment_payoff_due_from") or ""
                    ).strip(),
                }
            )

        if active_tools:
            active["required_tools"] = active_tools
            active["required_calls"] = active_calls
            active["mode"] = active_mode
            context.metadata[cls.REQUIRED_FOLLOWUP_CONTEXT_KEY] = active
            result["required_followup_tools"] = list(active_tools)
            result["required_followup_calls"] = deepcopy(active_calls)
            result["required_followup_mode"] = active_mode
        else:
            context.metadata.pop(cls.REQUIRED_FOLLOWUP_CONTEXT_KEY, None)

    @classmethod
    def _matching_followup_call_index(
        cls,
        calls: list[dict[str, object]],
        tool_name: str,
        tool_arguments: dict[str, object] | None,
    ) -> int | None:
        candidates = [
            (index, call)
            for index, call in enumerate(calls)
            if str(call.get("tool_name") or "").strip() == tool_name
        ]
        if not candidates:
            return None
        if tool_arguments is None:
            return candidates[0][0]
        for index, call in candidates:
            expected = cls._followup_identity_arguments(
                dict(call.get("arguments") or {})
            )
            if not expected or all(
                cls._identity_value(tool_arguments.get(key))
                == cls._identity_value(value)
                for key, value in expected.items()
            ):
                return index
        return None

    @classmethod
    def _followup_identity_arguments(
        cls,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        return {
            key: value
            for key, value in arguments.items()
            if key in cls._FOLLOWUP_IDENTITY_KEYS
            and cls._identity_value(value) not in ("", (), None)
        }

    @staticmethod
    def _identity_value(value: object) -> object:
        if isinstance(value, str):
            return " ".join(value.split()).strip()
        if isinstance(value, list):
            return tuple(GMToolReceiptPolicy._identity_value(item) for item in value)
        if isinstance(value, dict):
            return tuple(
                sorted(
                    (
                        str(key),
                        GMToolReceiptPolicy._identity_value(item),
                    )
                    for key, item in value.items()
                )
            )
        return value

    @classmethod
    def required_followup_calls(
        cls,
        receipts: list[GMToolReceipt],
    ) -> list[dict[str, object]]:
        for receipt in reversed(receipts):
            if not receipt.ok or not receipt.state_changed:
                continue
            if "required_followup_calls" not in receipt.result:
                return []
            return [
                deepcopy(item)
                for item in list(
                    receipt.result.get("required_followup_calls") or []
                )
                if isinstance(item, dict)
                and str(item.get("tool_name") or "").strip()
            ]
        return []

    @classmethod
    def followup_call_matches(
        cls,
        receipts: list[GMToolReceipt],
        *,
        tool_name: str,
        arguments: dict[str, object],
    ) -> bool:
        calls = [
            call
            for call in cls.required_followup_calls(receipts)
            if str(call.get("tool_name") or "").strip() == tool_name
        ]
        if not calls:
            return True
        return (
            cls._matching_followup_call_index(
                calls,
                tool_name,
                arguments,
            )
            is not None
        )

    @classmethod
    def _remember_committed_actions(
        cls,
        context: GMToolExecutionContext,
        result: dict[str, object],
    ) -> None:
        events: list[dict[str, object]] = []
        action_round = result.get("action_round")
        if isinstance(action_round, dict) and action_round:
            events.append(action_round)
        for item in list(result.get("action_round_events") or []):
            if isinstance(item, dict) and item:
                events.append(item)
        if not events:
            return
        actors = [
            str(item or "").strip()
            for item in list(
                context.metadata.get(cls.COMMITTED_ACTION_ACTORS_KEY) or []
            )
            if str(item or "").strip()
        ]
        rounds = dict(
            context.metadata.get(cls.COMMITTED_ACTION_ROUNDS_KEY) or {}
        )
        for event in events:
            progress = event.get("action_round_progress")
            actor = str(
                progress.get("actor")
                if isinstance(progress, dict)
                else event.get("actor") or ""
            ).strip()
            if not actor:
                continue
            if actor not in actors:
                actors.append(actor)
            rounds[actor] = deepcopy(event)
        if actors:
            context.metadata[cls.COMMITTED_ACTION_ACTORS_KEY] = actors
            context.metadata[cls.COMMITTED_ACTION_ROUNDS_KEY] = rounds

    @classmethod
    def action_already_committed(
        cls,
        context: GMToolExecutionContext,
        actor: str,
    ) -> bool:
        clean_actor = str(actor or "").strip()
        return bool(
            clean_actor
            and clean_actor
            in {
                str(item or "").strip()
                for item in list(
                    context.metadata.get(cls.COMMITTED_ACTION_ACTORS_KEY) or []
                )
            }
        )

    @classmethod
    def committed_action_round(
        cls,
        context: GMToolExecutionContext,
        actor: str,
    ) -> dict[str, object]:
        clean_actor = str(actor or "").strip()
        rounds = context.metadata.get(cls.COMMITTED_ACTION_ROUNDS_KEY)
        if not clean_actor or not isinstance(rounds, dict):
            return {}
        event = rounds.get(clean_actor)
        return deepcopy(event) if isinstance(event, dict) else {}

    @classmethod
    def state_change_recovered(cls, receipts: list[GMToolReceipt]) -> bool:
        successful_write_indexes = [
            index
            for index, receipt in enumerate(receipts)
            if receipt.ok and receipt.state_changed
        ]
        if not successful_write_indexes:
            return False
        if cls.required_followup_tools(receipts):
            return False
        return all(receipt.ok for receipt in receipts[successful_write_indexes[-1] + 1 :])

    @staticmethod
    def receipt_fallback(receipts: list[GMToolReceipt]) -> str:
        """Return only text backed by a successful committed tool.

        Retryable validation failures are private protocol feedback for the GM
        agent. Publishing their fallback text leaks backend state and can make
        a failed transaction look like an in-world NPC response.
        """

        successful_names = {
            receipt.tool_name
            for receipt in receipts
            if receipt.ok and receipt.state_changed
        }
        superseded = {
            earlier
            for later, earlier_names in GMToolReceiptPolicy._FALLBACK_SUPERSEDES.items()
            if later in successful_names
            for earlier in earlier_names
        }
        successful_fallbacks: list[str] = []
        for receipt in receipts:
            if not receipt.ok or not receipt.state_changed:
                continue
            if receipt.tool_name in superseded:
                continue
            candidate = str(receipt.public_fallback_reply or "").strip()
            if candidate and candidate not in successful_fallbacks:
                successful_fallbacks.append(candidate)
        if len(successful_fallbacks) > 1 and all(receipt.ok for receipt in receipts):
            return "\n".join(successful_fallbacks)
        for receipt in reversed(receipts):
            if (
                receipt.public_fallback_reply
                and (receipt.ok or (not receipt.ok and not receipt.retryable))
            ):
                return receipt.public_fallback_reply
        return ""

    @staticmethod
    def locked_public_reply(receipts: list[GMToolReceipt]) -> str:
        declared_state_lines: set[str] = set()
        latest_state_lines: list[str] = []
        for receipt in receipts:
            if not receipt.ok or not receipt.lock_public_reply:
                continue
            lines = [
                str(item or "").strip()
                for item in list(receipt.result.get("public_state_lines") or [])
                if str(item or "").strip()
            ]
            if not lines:
                continue
            declared_state_lines.update(lines)
            latest_state_lines = list(dict.fromkeys(lines))

        replies: list[str] = []
        for receipt in receipts:
            if not receipt.ok or not receipt.lock_public_reply:
                continue
            candidate = str(receipt.public_fallback_reply or "").strip()
            if not candidate:
                continue
            if declared_state_lines:
                candidate = "\n".join(
                    line
                    for line in candidate.splitlines()
                    if line.strip() not in declared_state_lines
                ).strip()
            if not candidate:
                continue
            if any(candidate == existing or candidate in existing for existing in replies):
                continue
            containing = next(
                (index for index, existing in enumerate(replies) if existing in candidate),
                None,
            )
            if containing is not None:
                replies[containing] = candidate
            else:
                replies.append(candidate)
        return "\n".join([*replies, *latest_state_lines])

    @classmethod
    def authoritative_reply(cls, receipts: list[GMToolReceipt]) -> str:
        return cls.locked_public_reply(receipts) or cls.receipt_fallback(receipts)

    @staticmethod
    def allowed_followup_tools(receipts: list[GMToolReceipt]) -> set[str] | None:
        """Return the explicit capability grant from the latest public write.

        ``None`` means no constrained continuation is active. An empty set is
        an explicit terminal grant, while a non-empty set is the complete list
        of tools the agent may call before publishing the locked reply.
        """

        for receipt in reversed(receipts):
            if not receipt.ok or not receipt.lock_public_reply:
                continue
            if "allowed_followup_tools" not in receipt.result:
                return None
            return {
                str(item or "").strip()
                for item in list(receipt.result.get("allowed_followup_tools") or [])
                if str(item or "").strip()
            }
        return None

    @staticmethod
    def required_followup_tools(receipts: list[GMToolReceipt]) -> set[str] | None:
        """Return a mandatory continuation, distinct from an optional grant."""

        for receipt in reversed(receipts):
            if not receipt.ok or not receipt.state_changed:
                continue
            if "required_followup_tools" not in receipt.result:
                # A later successful mutation is the completion point for an
                # earlier preparatory write such as focus_scene_branch.
                return None
            return {
                str(item or "").strip()
                for item in list(receipt.result.get("required_followup_tools") or [])
                if str(item or "").strip()
            }
        return None

    @classmethod
    def interrupted_reply(cls, receipts: list[GMToolReceipt]) -> str:
        return cls.locked_public_reply(receipts) or cls.receipt_fallback(receipts)

    @staticmethod
    def heartbeat_public_change_committed(
        context: GMToolExecutionContext,
        receipt: GMToolReceipt,
    ) -> bool:
        followups = receipt.result.get("required_followup_tools")
        return bool(
            context.metadata.get("system_gm_beat_request")
            and receipt.ok
            and receipt.state_changed
            and receipt.lock_public_reply
            and str(receipt.public_fallback_reply or "").strip()
            and not (isinstance(followups, list) and followups)
        )

    @staticmethod
    def terminal_public_result_ready(receipt: GMToolReceipt) -> bool:
        """Return whether one read-only receipt is a complete public answer."""

        return bool(
            receipt.ok
            and not receipt.state_changed
            and receipt.lock_public_reply
            and receipt.result.get("terminal_public_result") is True
            and str(receipt.public_fallback_reply or "").strip()
        )

    @staticmethod
    def terminal_public_change_committed(
        receipt: GMToolReceipt,
        *,
        terminal_public_tools: frozenset[str],
    ) -> bool:
        required_followups = receipt.result.get("required_followup_tools")
        if isinstance(required_followups, list) and required_followups:
            return False
        if (
            receipt.tool_name in {"decide_npc_response", "decide_collective_response"}
            and receipt.ok
            and receipt.lock_public_reply
            and str(receipt.public_fallback_reply or "").strip()
            and not list(receipt.result.get("allowed_followup_tools") or [])
        ):
            # NPC dialogue is normally one complete table-facing transaction.
            # Continuing the agent loop without an explicit capability grant
            # lets the model ask the same NPC again with paraphrased arguments.
            return True
        if not (
            receipt.tool_name in terminal_public_tools
            and receipt.ok
            and receipt.lock_public_reply
            and str(receipt.public_fallback_reply or "").strip()
        ):
            return False
        pending = receipt.result.get("pending_decisions")
        if isinstance(pending, list) and any(
            isinstance(item, dict) and str(item.get("owner") or "").strip() == "__gm__"
            for item in pending
        ):
            return False
        return True
