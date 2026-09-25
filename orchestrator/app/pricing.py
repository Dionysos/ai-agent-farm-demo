"""Calcul du coût d'une exécution à partir des tokens facturés et d'une table de tarifs."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.input + other.input, self.output + other.output,
                     self.cache_read + other.cache_read, self.cache_write + other.cache_write)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Usage":
        d = d or {}
        return cls(int(d.get("input", 0)), int(d.get("output", 0)),
                   int(d.get("cache_read", 0)), int(d.get("cache_write", 0)))


class Pricing:
    def __init__(self, path: str):
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        self._prices = {k: v for k, v in raw.items() if not k.startswith("_")}
        unverified = [k for k, v in self._prices.items() if not v.get("verified")]
        if unverified:
            log.warning("Tarifs non vérifiés dans %s : %s", path, ", ".join(unverified))

    def price_for(self, model: str) -> dict:
        """Tarif exact, sinon la clé la plus longue qui préfixe le modèle (ex. claude-sonnet-5-2026…)."""
        if model in self._prices:
            return self._prices[model]
        candidates = [k for k in self._prices if model.startswith(k)]
        if not candidates:
            raise KeyError(f"aucun tarif pour le modèle {model!r} dans pricing.json")
        return self._prices[max(candidates, key=len)]

    def cost(self, model: str, usage: Usage) -> float:
        p = self.price_for(model)
        return (usage.input * p["input"] + usage.output * p["output"]
                + usage.cache_read * p["cache_read"] + usage.cache_write * p["cache_write"]) / 1_000_000
