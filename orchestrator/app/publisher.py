"""Écritures dans OpenProject : sorties des agents, erreurs, résumé, suivi du crédit."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .openproject import Activity, OpenProjectClient, OpenProjectError, normalize

log = logging.getLogger(__name__)

# Pas de champs personnalisés (réservés à l'édition payante d'OpenProject) : le résumé et les
# cumuls de coût sont publiés en commentaires de l'orchestrateur préfixés par INFO.
INFO_PREFIX = "INFO · "
SUMMARY_TITLE = "Résumé agents"


def is_info(comment: Activity, role_of: dict[int, str]) -> bool:
    """Commentaire INFO de l'orchestrateur (résumé, cumuls) : exclu des échanges envoyés aux agents."""
    return role_of.get(comment.author_id) == "orchestrator" and comment.raw.lstrip().startswith(INFO_PREFIX)


def latest_summary(comments: list[Activity], role_of: dict[int, str]) -> str:
    """Texte du dernier commentaire « INFO · Résumé agents », ou "" s'il n'y en a pas."""
    header = f"{INFO_PREFIX}{SUMMARY_TITLE}"
    for c in reversed(comments):
        raw = c.raw.strip()
        if is_info(c, role_of) and raw.startswith(header):
            return raw[len(header):].strip()
    return ""

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
        await self.info(wp_id, f"{SUMMARY_TITLE}\n\n{summary}")

    async def block(self, wp_id: int, reason: str) -> None:
        await self.error(wp_id, reason)
        wp = await self._orch.get_work_package(wp_id)
        if normalize(wp.status_name) not in ("bloque", "termine"):
            await self._orch.update_work_package(wp_id, status_id=self.status_id("Bloqué"))

    async def error(self, wp_id: int, message: str) -> None:
        await self._orch.add_comment(wp_id, f"⚠️ **Orchestrateur** · {message}")

    async def info(self, wp_id: int, message: str) -> None:
        await self._orch.add_comment(wp_id, f"{INFO_PREFIX}{message}")

    async def tracking_comment(self, tracking_task_id: int, text: str) -> None:
        await self._orch.add_comment(tracking_task_id, text)

    async def record_cost(self, tracking_task_id: int | None, line: str, *, wp_id: int,
                          wp_total_usd: float, consumed_usd: float, remaining_usd: float) -> None:
        """Ligne de coût dans la tâche de suivi + mise à jour des cumuls (spec, section 8)."""
        if tracking_task_id:
            await self._orch.add_comment(tracking_task_id, line)
            await self.info(tracking_task_id, f"Crédit consommé : {consumed_usd:.4f} USD · "
                                              f"Crédit restant : {max(remaining_usd, 0.0):.4f} USD")
        await self.info(wp_id, f"Coût agents : {wp_total_usd:.4f} USD (cumul de la tâche)")
