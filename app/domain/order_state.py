"""Order state machine: Python mirror of the DB trigger orders_enforce_status_transition.

The DB trigger is the authority; this mirror gives early errors and unit tests.
Keep ALLOWED_TRANSITIONS identical to public.order_transition_allowed (migration
20261025090000_phase2_hardening.sql).
"""

from __future__ import annotations

DB_STATUSES: tuple[str, ...] = (
    "CREATED",
    "QUOTED",
    "AWAITING_APPROVAL",
    "APPROVED",
    "PAYMENT_PENDING",
    "PAID",
    "DENIED",
    "EXPIRED",
    "CANCELLED",
    "PAYMENT_FAILED",
    "REQUIRES_RECONCILIATION",
)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "CREATED": frozenset({"QUOTED", "CANCELLED", "EXPIRED"}),
    "QUOTED": frozenset({"AWAITING_APPROVAL", "CANCELLED", "EXPIRED"}),
    "AWAITING_APPROVAL": frozenset({"APPROVED", "DENIED", "EXPIRED", "CANCELLED"}),
    "APPROVED": frozenset({"PAYMENT_PENDING", "CANCELLED"}),
    "PAYMENT_PENDING": frozenset({"PAID", "PAYMENT_FAILED", "REQUIRES_RECONCILIATION"}),
    "REQUIRES_RECONCILIATION": frozenset({"PAID", "PAYMENT_FAILED", "PAYMENT_PENDING"}),
    # Provider-confirmed late completion only.
    "PAYMENT_FAILED": frozenset({"PAID"}),
    "PAID": frozenset(),
    "DENIED": frozenset(),
    "EXPIRED": frozenset(),
    "CANCELLED": frozenset(),
}

CANCELLABLE: frozenset[str] = frozenset({"CREATED", "QUOTED", "AWAITING_APPROVAL", "APPROVED"})


class InvalidTransition(ValueError):
    def __init__(self, from_status: str, to_status: str) -> None:
        super().__init__(f"illegal order transition {from_status} -> {to_status}")
        self.from_status = from_status
        self.to_status = to_status


def is_allowed(from_status: str, to_status: str) -> bool:
    return to_status in ALLOWED_TRANSITIONS.get(from_status, frozenset())


def assert_transition(from_status: str, to_status: str) -> None:
    if not is_allowed(from_status, to_status):
        raise InvalidTransition(from_status, to_status)


def to_api_status(db_status: str) -> str:
    if db_status == "AWAITING_APPROVAL":
        return "awaiting_device_approval"
    return db_status.lower()


def to_db_status(api_status: str) -> str:
    if api_status == "awaiting_device_approval":
        return "AWAITING_APPROVAL"
    upper = api_status.upper()
    if upper not in DB_STATUSES or upper == "AWAITING_APPROVAL":
        raise ValueError(f"unknown status: {api_status}")
    return upper
