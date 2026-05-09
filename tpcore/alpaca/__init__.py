"""Alpaca paper-broker adapter.

The only Alpaca-specific code in the project lives here. Engines reach
the broker exclusively through ``tpcore.interfaces.broker.BrokerExecutionInterface``.
"""

from .broker_adapter import AlpacaPaperBrokerAdapter
from .exceptions import BrokerUnavailableError

__all__ = ["AlpacaPaperBrokerAdapter", "BrokerUnavailableError"]
