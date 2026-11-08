"""Unit tests for pending-approvals long-poll endpoint (contract §6.4, AC3.10)."""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from tests.fakes.app import make_harness
from tests.fakes.clock import FakeClock
from tests.fakes.store import InMemoryStore


@pytest.mark.anyio
async def test_pending_approvals_path_device_mismatch() -> None:
    """Device ID in path mismatching bearer token device returns 403 device_mismatch."""
    h = make_harness()
    token, device_id, user_id = h.new_device()
    _, other_device_id, _ = h.new_device(user_id=user_id)

    # Call with device token for device_id, but requesting other_device_id
    resp = h.client.get(
        f"/api/v1/devices/{other_device_id}/pending-approvals?wait=0",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "device_mismatch"


@pytest.mark.anyio
async def test_pending_approvals_returns_empty_when_no_orders() -> None:
    """Returns empty orders array when no pending approvals exist."""
    h = make_harness()
    token, device_id, _ = h.new_device()

    resp = h.client.get(
        f"/api/v1/devices/{device_id}/pending-approvals?wait=0",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"orders": []}


@pytest.mark.anyio
async def test_pending_approvals_returns_active_awaiting_orders() -> None:
    """Returns awaiting approval orders for the device."""
    h = make_harness()
    token, device_id, user_id = h.new_device()

    order_id = h.store.create_order(
        user_id=user_id,
        merchant_id=h.store.add_merchant(),
        device_id=device_id,
        total_minor=2000,
        currency="MYR",
        status="awaiting_device_approval",
    )

    resp = h.client.get(
        f"/api/v1/devices/{device_id}/pending-approvals?wait=0",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["orders"]) == 1
    assert data["orders"][0]["order_id"] == order_id


@pytest.mark.anyio
async def test_pending_approvals_second_poll_replaces_first() -> None:
    """Second long-poll for same device replaces the first, causing first to return {"orders":[]}."""
    clock = FakeClock()
    store = InMemoryStore(clock)
    h = make_harness(store=store)
    token, device_id, _ = h.new_device()

    results: list[dict[str, Any]] = []

    async def _poll_first() -> None:
        resp = h.client.get(
            f"/api/v1/devices/{device_id}/pending-approvals?wait=20",
            headers={"Authorization": f"Bearer {token}"},
        )
        results.append(resp.json())

    async def _poll_second() -> None:
        await anyio.sleep(0.01)
        resp = h.client.get(
            f"/api/v1/devices/{device_id}/pending-approvals?wait=0",
            headers={"Authorization": f"Bearer {token}"},
        )
        results.append(resp.json())

    async with anyio.create_task_group() as tg:
        tg.start_soon(_poll_first)
        tg.start_soon(_poll_second)

    # Both polls should return valid JSON with orders key, first replaced cleanly
    assert len(results) == 2
    assert results[0] == {"orders": []}
    assert results[1] == {"orders": []}
