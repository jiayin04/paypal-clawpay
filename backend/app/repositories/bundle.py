"""Typed bundle of the persistence capabilities required by the application."""
from dataclasses import dataclass

from app.repositories.ports.approvals import ApprovalRepository
from app.repositories.ports.devices import DeviceRepository
from app.repositories.ports.events import (
    A2AMessageRepository,
    AuditRepository,
    ConversationRepository,
)
from app.repositories.ports.merchants import MerchantRepository
from app.repositories.ports.orders import OrderRepository
from app.repositories.ports.passports import PassportRepository
from app.repositories.ports.payment_methods import PaymentMethodRepository
from app.repositories.ports.payments import PaymentRepository
from app.repositories.ports.spending import SpendingRepository
from app.repositories.ports.webhooks import WebhookRepository


@dataclass
class Repositories:
    devices: DeviceRepository
    merchants: MerchantRepository
    orders: OrderRepository
    spending: SpendingRepository
    payment_methods: PaymentMethodRepository
    conversations: ConversationRepository
    a2a_messages: A2AMessageRepository
    audit: AuditRepository
    approvals: ApprovalRepository
    payments: PaymentRepository
    webhooks: WebhookRepository
    passports: PassportRepository
