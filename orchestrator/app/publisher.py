"""Écritures dans OpenProject : sorties des agents, erreurs, résumé, suivi du crédit."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .openproject import OpenProjectClient, OpenProjectError, normalize

log = logging.getLogger(__name__)

SUMMARY_FIELD = "Résumé agents"
WP_COST_FIELD = "Coût agents (USD)"
CONSUMED_FIELD = "Crédit consommé (USD)"
REMAINING_FIELD = "Crédit restant (USD)"

# Transitions autorisées par agent (spec, section 2) : statut courant -> statuts demandables
ALLOWED_TRANSITIONS: dict[str, dict[str, set[str]]] = {
    "scout":    {"analyse": {"developpement", "a valider"}},
    "coder":    {"developpement": {"revue"}},
    "reviewer": {"revue": {"developpement", "a valider"}},
}


@dataclass
class AgentOutput:
    comment: str
    next_status: str | None      # nom de statut ("Developpement", "A valider"…) ou None
    summary: str


class Publisher:
    def __init__(self, clients: dict[str, OpenProjectClient], statuses: dict[str, int]):
        self._clients = clients          # {"scout": …, "coder": …, "reviewer": …, "orchestrator": …}
        self._statuses = statuses        # {nom normalisé: id}, voir OpenProjectClient.list_statuses
        self._orch = clients["orchestrator"]

    def status_id(self, name: str) -> int:
        return self._statuses[normalize(name)]

    async def publish_agent_output(self, role: str, wp_id: int, current_status: str,
                                   header: str, output: AgentOutput) -> tuple[int, str | None]:
        """Publie le commentaire au nom de l'agent, puis applique le statut s'il est autorisé.

        Renvoie (id du commentaire, statut appliqué ou None).
        """
        client = self._clients[role]
        comment = await client.add_comment(wp_id, f"{header}\n\n{output.comment}")

        if not output.next_status:
            return comment.id, None
        wanted = normalize(output.next_status)
        allowed = ALLOWED_TRANSITIONS.get(role, {}).get(normalize(current_status), set())
        if wanted not in allowed:
            log.warning("Transition refusée : %s a demandé %s -> %s sur #%s",
                        role, current_status, output.next_status, wp_id)
            await self.error(wp_id, f"Transition demandée par **{role}** non autorisée : "
                                    f"{current_status} → {output.next_status}. Statut inchangé.")
            return comment.id, None
        try:
            await client.update_work_package(wp_id, status_id=self.status_id(wanted))
        except OpenProjectError as exc:
            # 422 : transition refusée par le workflow OpenProject du rôle "Agent"
            await self.error(wp_id, f"OpenProject a refusé le changement de statut : {exc}")
            return comment.id, None
        return comment.id, wanted

    async def save_summary(self, wp_id: int, summary: str) -> None:
        await self._set_fields(wp_id, {SUMMARY_FIELD: summary})

    async def block(self, wp_id: int, reason: str) -> None:
        await self.error(wp_id, reason)
        wp = await self._orch.get_work_package(wp_id)
        if normalize(wp.status_name) not in ("bloque", "termine"):
            await self._orch.update_work_package(wp_id, status_id=self.status_id("Bloqué"))

    async def error(self, wp_id: int, message: str) -> None:
        await self._orch.add_comment(wp_id, f"⚠️ **Orchestrateur** · {message}")

    async def tracking_comment(self, tracking_task_id: int, text: str) -> None:
        await self._orch.add_comment(tracking_task_id, text)

    async def record_cost(self, tracking_task_id: int | None, line: str, *, wp_id: int,
                          wp_total_usd: float, consumed_usd: float, remaining_usd: float) -> None:
        """Ligne de coût dans la tâche de suivi + mise à jour des cumuls (spec, section 8)."""
        if tracking_task_id:
            await self._orch.add_comment(tracking_task_id, line)
            await self._set_fields(tracking_task_id, {
                CONSUMED_FIELD: round(consumed_usd, 4),
                REMAINING_FIELD: round(max(remaining_usd, 0.0), 4),
            })
        await self._set_fields(wp_id, {WP_COST_FIELD: round(wp_total_usd, 4)})

    async def _set_fields(self, wp_id: int, fields: dict) -> None:
        # Un champ manquant ne doit pas bloquer la démo : on journalise et on continue
        try:
            await self._orch.update_work_package(wp_id, custom_fields=fields)
        except KeyError as exc:
            log.warning("#%s : %s", wp_id, exc)
