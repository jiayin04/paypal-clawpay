import itertools

import pytest

from app.domain.order_state import (
    ALLOWED_TRANSITIONS,
    DB_STATUSES,
    InvalidTransition,
    assert_transition,
    to_api_status,
    to_db_status,
)

EXPECTED = {
    ("CREATED", "QUOTED"), ("CREATED", "CANCELLED"), ("CREATED", "EXPIRED"),
    ("QUOTED", "AWAITING_APPROVAL"), ("QUOTED", "CANCELLED"), ("QUOTED", "EXPIRED"),
    ("AWAITING_APPROVAL", "APPROVED"), ("AWAITING_APPROVAL", "DENIED"),
    ("AWAITING_APPROVAL", "EXPIRED"), ("AWAITING_APPROVAL", "CANCELLED"),
    ("APPROVED", "PAYMENT_PENDING"), ("APPROVED", "CANCELLED"),
    ("PAYMENT_PENDING", "PAID"), ("PAYMENT_PENDING", "PAYMENT_FAILED"),
    ("PAYMENT_PENDING", "REQUIRES_RECONCILIATION"),
    ("REQUIRES_RECONCILIATION", "PAID"), ("REQUIRES_RECONCILIATION", "PAYMENT_FAILED"),
    ("REQUIRES_RECONCILIATION", "PAYMENT_PENDING"),
    ("PAYMENT_FAILED", "PAID"),
}


def test_status_set() -> None:
    assert len(DB_STATUSES) == 11
    assert set(ALLOWED_TRANSITIONS) == set(DB_STATUSES)


@pytest.mark.parametrize(("src", "dst"), list(itertools.product(DB_STATUSES, DB_STATUSES)))
def test_full_matrix(src: str, dst: str) -> None:
    if (src, dst) in EXPECTED:
        assert_transition(src, dst)
    else:
        with pytest.raises(InvalidTransition):
            assert_transition(src, dst)


def test_api_names() -> None:
    assert to_api_status("AWAITING_APPROVAL") == "awaiting_device_approval"
    assert to_api_status("REQUIRES_RECONCILIATION") == "requires_reconciliation"
    assert to_api_status("PAID") == "paid"
    assert to_db_status("awaiting_device_approval") == "AWAITING_APPROVAL"
    assert to_db_status("payment_failed") == "PAYMENT_FAILED"
    with pytest.raises(ValueError):
        to_db_status("awaiting_approval")
