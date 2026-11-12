"""Keep the centre's subscriptions in step with the rows the board already holds."""
from __future__ import annotations

from misaka.core.messaging.client import CenterClient, CenterError


class SubscriptionSync:
    """One endpoint per instance; one subscription per card subscription row."""

    def __init__(self, client: CenterClient, endpoint: str) -> None:
        self.client = client
        self.endpoint = endpoint

    @staticmethod
    def name_for(card_id: str, index: int) -> str:
        return f"card:{card_id}:{index}"

    def apply_card(self, card_id: str, specs: list[dict]) -> list[str]:
        """Push a card's subscriptions; returns the names now in force."""
        names = []
        for index, spec in enumerate(specs):
            name = self.name_for(card_id, index)
            self.client.put_subscription(name, self.endpoint, spec["source"], spec["type"])
            names.append(name)
        return names

    def clear(self, names: list[str]) -> list[str]:
        """Drop subscriptions, returning the names the centre refused to forget."""
        refused = []
        for name in names:
            try:
                self.client.drop_subscription(name)
            except CenterError as error:
                if error.status != 404:
                    refused.append(name)
        return refused
