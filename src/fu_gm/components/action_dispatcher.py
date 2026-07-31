from __future__ import annotations

from collections.abc import Callable

from fu_gm.models import Action, ActionResolution, ActionType


ActionHandler = Callable[[Action], ActionResolution]


class ActionDispatcher:
    """Explicit registry from validated action types to rule handlers."""

    def __init__(self) -> None:
        self._handlers: dict[ActionType, ActionHandler] = {}

    def register(self, action_type: ActionType, handler: ActionHandler) -> None:
        if action_type in self._handlers:
            raise ValueError(f"重复注册动作处理器：{action_type.value}")
        self._handlers[action_type] = handler

    def register_many(self, handlers: dict[ActionType, ActionHandler]) -> None:
        for action_type, handler in handlers.items():
            self.register(action_type, handler)

    def dispatch(self, action: Action) -> ActionResolution | None:
        handler = self._handlers.get(action.action_type)
        return handler(action) if handler is not None else None

    @property
    def registered_types(self) -> tuple[ActionType, ...]:
        return tuple(self._handlers)
