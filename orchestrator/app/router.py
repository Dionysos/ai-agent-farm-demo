"""Règles de routage R1 à R7 (spec, section 5). Fonction pure, testable sans OpenProject."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .config import AGENT_ROLES

AGENT_BY_STATUS = {"analyse": "scout", "developpement": "coder", "revue": "reviewer"}

Action = Literal["run", "transition", "ignore", "hold", "block"]
BudgetMode = Literal["normal", "mentions_only", "stopped"]


@dataclass(frozen=True)
class Decision:
    action: Action
    rule: str
    agent: str | None = None
    mode: Literal["step", "reply"] | None = None
    target_status: str | None = None
    reason: str = ""


def route(kind: str, status: str, author_role: str | None,
          mentioned_roles: list[str], budget: BudgetMode) -> Decision:
    """
    kind            : "wp_created" | "status_changed" | "comment"
    status          : statut courant normalisé ("analyse", "a valider"…)
    author_role     : auteur du commentaire : "humain", un rôle d'agent ou "orchestrator"
    mentioned_roles : agents mentionnés dans le commentaire, dans l'ordre
    budget          : état du budget démo (spec, section 8)
    """
    if kind == "wp_created":
        if status == "nouveau":
            return Decision("transition", "R1", target_status="analyse")
        kind = "status_changed"          # tâche créée directement dans une étape d'agent

    if kind == "status_changed":
        agent = AGENT_BY_STATUS.get(status)
        if agent is None:
            return Decision("ignore", "R7", reason=f"aucun agent pour le statut « {status} »")
        if budget == "stopped":
            return Decision("block", "budget", reason="budget de la démo atteint")
        if budget == "mentions_only":
            return Decision("hold", "budget", agent=agent,
                            reason="budget à 80 % : enchaînement automatique suspendu")
        return Decision("run", "R2", agent=agent, mode="step")

    if kind == "comment":
        if author_role != "humain":
            return Decision("ignore", "R5", reason=f"commentaire écrit par {author_role}")
        agents = [r for r in mentioned_roles if r in AGENT_ROLES]
        if not agents:
            return Decision("ignore", "R6", reason="commentaire sans mention d'agent")
        if budget == "stopped":
            return Decision("block", "budget", reason="budget de la démo atteint")
        return Decision("run", "R3" if len(agents) == 1 else "R4", agent=agents[0], mode="reply")

    return Decision("ignore", "?", reason=f"événement inconnu : {kind}")
