from __future__ import annotations

import re
from copy import deepcopy

from fu_gm.components.scene_change_authority import SceneChangeAuthorityPolicy
from fu_gm.context_governance import GMToolResultBudgeter
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
            "candidate_id",
            "category",
            # Safety declarations and Session 0 hero operations can issue
            # several obligations through the same tool name.  Treat their
            # semantic identity as stable too, otherwise a second ``line``
            # could accidentally satisfy a pending ``veil`` (or a different
            # hero could satisfy the requested confirmation).
            "kind",
            "subject",
            "old_name",
            "new_name",
            "updates",
        }
    )

    _FALLBACK_SUPERSEDES = {
        "confirm_hero_draft": {"update_hero_draft"},
        "confirm_session_zero_proposal": {"propose_session_zero_update"},
        "load_campaign": {"list_saves"},
    }

    _START_SESSION_MODEL_RESULT_KEYS = frozenset(
        {
            "adventure_opening_required",
            "adventure_resumed",
            "resumed_scene",
            "opening_contract",
            "opening_character_state",
            "opening_equipment_restrictions",
            "opening_equipment_instruction",
            "session_situation_contract",
            "allowed_followup_tools",
            "required_followup_tools",
            "required_followup_calls",
            "required_followup_mode",
            "source_event",
        }
    )

    @classmethod
    def model_view(
        cls,
        receipt: GMToolReceipt,
        *,
        max_result_chars: int = 0,
    ) -> dict[str, object]:
        """返回供下一轮模型决策使用的回执视图。

        权威回执仍完整保存在执行账本与审计日志中。这里只去掉下一轮
        决策不需要的存档路径、地图和整场候选场景，避免一次开场工具把
        同一份场次策划重复塞回上下文。
        """

        payload = receipt.to_dict()
        if (
            receipt.ok
            and not receipt.lock_public_reply
            and receipt.result.get("silent_commit_allowed") is True
        ):
            # The fallback is retained on the authoritative receipt for
            # provider-failure recovery. It is not a style example for the
            # normal post-tool model turn.
            payload["public_fallback_reply"] = ""
        result = payload.get("result")
        if not isinstance(result, dict):
            return payload
        if (
            receipt.tool_name == "start_session"
            and receipt.ok
            and bool(result.get("adventure_opening_required"))
        ):
            compact_result = {
                key: deepcopy(value)
                for key, value in result.items()
                if key in cls._START_SESSION_MODEL_RESULT_KEYS
            }
            contract = compact_result.get("session_situation_contract")
            if isinstance(contract, dict):
                compact_result["session_situation_contract"] = (
                    cls._opening_situation_model_view(contract)
                )
            compact_result["model_view_scope"] = "opening_scene"
            result = compact_result
        if max_result_chars > 0:
            result = GMToolResultBudgeter.project(
                result,
                max_chars=max_result_chars,
            ).result
        payload["result"] = result
        return payload

    @staticmethod
    def _opening_situation_model_view(
        contract: dict[str, object],
    ) -> dict[str, object]:
        """只保留建立首场局面所需的场次契约材料。"""

        scenes = [
            deepcopy(item)
            for item in list(contract.get("potential_scenes") or [])
            if isinstance(item, dict)
        ]
        opening_scene = next(
            (
                item
                for item in scenes
                if str(item.get("scene_role") or "").strip() == "strong_start"
            ),
            None,
        )
        if opening_scene is None:
            opening_scene = next(
                (item for item in scenes if item.get("optional") is not True),
                scenes[0] if scenes else None,
            )

        npc_names = {
            str(item or "").strip()
            for item in list((opening_scene or {}).get("npc_names") or [])
            + list((opening_scene or {}).get("required_npc_names") or [])
            if str(item or "").strip()
        }
        clue_ids = {
            str(item or "").strip()
            for item in list((opening_scene or {}).get("clue_route_ids") or [])
            if str(item or "").strip()
        }
        opening_npcs = [
            deepcopy(item)
            for item in list(contract.get("important_npcs") or [])
            if isinstance(item, dict)
            and str(item.get("name") or "").strip() in npc_names
        ]
        opening_clues = [
            deepcopy(item)
            for item in list(contract.get("clue_routes") or [])
            if isinstance(item, dict)
            and str(item.get("route_id") or "").strip() in clue_ids
        ]

        keep = {
            "title",
            "location",
            "dramatic_question",
            "opening_disruption",
            "signature_image",
            "opposition_goal",
            "dilemma",
            "reversal",
            "closure_requirement",
            "irreversible_change",
            "ending_echo",
            "situation_facts",
            "flexible_secrets",
            "opening_equipment_restrictions",
            "escalation_ladder",
            "possible_payoffs",
            "instruction",
        }
        result = {
            key: deepcopy(value)
            for key, value in contract.items()
            if key in keep
        }
        result["opening_scene"] = opening_scene or {}
        result["opening_scene_npcs"] = opening_npcs
        result["opening_scene_clues"] = opening_clues
        return result

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
        if receipt.tool_name == "discover_capabilities":
            # Tool execution binds mutating calls to a source-event context.
            # That bound context is intentionally a copy, so transaction-local
            # capability grants must cross back through the authoritative
            # receipt rather than relying on a handler-side metadata mutation.
            granted = {
                str(item or "").strip()
                for item in list(result.get("all_granted_tool_names") or [])
                if str(item or "").strip()
            }
            if granted:
                current = {
                    str(item or "").strip()
                    for item in list(
                        context.metadata.get("gm_discovered_tool_names") or []
                    )
                    if str(item or "").strip()
                }
                context.metadata["gm_discovered_tool_names"] = sorted(
                    current | granted
                )
        cls._remember_committed_actions(context, result)
        SceneChangeAuthorityPolicy.remember_receipt_authorities(context, receipt)
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
        scene_response_followup = (
            SceneChangeAuthorityPolicy.normalized_scene_response_followup(
                result.get("scene_response_followup")
            )
        )
        if "commit_scene_response" in declared and scene_response_followup is None:
            # A deferred world-response label is an obligation, not permission
            # to invent its outcome.  Only a source tool that already committed
            # an exact public result may open the free-text delivery tool.
            declared = [
                name for name in declared if name != "commit_scene_response"
            ]
            result["required_followup_tools"] = list(declared)
            if isinstance(result.get("allowed_followup_tools"), list):
                result["allowed_followup_tools"] = [
                    str(item or "").strip()
                    for item in list(result.get("allowed_followup_tools") or [])
                    if str(item or "").strip() != "commit_scene_response"
                ]
            if isinstance(result.get("required_followup_calls"), list):
                result["required_followup_calls"] = [
                    deepcopy(item)
                    for item in list(result.get("required_followup_calls") or [])
                    if isinstance(item, dict)
                    and str(item.get("tool_name") or "").strip()
                    != "commit_scene_response"
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
        had_active_followup = bool(active_tools)

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
            if scene_response_followup is not None:
                active["scene_response_followup"] = deepcopy(
                    scene_response_followup
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
            if had_active_followup:
                # A valid follow-up may intentionally be read-only.  For
                # example, passing in a free scene without an automatic clock
                # does not mutate campaign state, but it still fulfils the
                # preparatory focus operation.  Leave an explicit terminal
                # marker on this receipt so receipt-list consumers do not
                # rediscover the stale obligation on an earlier write.
                result["required_followup_tools"] = []
                result["required_followup_calls"] = []
                result["required_followup_mode"] = "any"
                result["required_followup_resolved"] = True

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
            if not receipt.ok:
                continue
            if receipt.result.get("required_followup_resolved") is True:
                return []
            if "required_followup_calls" not in receipt.result:
                if receipt.state_changed:
                    return []
                continue
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

    @classmethod
    def state_change_recovered_with_player_input_blocker(
        cls,
        receipts: list[GMToolReceipt],
    ) -> bool:
        """Allow independent writes to commit when a later request must wait.

        This is intentionally stricter than ``state_change_recovered``.  A
        failed tool only becomes a terminal, deliverable result when its
        authoritative receipt explicitly says that progress now depends on a
        player-owned choice.  Provider failures, malformed calls and ordinary
        rule rejections still force the whole message transaction to roll back.
        """

        successful_write_indexes = [
            index
            for index, receipt in enumerate(receipts)
            if receipt.ok and receipt.state_changed
        ]
        if not successful_write_indexes or cls.required_followup_tools(receipts):
            return False
        trailing = receipts[successful_write_indexes[-1] + 1 :]
        if not trailing:
            return False
        return all(
            receipt.ok or cls._is_terminal_player_input_blocker(receipt)
            for receipt in trailing
        ) and any(cls._is_terminal_player_input_blocker(receipt) for receipt in trailing)

    @staticmethod
    def _is_terminal_player_input_blocker(receipt: GMToolReceipt) -> bool:
        return bool(
            not receipt.ok
            and not receipt.retryable
            and not receipt.state_changed
            and receipt.error_code
            and receipt.result.get("player_input_required") is True
            and str(receipt.public_fallback_reply or "").strip()
        )

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
                and (
                    (receipt.ok and receipt.state_changed)
                    or (not receipt.ok and not receipt.retryable)
                )
            ):
                return receipt.public_fallback_reply
        return ""

    @staticmethod
    def locked_public_reply(receipts: list[GMToolReceipt]) -> str:
        effective_receipts = [
            receipt
            for index, receipt in enumerate(receipts)
            if not (
                receipt.ok
                and receipt.lock_public_reply
                and not receipt.state_changed
                and any(
                    later.ok
                    and later.state_changed
                    and later.result.get("rolled_back") is not True
                    for later in receipts[index + 1 :]
                )
            )
        ]
        declared_state_lines: set[str] = set()
        latest_state_lines: list[str] = []
        for receipt in effective_receipts:
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
        for receipt in effective_receipts:
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
            normalized_candidate = re.sub(r"\s+", "", candidate)
            if any(
                normalized_candidate == re.sub(r"\s+", "", existing)
                or normalized_candidate in re.sub(r"\s+", "", existing)
                for existing in replies
            ):
                continue
            containing = next(
                (
                    index
                    for index, existing in enumerate(replies)
                    if re.sub(r"\s+", "", existing) in normalized_candidate
                ),
                None,
            )
            if containing is not None:
                replies[containing] = candidate
            else:
                replies.append(candidate)
        # One player message may resolve several authoritative tools in order
        # (for example: close a zero-HP window, then resume the deferred action).
        # Each resolution can observe the same next actor and append the same
        # held-action handoff.  Keep every distinct outcome, but publish an
        # identical line only once across the whole transaction.
        candidate_lines: list[tuple[str, str]] = []
        for block in [*replies, *latest_state_lines]:
            for line in str(block or "").splitlines():
                rendered = line.strip()
                normalized = re.sub(r"\s+", "", rendered)
                if not rendered or not normalized:
                    continue
                candidate_lines.append((rendered, normalized))
        last_occurrence = {
            normalized: index
            for index, (_rendered, normalized) in enumerate(candidate_lines)
        }
        merged_lines = [
            rendered
            for index, (rendered, normalized) in enumerate(candidate_lines)
            if last_occurrence[normalized] == index
        ]
        return "\n".join(merged_lines)

    @classmethod
    def authoritative_reply(cls, receipts: list[GMToolReceipt]) -> str:
        return cls.locked_public_reply(receipts) or cls.receipt_fallback(receipts)

    @staticmethod
    def mixed_message_followup_pending(
        receipts: list[GMToolReceipt],
    ) -> bool:
        """Return whether a committed rules choice still has a prose question.

        The flag lives on the receipt rather than campaign state because it is
        an obligation of the current message transaction, not a new table
        decision.  Failure handling and terminal reply validation both consult
        this helper so a committed rule result cannot silently swallow the
        player's second question.
        """

        return any(
            receipt.ok
            and receipt.state_changed
            and receipt.result.get("mixed_message_followup_pending") is True
            for receipt in receipts
        )

    @staticmethod
    def natural_resolution_pending(
        receipts: list[GMToolReceipt],
    ) -> bool:
        """Return whether a committed fact still needs table-facing prose.

        The authoritative fallback remains on the receipt for provider failure
        recovery.  This flag only prevents that audit text from being read
        aloud verbatim during the normal post-tool path.
        """

        return any(
            receipt.ok
            and receipt.state_changed
            and receipt.result.get("natural_resolution_pending") is True
            for receipt in receipts
        )

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
            if not receipt.ok:
                continue
            if receipt.result.get("required_followup_resolved") is True:
                return None
            if "required_followup_tools" not in receipt.result:
                # A later successful mutation is the completion point for an
                # earlier preparatory write when apply_context was not used
                # (for example in extension registries and direct policy
                # tests). Read-only receipts only complete it when they carry
                # the explicit resolved marker above.
                if receipt.state_changed:
                    return None
                continue
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
    def public_material_change_committed(receipt: GMToolReceipt) -> bool:
        """Return whether one receipt proves a deliverable public change.

        ``state_changed`` alone is deliberately insufficient here.  A system
        beat may prepare a private NPC sheet or touch an unchanged runtime
        field; neither operation authorizes the model to narrate a new scene
        event.  A material beat is complete only when the authoritative tool
        has also locked a non-empty public result and has no mandatory
        follow-up left unresolved.
        """

        followups = receipt.result.get("required_followup_tools")
        return bool(
            receipt.ok
            and receipt.state_changed
            and receipt.lock_public_reply
            and str(receipt.public_fallback_reply or "").strip()
            and not (isinstance(followups, list) and followups)
        )

    @staticmethod
    def heartbeat_public_change_committed(
        context: GMToolExecutionContext,
        receipt: GMToolReceipt,
    ) -> bool:
        return bool(
            context.metadata.get("system_gm_beat_request")
            and GMToolReceiptPolicy.public_material_change_committed(receipt)
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
            receipt.tool_name
            in {
                "decide_npc_response",
                "decide_collective_response",
            }
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
