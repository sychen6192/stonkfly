"""Spot-order action for the fixed neural decoder, shaped as an AgentKit provider.

The public Action objects are invoked directly by the fixed neural decoder.
No LLM, general wallet tools, transfers, or AgentKit analytics decorator.
AgentKit is optional (`pip install -e '.[agentkit]'`); without it, the same
two-class interface is defined here, avoiding about 90 extra blockchain packages.
"""

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

try:
    from coinbase_agentkit import ActionProvider
    from coinbase_agentkit.action_providers.action_provider import Action
except ModuleNotFoundError:

    class Action(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        name: str
        description: str
        args_schema: type[BaseModel] | None = None
        invoke: Callable = Field(..., exclude=True)

    class ActionProvider(ABC):
        def __init__(self, name, action_providers):
            self.name = name
            self.action_providers = action_providers

        @abstractmethod
        def supports_network(self, network): ...


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str
    side: Literal["BUY", "SELL"]


class StonkflyActions(ActionProvider):
    def __init__(self, guard, broker):
        self.guard = guard
        self.broker = broker
        self.quotes = {}
        super().__init__("stonkfly", [])

    def supports_network(self, network):
        return getattr(network, "network_id", None) == "coinbase-advanced"

    def get_actions(self, wallet_provider=None):
        return [
            Action(
                name="stonkfly_spot_order",
                description="Submit a budget-checked, price-bounded spot FOK order from a neural proposal.",
                args_schema=Proposal,
                invoke=self.invoke,
            )
        ]

    def invoke(self, args):
        p = Proposal.model_validate(args)
        plan = self.guard.plan(p.product, p.side, self.quotes)
        plan["neural_observation"] = self.guard.l.get("observation")
        plan["checkpoint"] = self.guard.l.get("checkpoint")
        plan = self.guard.l.reserve(plan, time.time())
        return self.broker.execute(plan, self.guard.before_submit)
