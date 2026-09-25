"""Cumul des coûts et seuils du budget démo (spec, section 8)."""
from __future__ import annotations

from .router import BudgetMode
from .state import StateStore

THRESHOLDS = (0.5, 0.8, 1.0)


class Budget:
    def __init__(self, store: StateStore, budget_usd: float):
        self._store = store
        self.budget_usd = budget_usd

    @property
    def total(self) -> float:
        return self._store.number("cost:total")

    def wp_total(self, wp_id: int) -> float:
        return self._store.number(f"cost:wp:{wp_id}")

    def mode(self) -> BudgetMode:
        ratio = self.total / self.budget_usd if self.budget_usd else 1.0
        if ratio >= 1.0:
            return "stopped"
        if ratio >= 0.8:
            return "mentions_only"
        return "normal"

    def add(self, wp_id: int, cost_usd: float) -> tuple[float, float, list[float]]:
        """Ajoute un coût. Renvoie (cumul démo, cumul de la tâche, seuils franchis pour la première fois)."""
        total = self._store.incr("cost:total", cost_usd)
        wp_total = self._store.incr(f"cost:wp:{wp_id}", cost_usd)
        crossed = []
        for t in THRESHOLDS:
            if total >= t * self.budget_usd and not self._store.get(f"alert:{t}"):
                self._store.set(f"alert:{t}", "1")
                crossed.append(t)
        return total, wp_total, crossed
