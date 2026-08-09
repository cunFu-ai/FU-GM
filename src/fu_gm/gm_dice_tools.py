from __future__ import annotations

from typing import Any, Protocol

from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
)


class DiceToolHost(Protocol):
    def _runtime(self, campaign_id: str) -> Any: ...

    def _autosave_campaign(self, runtime: Any, campaign_id: str) -> str: ...


class GMDiceToolService:
    """Authoritative public dice for table randomization.

    Character checks, attacks, travel and other rules procedures keep using
    their dedicated tools. This service covers an explicit tabletop roll or a
    random-table choice whose mapping must be fixed before the die is rolled.
    """

    _SELECTION_CONTEXTS = ("none", "first_act")

    def __init__(self, host: DiceToolHost) -> None:
        self.host = host

    def register_tools(self, registry: GMToolRegistry) -> None:
        registry.register(
            GMToolDefinition(
                name="roll_dice",
                description=(
                    "进行公开且不可由模型改写的桌面掷骰。适用于玩家明确要求掷骰、"
                    "或GM需要按预先列好的随机表作选择；属性检定、攻击、旅行等已有"
                    "专用规则流程不得改用本工具。普通掷骰填写骰数与面数；按候选项"
                    "随机时先完整提交choices；按当前第一幕候选随机时使用"
                    "selection_context=first_act，由工具读取权威候选、掷骰并精确写入本次结果；"
                    "玩家之后明确改变共识时仍可正常改选。"
                ),
                handler=self.roll_dice,
                parameters=(
                    GMToolParameter(
                        "purpose",
                        "string",
                        "这次公开掷骰要决定什么；简短描述即可。",
                        required=True,
                        schema_details={"minLength": 1, "maxLength": 160},
                    ),
                    GMToolParameter(
                        "dice_count",
                        "integer",
                        "骰子数量，默认1，范围1到20。",
                        schema_details={"minimum": 1, "maximum": 20},
                    ),
                    GMToolParameter(
                        "die_size",
                        "integer",
                        "骰子面数，默认6，范围2到1000。候选选择时应等于候选数量。",
                        schema_details={"minimum": 2, "maximum": 1000},
                    ),
                    GMToolParameter(
                        "modifier",
                        "integer",
                        "结果修正值，默认0，范围-1000到1000。候选选择时必须为0。",
                        schema_details={"minimum": -1000, "maximum": 1000},
                    ),
                    GMToolParameter(
                        "choices",
                        "array",
                        "可选。掷骰前固定的候选表，骰面1开始依次对应数组顺序。",
                        schema_details={
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string", "minLength": 1},
                                    "label": {"type": "string", "minLength": 1},
                                },
                                "required": ["id", "label"],
                                "additionalProperties": False,
                            },
                            "minItems": 2,
                            "maxItems": 100,
                        },
                    ),
                    GMToolParameter(
                        "selection_context",
                        "string",
                        "none为普通掷骰或自定义候选；first_act为从当前权威第一幕候选中随机选择并落档。",
                        enum=self._SELECTION_CONTEXTS,
                    ),
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )

    def roll_dice(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        purpose = str(arguments.get("purpose") or "").strip()
        dice_count = int(arguments.get("dice_count") or 1)
        die_size = int(arguments.get("die_size") or 6)
        modifier = int(arguments.get("modifier") or 0)
        selection_context = str(
            arguments.get("selection_context") or "none"
        ).strip()
        raw_choices = list(arguments.get("choices") or [])

        runtime = self.host._runtime(context.campaign_id)
        choices: list[dict[str, str]] = []
        required_followup_tools: list[str] = []
        required_followup_calls: list[dict[str, object]] = []
        next_question = ""

        if selection_context == "first_act":
            if raw_choices:
                return self._failure(
                    "FIRST_ACT_CHOICES_ARE_AUTHORITATIVE",
                    "第一幕候选必须来自当前第零章状态，不能另行提交choices。",
                    "删除choices并保留selection_context=first_act后重试。",
                )
            world = runtime.app.session_zero_manager.state.world
            choices = [
                {
                    "id": str(candidate.candidate_id),
                    "label": str(candidate.title),
                }
                for candidate in world.first_act_candidates
                if str(candidate.candidate_id or "").strip()
                and str(candidate.title or "").strip()
            ]
            if len(choices) < 2:
                return self._failure(
                    "FIRST_ACT_CANDIDATES_UNAVAILABLE",
                    "当前没有足够的第一幕候选可供掷骰。",
                    "先完成世界、小队与角色共创，让第零章生成第一幕候选后再掷骰。",
                )
            if dice_count != 1 or modifier != 0:
                return self._failure(
                    "SELECTION_REQUIRES_SINGLE_UNMODIFIED_DIE",
                    "随机选择第一幕必须使用一枚不带修正的骰子。",
                    "使用dice_count=1、modifier=0；die_size省略或等于候选数量。",
                )
            if "die_size" in arguments and die_size != len(choices):
                return self._failure(
                    "SELECTION_DIE_SIZE_MISMATCH",
                    "骰子面数与当前第一幕候选数量不一致。",
                    f"删除die_size或改为{len(choices)}。",
                )
            die_size = len(choices)
        elif raw_choices:
            choices = [
                {
                    "id": str(item.get("id") or "").strip(),
                    "label": str(item.get("label") or "").strip(),
                }
                for item in raw_choices
                if isinstance(item, dict)
            ]
            if len({item["id"] for item in choices}) != len(choices):
                return self._failure(
                    "DUPLICATE_CHOICE_ID",
                    "随机表中的候选ID必须互不相同。",
                    "为每个choices条目提供唯一id后重试。",
                )
            if dice_count != 1 or modifier != 0:
                return self._failure(
                    "SELECTION_REQUIRES_SINGLE_UNMODIFIED_DIE",
                    "候选选择必须使用一枚不带修正的骰子。",
                    "使用dice_count=1、modifier=0；die_size省略或等于候选数量。",
                )
            if "die_size" in arguments and die_size != len(choices):
                return self._failure(
                    "SELECTION_DIE_SIZE_MISMATCH",
                    "骰子面数与候选数量不一致。",
                    f"删除die_size或改为{len(choices)}。",
                )
            die_size = len(choices)

        with runtime.transaction_lock:
            rolls = [
                runtime.app.interceptor.rules_engine.roll_die(die_size)
                for _ in range(dice_count)
            ]
            total = sum(rolls) + modifier
            selected_choice = choices[rolls[0] - 1] if choices else None

            if selection_context == "first_act" and selected_choice is not None:
                candidate = next(
                    (
                        item
                        for item in runtime.app.session_zero_manager.state.world.first_act_candidates
                        if item.candidate_id == selected_choice["id"]
                    ),
                    None,
                )
                if candidate is not None and candidate.questions:
                    next_question = str(candidate.questions[0] or "").strip()
                required_followup_tools = ["commit_session_zero_update"]
                required_followup_calls = [
                    {
                        "tool_name": "commit_session_zero_update",
                        "arguments": {
                            "updates": {
                                "selected_first_act_id": selected_choice["id"],
                            }
                        },
                    }
                ]

            saved_path = self.host._autosave_campaign(
                runtime,
                context.campaign_id,
            )

        notation = self._notation(dice_count, die_size, modifier)
        public_reply = self._public_reply(
            purpose=purpose,
            notation=notation,
            rolls=rolls,
            total=total,
            modifier=modifier,
            choice_map=choices,
            selected_choice=selected_choice,
            first_act=selection_context == "first_act",
            next_question=next_question,
        )
        result: dict[str, object] = {
            "purpose": purpose,
            "notation": notation,
            "dice_count": dice_count,
            "die_size": die_size,
            "modifier": modifier,
            "rolls": rolls,
            "total": total,
            "selection_context": selection_context,
            "choice_map": choices,
            "selected_index": rolls[0] if selected_choice is not None else 0,
            "selected_choice": selected_choice or {},
            "saved_path": saved_path,
        }
        if required_followup_tools:
            result.update(
                {
                    "allowed_followup_tools": list(required_followup_tools),
                    "required_followup_tools": list(required_followup_tools),
                    "required_followup_calls": list(required_followup_calls),
                    "required_followup_mode": "all",
                }
            )
        return GMToolReceipt.success(
            "roll_dice",
            result=result,
            state_changed=True,
            public_reply=public_reply,
            lock_public_reply=True,
        )

    @staticmethod
    def _notation(dice_count: int, die_size: int, modifier: int) -> str:
        notation = f"{dice_count}d{die_size}"
        if modifier > 0:
            return f"{notation}+{modifier}"
        if modifier < 0:
            return f"{notation}{modifier}"
        return notation

    @staticmethod
    def _public_reply(
        *,
        purpose: str,
        notation: str,
        rolls: list[int],
        total: int,
        modifier: int,
        choice_map: list[dict[str, str]],
        selected_choice: dict[str, str] | None,
        first_act: bool,
        next_question: str,
    ) -> str:
        if selected_choice is not None:
            mapping = "、".join(
                f"{index}【{item['label']}】"
                for index, item in enumerate(
                    choice_map,
                    start=1,
                )
            )
            prefix = f"候选顺序是{mapping}。" if mapping else ""
            if first_act:
                reply = (
                    f"{prefix}我掷出了{notation}={rolls[0]}，"
                    f"对应【{selected_choice['label']}】。"
                )
            else:
                reply = (
                    f"{prefix}我为“{purpose}”掷出了{notation}={rolls[0]}，"
                    f"对应【{selected_choice['label']}】。"
                )
            if first_act:
                reply += (
                    f"这次随机结果就先定为这个开场。{next_question}"
                    if next_question
                    else "这次随机结果就先定为这个开场。"
                )
            return reply
        roll_text = "、".join(str(value) for value in rolls)
        if len(rolls) == 1 and modifier == 0:
            return f"为“{purpose}”掷骰：{notation}={rolls[0]}。"
        return (
            f"为“{purpose}”掷骰：{notation}，"
            f"骰面为{roll_text}，合计{total}。"
        )

    @staticmethod
    def _failure(code: str, message: str, hint: str) -> GMToolReceipt:
        return GMToolReceipt.failure(
            "roll_dice",
            code,
            message,
            hint,
        )
