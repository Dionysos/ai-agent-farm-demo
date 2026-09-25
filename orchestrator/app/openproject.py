"""Client OpenProject (API v3) : lecture et écriture des tâches, commentaires, statuts.

Une instance par compte OpenProject (scout, coder, reviewer, orchestrator) : chaque
écriture est ainsi attribuée au bon compte dans l'historique de la tâche.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

API = "/api/v3"


class OpenProjectError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"OpenProject {status}: {message}")
        self.status = status


class LockConflict(OpenProjectError):
    """La tâche a été modifiée entre la lecture et l'écriture (lockVersion périmée)."""


# ---------------------------------------------------------------- modèles

@dataclass(frozen=True)
class Activity:
    id: int
    work_package_id: int
    type: str                  # "Activity::Comment" ou "Activity"
    author_id: int | None
    raw: str                   # texte markdown du commentaire ("" si pas de commentaire)
    created_at: datetime

    @property
    def is_comment(self) -> bool:
        return self.type == "Activity::Comment" and bool(self.raw.strip())

    @property
    def mentioned_user_ids(self) -> list[int]:
        return parse_mentions(self.raw)


@dataclass
class WorkPackage:
    id: int
    subject: str
    description: str
    status_id: int
    status_name: str
    project_id: int
    author_id: int | None
    lock_version: int
    updated_at: datetime
    raw: dict[str, Any] = field(repr=False)


# ---------------------------------------------------------------- utilitaires

