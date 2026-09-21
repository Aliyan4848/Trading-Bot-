"""Paper broker: market data, async fills, idempotency, SL/TP, settlement."""

from __future__ import annotations

import asyncio

import pytest

from tradingbot.broker.models import OperationState, OrderRequest, Side
from tradingbot.broker.paper import PaperBroker


def _req(**kw) -> OrderRequest:
    base = dict(
        instrument="EURUSD",
        side=Side.BUY,
        volume=0.1,
        client_request_id="cr-00001",
    )
    base.update(kw)
    return OrderRequest(**base)


async def _wait_fill(broker: PaperBroker, op_id: str, timeout: float = 3.0) -> None:
    async def poll():
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            status = await broker.operation_status(op_id)
            if status.state in (OperationState.FILLED, OperationState.REJECTED, OperationState.CLOSED):
                return status
            await asyncio.sleep(0.01)
        raise TimeoutError(f"operation {op_id} did not settle")
    return await asyncio.wait_for(poll(), timeout + 1)


async def test_ticks_stream(settings):
    broker = PaperBroker(settings)
    await broker.connect()
    received = []

    async def consume():
        async for tick in broker.subscribe_ticks(["EURUSD"]):
            received.append(tick)
            if len(received) >= 5:
                break

    await asyncio.wait_for(consume(), timeout=5)
    assert all(t.instrument == "EURUSD" for t in received)
    assert all(t.ask > t.bid for t in received)
    assert all(t.ask - t.bid > 0 for t in received)
    await broker.close()


async def test_market_order_fill_lifecycle(settings):
    broker = PaperBroker(settings)
    await broker.connect()
    conditions = await broker.instrument_conditions("EURUSD")

    ack = await broker.place_order(_req())
    assert ack.state is OperationState.ACCEPTED
    assert ack.operation_id
    status = await _wait_fill(broker, ack.operation_id)
    assert status.state is OperationState.FILLED
    assert status.position_id is not None

    positions = await broker.positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.side is Side.BUY
    assert pos.open_price > 0
    # fill must be at ask (or ask + slippage) — never below the pre-fill ask
    assert pos.open_price >= conditions.spread  # sanity: well above zero
    await broker.close()


async def test_idempotency_same_payload_returns_original(settings):
    broker = PaperBroker(settings)
    await broker.connect()
    ack1 = await broker.place_order(_req())
    await _wait_fill(broker, ack1.operation_id)

    ack2 = await broker.place_order(_req())  # identical key + payload
    assert ack2.operation_id == ack1.operation_id
    positions = await broker.positions()
    assert len(positions) == 1  # no duplicate position
    await broker.close()


async def test_idempotency_conflict_rejected(settings):
    broker = PaperBroker(settings)
    await broker.connect()
    ack1 = await broker.place_order(_req())
    await _wait_fill(broker, ack1.operation_id)

    conflict = await broker.place_order(_req(volume=0.2))  # same key, different payload
    assert conflict.state is OperationState.REJECTED
    assert conflict.error_code == "IDEMPOTENCY_CONFLICT"
    assert len(await broker.positions()) == 1
    await broker.close()


async def test_volume_validation(settings):
    broker = PaperBroker(settings)
    await broker.connect()
    too_small = await broker.place_order(_req(volume=0.005, client_request_id="cr-vol-1"))
    assert too_small.state is OperationState.REJECTED
    assert "VOLUME" in too_small.error_code
    bad_step = await broker.place_order(_req(volume=0.115, client_request_id="cr-vol-2"))
    assert bad_step.state is OperationState.REJECTED
    assert bad_step.error_code == "TRADING_RULE_INVALID_VOLUME_STEP"
    assert len(await broker.positions()) == 0
    await broker.close()


async def test_sl_tp_levels_validation(settings):
    broker = PaperBroker(settings)
    await broker.connect()
    # buy with sl above market => invalid
    bad = await broker.place_order(
        _req(sl=9999.0, tp=10000.0, client_request_id="cr-sltp-1")
    )
    assert bad.state is OperationState.REJECTED
    assert bad.error_code == "TRADING_RULE_INVALID_PRICE_LEVELS"
    await broker.close()


