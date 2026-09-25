"""Appel des services agents (conteneurs Pi) depuis l'orchestrateur."""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from .pricing import Usage
from .telemetry import instrument_httpx_client


class AgentError(Exception):
    def __init__(self, message: str, usage: Usage | None = None, model: str | None = None):
        super().__init__(message)
        self.usage = usage or Usage()
        self.model = model


@dataclass
class AgentResult:
    text: str
    output: dict | None
    usage: Usage
    model: str
    duration_ms: int
    retries: int
    pi_cost_usd: float


class AgentClient:
    def __init__(self, urls: dict[str, str], run_timeout_s: int):
        self._urls = urls
        # Marge au-delà du délai de Pi pour laisser l'agent répondre proprement
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(run_timeout_s + 30, connect=5))
        instrument_httpx_client(self._http)   # span HTTP + propagation automatique de traceparent

    async def aclose(self) -> None:
        await self._http.aclose()

    async def health(self, role: str) -> dict:
        resp = await self._http.get(f"{self._urls[role]}/health", timeout=5)
        resp.raise_for_status()
        return resp.json()

    async def run(self, role: str, context: str, *, extra_instruction: str | None = None) -> AgentResult:
        try:
            resp = await self._http.post(f"{self._urls[role]}/run",
                                         json={"context": context, "extra_instruction": extra_instruction})
        except httpx.TimeoutException as exc:
            raise AgentError(f"l'agent {role} n'a pas répondu à temps") from exc
        except httpx.TransportError as exc:
            raise AgentError(f"agent {role} injoignable : {exc}") from exc
        if resp.status_code != 200:
            raise AgentError(f"agent {role} : HTTP {resp.status_code} {resp.text[:300]}")

        data = resp.json()
        usage = Usage.from_dict(data.get("usage"))
        if data.get("error"):
            raise AgentError(f"agent {role} : {data['error']}", usage, data.get("model"))
        return AgentResult(text=data.get("text", ""), output=data.get("output"), usage=usage,
                           model=data.get("model", ""), duration_ms=data.get("duration_ms", 0),
                           retries=data.get("retries", 0), pi_cost_usd=data.get("pi_cost_usd", 0.0))