_MENTION_TAG = re.compile(r"<mention\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')


def parse_mentions(raw: str) -> list[int]:
    """Ids des utilisateurs mentionnés, dans l'ordre d'apparition, sans doublon.

    OpenProject stocke une mention sous la forme :
    <mention class="mention" data-id="5" data-type="user" data-text="@Coder">@Coder</mention>
    """
    ids: list[int] = []
    for tag in _MENTION_TAG.findall(raw or ""):
        attrs = dict(_ATTR.findall(tag))
        if attrs.get("data-type") == "user" and attrs.get("data-id", "").isdigit():
            uid = int(attrs["data-id"])
            if uid not in ids:
                ids.append(uid)
    return ids


_MENTION_FULL = re.compile(r"<mention\b[^>]*>(.*?)</mention>", re.IGNORECASE | re.DOTALL)


def mentions_to_text(raw: str) -> str:
    """Remplace le markup des mentions par leur texte (« @Coder ») pour le contexte des agents."""
    return _MENTION_FULL.sub(lambda m: m.group(1), raw or "")


def mention(user_id: int, display_name: str) -> str:
    """Markup d'une mention, à insérer dans un commentaire (ex. le Scout qui interpelle l'humain)."""
    return (f'<mention class="mention" data-id="{user_id}" data-type="user" '
            f'data-text="@{display_name}">@{display_name}</mention>')


def normalize(name: str) -> str:
    """'À valider' -> 'a valider', 'Développement' -> 'developpement'."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return " ".join(s.lower().split())


def id_from_href(href: str | None) -> int | None:
    if not href:
        return None
    tail = href.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _to_activity(wp_id: int, a: dict[str, Any]) -> Activity:
    return Activity(
        id=a["id"],
        work_package_id=wp_id,
        type=a.get("_type", ""),
        author_id=id_from_href(a.get("_links", {}).get("user", {}).get("href")),
        raw=(a.get("comment") or {}).get("raw") or "",
        created_at=_dt(a["createdAt"]),
    )


def _to_work_package(d: dict[str, Any]) -> WorkPackage:
    links = d["_links"]
    return WorkPackage(
        id=d["id"],
        subject=d.get("subject", ""),
        description=(d.get("description") or {}).get("raw") or "",
        status_id=id_from_href(links["status"]["href"]),
        status_name=links["status"].get("title", ""),
        project_id=id_from_href(links["project"]["href"]),
        author_id=id_from_href(links.get("author", {}).get("href")),
        lock_version=d["lockVersion"],
        updated_at=_dt(d["updatedAt"]),
        raw=d,
    )


# ---------------------------------------------------------------- client

class OpenProjectClient:
    RETRY_DELAYS = (1, 3, 9)   # secondes, sur erreur réseau, 429 ou 5xx

    def __init__(self, base_url: str, api_key: str, *, host: str | None = None, timeout: float = 15.0):
        headers = {"Accept": "application/hal+json"}
        if host:
            # Appel direct au conteneur (sans nginx) : OpenProject refuse un Host différent de
            # son OPENPROJECT_HOST__NAME et redirige vers https sans X-Forwarded-Proto
            headers |= {"Host": host, "X-Forwarded-Proto": "https"}
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=("apikey", api_key),
            timeout=timeout,
            headers=headers,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        for attempt, delay in enumerate((*self.RETRY_DELAYS, None)):
            try:
                resp = await self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if delay is None:
                    raise OpenProjectError(0, f"réseau : {exc}") from exc
                log.warning("OpenProject injoignable (%s), nouvelle tentative dans %ss", exc, delay)
                await asyncio.sleep(delay)
                continue

            if (resp.status_code == 429 or resp.status_code >= 500) and delay is not None:
                log.warning("OpenProject %s sur %s %s, nouvelle tentative dans %ss",
                            resp.status_code, method, path, delay)
                await asyncio.sleep(delay)
                continue
            if resp.status_code == 409:
                raise LockConflict(409, resp.text[:300])
            if resp.status_code >= 400:
                raise OpenProjectError(resp.status_code, resp.text[:500])
            return resp.json() if resp.content else {}
        raise AssertionError("inatteignable")

    # ------------------------------------------------------------ lecture

    async def me(self) -> dict[str, Any]:
        """Compte associé à la clé : sert à connaître l'id utilisateur de chaque agent."""
        return await self._request("GET", f"{API}/users/me")

    async def get_project(self, project: str | int) -> dict[str, Any]:
        return await self._request("GET", f"{API}/projects/{project}")

    async def get_work_package(self, wp_id: int) -> WorkPackage:
        return _to_work_package(await self._request("GET", f"{API}/work_packages/{wp_id}"))

    async def list_activities(self, wp_id: int) -> list[Activity]:
        """Historique complet de la tâche (commentaires et modifications), du plus ancien au plus récent."""
        data = await self._request("GET", f"{API}/work_packages/{wp_id}/activities")
        return [_to_activity(wp_id, a) for a in data["_embedded"]["elements"]]

    async def list_comments(self, wp_id: int, last: int | None = None) -> list[Activity]:
        """Commentaires uniquement ; `last` limite aux N derniers (construction du contexte)."""
        comments = [a for a in await self.list_activities(wp_id) if a.is_comment]
        return comments[-last:] if last else comments

    async def project_work_packages(self, project: str | int, *, page_size: int = 50,
                                    updated_since: datetime | None = None) -> list[WorkPackage]:
        """Tâches du projet, les plus récemment modifiées d'abord.

        Le filtre statut "*" inclut les tâches fermées (exclues par défaut par l'API).
        Avec `updated_since`, s'arrête dès qu'une tâche est plus ancienne (bornes incluses :
        la déduplication en aval absorbe les doublons).
        """
        params = {
            "filters": json.dumps([{"status": {"operator": "*", "values": []}}]),
            "sortBy": json.dumps([["updatedAt", "desc"]]),
            "pageSize": page_size,
            "offset": 1,
        }
        result: list[WorkPackage] = []
        while True:
            data = await self._request("GET", f"{API}/projects/{project}/work_packages", params=params)
            elements = [_to_work_package(e) for e in data["_embedded"]["elements"]]
            for wp in elements:
                if updated_since and wp.updated_at < updated_since:
                    return result
                result.append(wp)
            if len(elements) < page_size or params["offset"] * page_size >= data.get("total", 0):
                return result
            params["offset"] += 1

    async def list_statuses(self) -> dict[str, int]:
        """{nom normalisé: id}, ex. {'a valider': 5, 'developpement': 3}."""
        data = await self._request("GET", f"{API}/statuses")
        return {normalize(s["name"]): s["id"] for s in data["_embedded"]["elements"]}

    # ------------------------------------------------------------ écriture

    async def add_comment(self, wp_id: int, markdown: str) -> Activity:
        """Publie un commentaire au nom du compte de ce client."""
        data = await self._request(
            "POST", f"{API}/work_packages/{wp_id}/activities",
            json={"comment": {"raw": markdown}},
        )
        return _to_activity(wp_id, data)

    async def update_work_package(self, wp_id: int, *, status_id: int, retries: int = 3) -> WorkPackage:
        """Change le statut, avec gestion du verrou optimiste.

        Une transition refusée par le workflow OpenProject lève OpenProjectError (422).
        """
        for attempt in range(retries):
            wp = await self.get_work_package(wp_id)
            body: dict[str, Any] = {"lockVersion": wp.lock_version,
                                    "_links": {"status": {"href": f"{API}/statuses/{status_id}"}}}
            try:
                return _to_work_package(
                    await self._request("PATCH", f"{API}/work_packages/{wp_id}", json=body))
            except LockConflict:
                if attempt == retries - 1:
                    raise
                log.info("Conflit de version sur #%s, nouvel essai", wp_id)
                await asyncio.sleep(0.5 * (attempt + 1))
        raise AssertionError("inatteignable")
