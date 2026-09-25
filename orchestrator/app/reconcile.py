"""Rapprochement quotidien avec la consommation réelle (API Usage & Cost, clé admin).

L'API Admin n'est disponible que pour les comptes organisation, et le rapport de coûts
peut avoir quelques heures de retard : un écart temporaire est normal.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import httpx

from .budget import Budget
from .config import Settings
from .publisher import Publisher
from .state import StateStore

log = logging.getLogger(__name__)

COST_REPORT_URL = "https://api.anthropic.com/v1/organizations/cost_report"


async def fetch_cost_usd(admin_key: str, since: datetime, workspace_ids: tuple[str, ...]) -> float:
    """Somme des coûts depuis `since` (début de journée UTC). `amount` est en cents."""
    params: list[tuple[str, str]] = [("starting_at", since.strftime("%Y-%m-%dT00:00:00Z")), ("limit", "31")]
    if workspace_ids:
        params.append(("group_by[]", "workspace_id"))
    cents, page = 0.0, None
    async with httpx.AsyncClient(timeout=30, headers={"x-api-key": admin_key,
                                                      "anthropic-version": "2023-06-01"}) as http:
        while True:
            resp = await http.get(COST_REPORT_URL, params=params + ([("page", page)] if page else []))
            resp.raise_for_status()
            data = resp.json()
            for bucket in data.get("data", []):
                for r in bucket.get("results", []):
                    if workspace_ids and r.get("workspace_id") not in workspace_ids:
                        continue
                    if r.get("currency") != "USD":
                        raise ValueError(f"devise inattendue : {r.get('currency')}")
                    cents += float(r["amount"])
            if not data.get("has_more"):
                return cents / 100
            page = data["next_page"]


async def reconcile_loop(settings: Settings, store: StateStore, budget: Budget, publisher: Publisher) -> None:
    if not settings.admin_key or not settings.tracking_task_id:
        log.info("Rapprochement désactivé (clé admin ou tâche de suivi absente)")
        return
    await asyncio.sleep(600)
    while True:
        try:
            since = datetime.fromisoformat(store.get("demo_started_at"))
            console = await fetch_cost_usd(settings.admin_key, since, settings.workspace_ids)
            ours = budget.total
            gap = ours - console
            pct = f" ({gap / console * 100:+.1f} %)" if console else ""
            scope = "workspaces des agents" if settings.workspace_ids else "toute l'organisation"
            await publisher.tracking_comment(
                settings.tracking_task_id,
                f"🔎 **Rapprochement** depuis le {since:%Y-%m-%d} ({scope}) · console : {console:.2f} USD · "
                f"orchestrateur : {ours:.2f} USD · écart : {gap:+.2f} USD{pct}")
        except Exception:
            log.exception("Échec du rapprochement des coûts")
        await asyncio.sleep(86400)
