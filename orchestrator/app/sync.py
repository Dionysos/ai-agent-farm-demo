"""Transforme "la tâche X a changé" (webhook ou polling) en événements métier dédupliqués.

Le webhook et le polling ne font que signaler une tâche à resynchroniser ; c'est la
comparaison avec le dernier état connu qui produit les événements. Les deux sources
peuvent donc se chevaucher sans créer de doublon.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from .openproject import Activity, OpenProjectClient, WorkPackage
from .state import StateStore

log = logging.getLogger(__name__)

EventKind = Literal["wp_created", "status_changed", "comment"]


@dataclass(frozen=True)
class Event:
    key: str                     # clé de déduplication
    kind: EventKind
    work_package: WorkPackage
    comment: Activity | None = None   # renseigné pour kind == "comment"


class WorkPackageSync:
    def __init__(self, op: OpenProjectClient, store: StateStore, project: str | int):
        self._op = op
        self._store = store
        self._project = project
        self._locks: dict[int, asyncio.Lock] = {}

    async def bootstrap(self) -> None:
        """Premier démarrage : enregistre l'existant sans produire d'événement."""
        if self._store.get("bootstrapped"):
            return
        for wp in await self._op.project_work_packages(self._project):
            acts = await self._op.list_activities(wp.id)
            self._store.save_wp(wp.id, wp.status_id, max((a.id for a in acts), default=0))
        self._store.set("poll_cursor", datetime.now(timezone.utc).isoformat())
        self._store.set("bootstrapped", "1")
        log.info("Bootstrap terminé")

    async def sync(self, wp_id: int) -> list[Event]:
        lock = self._locks.setdefault(wp_id, asyncio.Lock())
        async with lock:
            wp = await self._op.get_work_package(wp_id)
            acts = await self._op.list_activities(wp_id)
            max_act = max((a.id for a in acts), default=0)
            previous = self._store.get_wp(wp_id)

            events: list[Event] = []
            if previous is None:
                last_seen = 0
                events.append(Event(f"created:{wp.id}", "wp_created", wp))
            else:
                prev_status, last_seen = previous
                if wp.status_id != prev_status:
                    # la lockVersion rend la clé unique même si la tâche revient à un ancien statut
                    events.append(Event(f"status:{wp.id}:{wp.status_id}:{wp.lock_version}",
                                        "status_changed", wp))

            events += [Event(f"comment:{a.id}", "comment", wp, a)
                       for a in acts if a.id > last_seen and a.is_comment]

            self._store.save_wp(wp_id, wp.status_id, max(max_act, last_seen))
            # Marqué "traité" dès l'émission : un crash pendant le traitement perd l'événement.
            # Acceptable pour la démo ; sinon, marquer après traitement réussi.
            return [e for e in events if self._store.mark_processed(e.key)]


async def poll_loop(op: OpenProjectClient, store: StateStore, project: str | int,
                    queue: asyncio.Queue[tuple[int, str]], interval_s: int) -> None:
    """Filet de sécurité : repère les tâches modifiées depuis le dernier passage."""
    while True:
        try:
            cursor = store.get("poll_cursor")
            since = datetime.fromisoformat(cursor) - timedelta(seconds=5) if cursor else None
            wps = await op.project_work_packages(project, updated_since=since)
            for wp in wps:
                queue.put_nowait((wp.id, "polling"))
            if wps:
                store.set("poll_cursor", max(wp.updated_at for wp in wps).isoformat())
        except Exception:
            log.exception("Échec du polling OpenProject")
        await asyncio.sleep(interval_s)
