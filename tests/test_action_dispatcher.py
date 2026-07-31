import pytest

from fu_gm.components.action_dispatcher import ActionDispatcher
from fu_gm.models import Action, ActionResolution, ActionType


def test_dispatcher_routes_registered_action() -> None:
    dispatcher = ActionDispatcher()
    dispatcher.register(
        ActionType.NARRATE,
        lambda action: ActionResolution(action=action, rules_text="ok", payload={}),
    )

    result = dispatcher.dispatch(Action(ActionType.NARRATE, {"summary": "继续"}))

    assert result is not None
    assert result.rules_text == "ok"
    assert dispatcher.registered_types == (ActionType.NARRATE,)


def test_dispatcher_rejects_duplicate_registration() -> None:
    dispatcher = ActionDispatcher()
    handler = lambda action: ActionResolution(action=action, rules_text="", payload={})
    dispatcher.register(ActionType.NARRATE, handler)

    with pytest.raises(ValueError, match="重复注册"):
        dispatcher.register(ActionType.NARRATE, handler)


def test_dispatcher_returns_none_for_extension_action_without_handler() -> None:
    assert ActionDispatcher().dispatch(Action(ActionType.NARRATE, {})) is None
