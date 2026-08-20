from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys


def _load_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "integrations"
        / "astrbot"
        / "fu_gm_bridge"
        / "request_coordinator.py"
    )
    spec = importlib.util.spec_from_file_location(
        "fu_gm_bridge_request_coordinator",
        path,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


request_coordinator = _load_module()


def test_same_channel_requests_are_serialized_and_lane_is_pruned() -> None:
    async def scenario() -> None:
        coordinator = request_coordinator.ChannelRequestCoordinator()
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        order: list[str] = []

        async def first() -> str:
            order.append("first-start")
            first_entered.set()
            await release_first.wait()
            order.append("first-end")
            return "first"

        async def second() -> str:
            order.append("second-start")
            order.append("second-end")
            return "second"

        first_task = asyncio.create_task(coordinator.run("group-1", first))
        await first_entered.wait()
        second_task = asyncio.create_task(coordinator.run("group-1", second))
        await asyncio.sleep(0)
        assert order == ["first-start"]

        release_first.set()
        assert await first_task == "first"
        assert await second_task == "second"
        assert order == [
            "first-start",
            "first-end",
            "second-start",
            "second-end",
        ]
        assert coordinator.lane_count() == 0

    asyncio.run(scenario())


def test_different_channels_can_run_concurrently() -> None:
    async def scenario() -> None:
        coordinator = request_coordinator.ChannelRequestCoordinator()
        both_entered = asyncio.Event()
        entered: set[str] = set()

        async def request(name: str) -> str:
            entered.add(name)
            if len(entered) == 2:
                both_entered.set()
            await both_entered.wait()
            return name

        results = await asyncio.gather(
            coordinator.run("group-1", lambda: request("one")),
            coordinator.run("group-2", lambda: request("two")),
        )
        assert results == ["one", "two"]
        assert coordinator.lane_count() == 0

    asyncio.run(scenario())


def test_same_channel_gate_waits_for_platform_delivery_before_next_request() -> None:
    async def scenario() -> None:
        coordinator = request_coordinator.ChannelTurnGate()
        response_ready = asyncio.Event()
        release_delivery = asyncio.Event()
        order: list[str] = []

        async def first_turn() -> str:
            order.append("first-request")
            response_ready.set()
            order.append("first-delivery-start")
            await release_delivery.wait()
            order.append("first-delivery-confirmed")
            return "first"

        async def second_turn() -> str:
            order.append("second-request")
            return "second"

        first_task = asyncio.create_task(coordinator.run("group-1", first_turn))
        await response_ready.wait()
        second_task = asyncio.create_task(coordinator.run("group-1", second_turn))
        await asyncio.sleep(0)
        assert order == ["first-request", "first-delivery-start"]

        release_delivery.set()
        assert await first_task == "first"
        assert await second_task == "second"
        assert order == [
            "first-request",
            "first-delivery-start",
            "first-delivery-confirmed",
            "second-request",
        ]

    asyncio.run(scenario())
