"""Unit tests for spending policy and daily spending limits (§3.5, §3.6, AC3.3).

Covers:
- RM500 daily spending limit boundary (50000 accepted, 50001 rejected).
- Capacity release on order cancellation and quote expiry (reopen).
- Multi-currency rejection (currency_mismatch / currency_not_supported).
- Daily spending day rollover in Asia/Kuala_Lumpur timezone.
- SpendingService calculations: summary, remaining_minor clamp, and tool_summary.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.core.config import Settings
from app.services.spending_service import SpendingService
from tests.fakes.app import make_harness
from tests.fakes.clock import FakeClock
from tests.fakes.store import InMemoryStore

KL_TZ = "Asia/Kuala_Lumpur"


def test_spending_cap_exact_50000_boundary() -> None:
    """committed + reserved = 50000 minor is accepted; 50001 is rejected."""
    store = InMemoryStore()
    user_id = store.add_user()
    approvals = store.repositories().approvals

    # 1. First reservation: 49000 minor -> OK
    ok, reason = approvals._reserve_spending(
        user_id=user_id,
        order_id=str(uuid4()),
        amount_minor=49000,
        currency="MYR",
        daily_limit_minor=50000,
        timezone=KL_TZ,
    )
    assert ok is True
    assert reason == "reserved"

    # 2. Second reservation: 1000 minor (total = 50000) -> OK
    ok, reason = approvals._reserve_spending(
        user_id=user_id,
        order_id=str(uuid4()),
        amount_minor=1000,
        currency="MYR",
        daily_limit_minor=50000,
        timezone=KL_TZ,
    )
    assert ok is True
    assert reason == "reserved"

    # 3. Third reservation: 1 minor (total would be 50001) -> rejected
    ok, reason = approvals._reserve_spending(
        user_id=user_id,
        order_id=str(uuid4()),
        amount_minor=1,
        currency="MYR",
        daily_limit_minor=50000,
        timezone=KL_TZ,
    )
    assert ok is False
    assert reason == "daily_limit_exceeded"


def test_spending_reopen_after_cancellation() -> None:
    """Cancelling an order releases its reserved spending and allows new reservations."""
    store = InMemoryStore()
    user_id = store.add_user()
    repos = store.repositories()

    order_id = str(uuid4())
    store.orders[order_id] = {
        "id": order_id,
        "user_id": user_id,
        "device_id": str(uuid4()),
        "merchant_id": "grocer",
        "currency": "MYR",
        "subtotal_minor": 50000,
        "total_minor": 50000,
        "status": "AWAITING_APPROVAL",
        "quote_expires_at": store.now() + timedelta(seconds=300),
        "cart_sha256": "dummy",
        "created_at": store.now(),
        "updated_at": store.now(),
    }

    # Reserve full 50000 cap
    ok, _ = repos.approvals._reserve_spending(
        user_id=user_id,
        order_id=order_id,
        amount_minor=50000,
        currency="MYR",
        daily_limit_minor=50000,
        timezone=KL_TZ,
    )
    assert ok is True

    # Next reservation fails
    ok2, reason2 = repos.approvals._reserve_spending(
        user_id=user_id,
        order_id=str(uuid4()),
        amount_minor=1000,
        currency="MYR",
        daily_limit_minor=50000,
        timezone=KL_TZ,
    )
    assert ok2 is False
    assert reason2 == "daily_limit_exceeded"

    # Cancel order -> releases reservation
    async def _cancel_and_retry() -> None:
        res = await repos.orders.cancel(
            order_id=order_id,
            user_id=user_id,
            actor_type="user",
            actor_id=user_id,
        )
        assert res.ok is True
        assert res.reason == "cancelled"

        # Now reservation succeeds!
        ok3, reason3 = repos.approvals._reserve_spending(
            user_id=user_id,
            order_id=str(uuid4()),
            amount_minor=1000,
            currency="MYR",
            daily_limit_minor=50000,
            timezone=KL_TZ,
        )
        assert ok3 is True
        assert reason3 == "reserved"

    asyncio.run(_cancel_and_retry())


def test_spending_reopen_after_expiry() -> None:
    """Expiring an order releases its reserved spending."""
    clock = FakeClock()
    store = InMemoryStore(clock)
    user_id = store.add_user()
    repos = store.repositories()

    order_id = str(uuid4())
    store.orders[order_id] = {
        "id": order_id,
        "user_id": user_id,
        "device_id": str(uuid4()),
        "merchant_id": "grocer",
        "currency": "MYR",
        "subtotal_minor": 50000,
        "total_minor": 50000,
        "status": "AWAITING_APPROVAL",
        "quote_expires_at": store.now() + timedelta(seconds=60),
        "cart_sha256": "dummy",
        "created_at": store.now(),
        "updated_at": store.now(),
    }

    ok, _ = repos.approvals._reserve_spending(
        user_id=user_id,
        order_id=order_id,
        amount_minor=50000,
        currency="MYR",
        daily_limit_minor=50000,
        timezone=KL_TZ,
    )
    assert ok is True

    # Advance clock past quote expiry
    clock.advance(100)

    async def _expire_and_retry() -> None:
        res = await repos.orders.expire(order_id)
        assert res.ok is True

        # Now capacity is freed
        ok_new, reason_new = repos.approvals._reserve_spending(
            user_id=user_id,
            order_id=str(uuid4()),
            amount_minor=20000,
            currency="MYR",
            daily_limit_minor=50000,
            timezone=KL_TZ,
        )
        assert ok_new is True
        assert reason_new == "reserved"

    asyncio.run(_expire_and_retry())


def test_spending_currency_mismatch_rejected() -> None:
    """Reservations in a different currency than existing records for the day fail."""
    store = InMemoryStore()
    user_id = store.add_user()
    repos = store.repositories()

    # Existing spending in MYR
    ok1, _ = repos.approvals._reserve_spending(
        user_id=user_id,
        order_id=str(uuid4()),
        amount_minor=5000,
        currency="MYR",
        daily_limit_minor=50000,
        timezone=KL_TZ,
    )
    assert ok1 is True

    # Reservation in USD fails
    ok2, reason2 = repos.approvals._reserve_spending(
        user_id=user_id,
        order_id=str(uuid4()),
        amount_minor=1000,
        currency="USD",
        daily_limit_minor=50000,
        timezone=KL_TZ,
    )
    assert ok2 is False
    assert reason2 == "currency_mismatch"


def test_spending_day_timezone_rollover() -> None:
    """Spending resets at midnight in Asia/Kuala_Lumpur timezone."""
    # 2026-10-24 23:00 KL time = 2026-10-24 15:00 UTC
    kl_dt = datetime(2026, 10, 24, 23, 0, 0, tzinfo=ZoneInfo(KL_TZ))
    utc_dt = kl_dt.astimezone(timezone.utc)
    clock = FakeClock(utc_dt)
    store = InMemoryStore(clock)
    user_id = store.add_user()
    repos = store.repositories()

    # Fill the limit for 2026-10-24 KL time
    ok1, _ = repos.approvals._reserve_spending(
        user_id=user_id,
        order_id=str(uuid4()),
        amount_minor=50000,
        currency="MYR",
        daily_limit_minor=50000,
        timezone=KL_TZ,
    )
    assert ok1 is True

    # Same day: fails
    ok2, reason2 = repos.approvals._reserve_spending(
        user_id=user_id,
        order_id=str(uuid4()),
        amount_minor=1000,
        currency="MYR",
        daily_limit_minor=50000,
        timezone=KL_TZ,
    )
    assert ok2 is False
    assert reason2 == "daily_limit_exceeded"

    # Advance clock by 2 hours (01:00 AM KL time on 2026-10-25)
    clock.advance(7200)

    # Next day: new reservation succeeds
    ok3, reason3 = repos.approvals._reserve_spending(
        user_id=user_id,
        order_id=str(uuid4()),
        amount_minor=10000,
        currency="MYR",
        daily_limit_minor=50000,
        timezone=KL_TZ,
    )
    assert ok3 is True
    assert reason3 == "reserved"


def test_spending_service_summary_calculations() -> None:
    """SpendingService correctly derives spent, reserved, and remaining minor."""
    h = make_harness(daily_spending_limit_minor=50000)
    user_id = h.new_user()
    today = h.clock.utc_now().astimezone(ZoneInfo(KL_TZ)).date()

    # Add committed and reserved spending
    h.store.spending_records.append({
        "user_id": user_id,
        "order_id": str(uuid4()),
        "amount_minor": 1200,
        "currency": "MYR",
        "status": "committed",
        "spending_day": today,
    })
    h.store.spending_records.append({
        "user_id": user_id,
        "order_id": str(uuid4()),
        "amount_minor": 800,
        "currency": "MYR",
        "status": "reserved",
        "spending_day": today,
    })

    async def _test() -> None:
        summary = await h.services.spending.summary(user_id)
        assert summary.day == today
        assert summary.committed_minor == 1200
        assert summary.reserved_minor == 800

        remaining = h.services.spending.remaining_minor(summary)
        # 50000 - 1200 - 800 = 48000
        assert remaining == 48000

        tool_summary = await h.services.spending.tool_summary(user_id)
        assert tool_summary["period"] == "today"
        assert tool_summary["day"] == today.isoformat()
        assert tool_summary["currency"] == "MYR"
        assert tool_summary["limit_minor"] == 50000
        assert tool_summary["committed_minor"] == 1200
        assert tool_summary["reserved_minor"] == 800
        assert tool_summary["remaining_minor"] == 48000
        assert tool_summary["remaining"] == "RM480.00"

    asyncio.run(_test())


def test_spending_remaining_clamps_to_zero() -> None:
    """remaining_minor clamps to 0 if total spending exceeds limit."""
    h = make_harness(daily_spending_limit_minor=1000, phone_approval_threshold_minor=1000)
    user_id = h.new_user()
    today = h.clock.utc_now().astimezone(ZoneInfo(KL_TZ)).date()

    h.store.spending_records.append({
        "user_id": user_id,
        "order_id": str(uuid4()),
        "amount_minor": 2000,
        "currency": "MYR",
        "status": "committed",
        "spending_day": today,
    })

    async def _test() -> None:
        summary = await h.services.spending.summary(user_id)
        remaining = h.services.spending.remaining_minor(summary)
        assert remaining == 0

    asyncio.run(_test())
