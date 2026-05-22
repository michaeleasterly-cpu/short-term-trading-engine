"""Shared scaffolding for per-trade engine order managers."""
from __future__ import annotations

from tpcore.order_management.base_order_manager import BaseOrderManager
from tpcore.order_management.transient_retry import (
    DEGRADED_POSITION_EVENT,
    ORDER_ESCALATED_EVENT,
    is_pre_response_transient,
    submit_with_transient_retry,
)

__all__ = [
    "BaseOrderManager",
    "DEGRADED_POSITION_EVENT",
    "ORDER_ESCALATED_EVENT",
    "is_pre_response_transient",
    "submit_with_transient_retry",
]
