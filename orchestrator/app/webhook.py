"""Réception des webhooks OpenProject.

Vérifie la signature HMAC puis se contente de mettre la tâche en file de synchronisation :
le webhook sert à réagir vite, la vérité vient toujours de la relecture via l'API.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request

log = logging.getLogger(__name__)

_ALGOS = {"sha1": hashlib.sha1, "sha256": hashlib.sha256}


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """En-tête attendu : 'sha1=<hex>' (HMAC du corps brut avec le secret du webhook)."""
    if not header or "=" not in header:
        return False
    algo, _, received = header.partition("=")
    digestmod = _ALGOS.get(algo.lower())
    if digestmod is None:
        return False
    expected = hmac.new(secret.encode(), body, digestmod).hexdigest()
    return hmac.compare_digest(expected, received)


def build_router(secret: str, queue: asyncio.Queue[tuple[int, str]]) -> APIRouter:
    router = APIRouter()

    @router.post("/webhook", status_code=202)
    async def receive(request: Request,
                      x_op_signature: str | None = Header(default=None)) -> dict[str, str]:
        body = await request.body()
        if not verify_signature(secret, body, x_op_signature):
            raise HTTPException(status_code=401, detail="signature invalide")

        payload = await request.json()
        action = payload.get("action", "")
        wp_id = (payload.get("work_package") or {}).get("id")
        if action.startswith("work_package:") and isinstance(wp_id, int):
            queue.put_nowait((wp_id, "webhook"))
            log.info("Webhook %s pour #%s", action, wp_id)
        return {"status": "accepted"}

    @router.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return router
