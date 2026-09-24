import asyncio
import pytest
from mcp_hub.concurrency import ConcurrencyGuard


@pytest.mark.asyncio
async def test_exclusive_serializes_calls():
    guard = ConcurrencyGuard("exclusive")
    order: list[str] = []

    async def slow(tag: str):
        order.append(f"{tag}-start")
        await asyncio.sleep(0.05)
        order.append(f"{tag}-end")
        return tag

    await asyncio.gather(guard.run(lambda: slow("a")), guard.run(lambda: slow("b")))
    # second call must not start until the first has ended
    assert order == ["a-start", "a-end", "b-start", "b-end"] or order == ["b-start", "b-end", "a-start", "a-end"]


@pytest.mark.asyncio
async def test_parallel_does_not_serialize():
    guard = ConcurrencyGuard("parallel")
    order: list[str] = []

    async def slow(tag: str):
        order.append(f"{tag}-start")
        await asyncio.sleep(0.05)
        order.append(f"{tag}-end")
        return tag

    await asyncio.gather(guard.run(lambda: slow("a")), guard.run(lambda: slow("b")))
    # both starts happen before either end, proving no serialization
    assert order[0].endswith("-start") and order[1].endswith("-start")


@pytest.mark.asyncio
async def test_run_returns_the_coroutine_result():
    guard = ConcurrencyGuard("exclusive")
    result = await guard.run(lambda: asyncio.sleep(0, result="value"))
    assert result == "value"
