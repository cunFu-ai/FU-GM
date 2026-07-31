import asyncio

from fu_gm.components.skill_lifecycle_coordinator import SkillLifecycleOutcome
from fu_gm.components.skill_lifecycle_event_buffer import SkillLifecycleEventBuffer
from fu_gm.components.skill_trigger_manager import SkillEventResult


def outcome(label: str) -> SkillLifecycleOutcome:
    return SkillLifecycleOutcome(
        event=label,
        result=SkillEventResult(),
        records=[{"event": label}],
        windows=[{"window_id": label}],
    )


def test_event_buffer_drains_one_transaction_once() -> None:
    buffer = SkillLifecycleEventBuffer()

    with buffer.transaction():
        buffer.capture(outcome("痛楚"))
        batch = buffer.drain()
        second = buffer.drain()

    assert batch.records == [{"event": "痛楚"}]
    assert batch.windows == [{"window_id": "痛楚"}]
    assert second.records == []
    assert second.windows == []


def test_concurrent_skill_transactions_cannot_cross_contaminate() -> None:
    buffer = SkillLifecycleEventBuffer()

    async def resolve(label: str) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        with buffer.transaction():
            buffer.capture(outcome(label))
            await asyncio.sleep(0)
            batch = buffer.drain()
            return batch.records, batch.windows

    async def run():
        return await asyncio.gather(resolve("灵智回流"), resolve("治愈之力"))

    first, second = asyncio.run(run())

    assert first == ([{"event": "灵智回流"}], [{"window_id": "灵智回流"}])
    assert second == ([{"event": "治愈之力"}], [{"window_id": "治愈之力"}])