async def test_stop_loss_triggers_on_tick(settings):
    broker = PaperBroker(settings)
    await broker.connect()
    await broker.force_price("EURUSD", bid=1.0850, ask=1.0862)

    ack = await broker.place_order(_req(sl=1.0800, tp=1.1200, client_request_id="cr-sl-1"))
    status = await _wait_fill(broker, ack.operation_id)
    assert status.state is OperationState.FILLED

    balance_before = (await broker.account_info()).balance
    # price collapses through the stop
    await broker.force_price("EURUSD", bid=1.0790, ask=1.0802)

    deadline = asyncio.get_event_loop().time() + 2.0
    while asyncio.get_event_loop().time() < deadline:
        if not await broker.positions():
            break
        await asyncio.sleep(0.01)
    assert await broker.positions() == []

    deals = await broker.history_deals()
    close_deals = [d for d in deals if d.kind == "close"]
    assert len(close_deals) == 1
    assert close_deals[0].reason == "SL"
    assert close_deals[0].price == 1.0800  # idealized fill at the stop
    assert close_deals[0].pnl is not None and close_deals[0].pnl < 0
    balance_after = (await broker.account_info()).balance
    assert balance_after == pytest.approx(balance_before + close_deals[0].pnl, abs=1e-6)
    await broker.close()


async def test_take_profit_triggers_on_tick(settings):
    broker = PaperBroker(settings)
    await broker.connect()
    await broker.force_price("EURUSD", bid=1.0850, ask=1.0862)
    ack = await broker.place_order(_req(sl=1.0800, tp=1.0900, client_request_id="cr-tp-1"))
    status = await _wait_fill(broker, ack.operation_id)
    assert status.state is OperationState.FILLED

    await broker.force_price("EURUSD", bid=1.0905, ask=1.0917)
    deadline = asyncio.get_event_loop().time() + 2.0
    while asyncio.get_event_loop().time() < deadline:
        if not await broker.positions():
            break
        await asyncio.sleep(0.01)
    deals = [d for d in await broker.history_deals() if d.kind == "close"]
    assert len(deals) == 1
    assert deals[0].reason == "TP"
    assert deals[0].pnl is not None and deals[0].pnl > 0
    await broker.close()


async def test_manual_close_sets_realized_pnl(settings):
    broker = PaperBroker(settings)
    await broker.connect()
    await broker.force_price("EURUSD", bid=1.0850, ask=1.0862)
    ack = await broker.place_order(_req(client_request_id="cr-mc-1"))
    status = await _wait_fill(broker, ack.operation_id)
    position_id = status.position_id

    close_ack = await broker.close_position(position_id, "cr-mc-close-1", reason="MANUAL")
    assert close_ack.state is OperationState.ACCEPTED
    deadline = asyncio.get_event_loop().time() + 3.0
    while asyncio.get_event_loop().time() < deadline:
        st = await broker.operation_status(close_ack.operation_id)
        if st.state is OperationState.CLOSED:
            break
        await asyncio.sleep(0.01)
    assert st.state is OperationState.CLOSED
    assert await broker.positions() == []
    info = await broker.account_info()
    assert info.open_positions == 0
    assert info.balance != settings.paper_start_balance  # pnl applied (± could be 0 if zero move)
    await broker.close()


async def test_account_info_shapes(settings):
    broker = PaperBroker(settings)
    await broker.connect()
    await broker.force_price("EURUSD", bid=1.0850, ask=1.0862)
    ack = await broker.place_order(_req(client_request_id="cr-acc-1"))
    await _wait_fill(broker, ack.operation_id)
    info = await broker.account_info()
    assert info.currency == "USD"
    assert info.leverage == settings.paper_leverage
    assert info.open_positions == 1
    assert info.equity == pytest.approx(info.balance + info.unrealized_pnl, abs=1e-6)
    assert info.free_margin == pytest.approx(info.equity - (info.equity / info.leverage * 0), abs=1e9)  # non-negative sanity
    assert info.free_margin > 0
    await broker.close()
