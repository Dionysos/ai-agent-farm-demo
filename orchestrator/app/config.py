"""Configuration de l'orchestrateur, lue depuis l'environnement (voir docker-compose.yml)."""
from __future__ import annotations

import os
from dataclasses import dataclass

AGENT_ROLES = ("scout", "coder", "reviewer")
ACCOUNTS = (*AGENT_ROLES, "orchestrator")


def _optional_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    return int(value) if value else None


@dataclass(frozen=True)
class Settings:
    op_url: str
    project: str
    tracking_task_id: int | None
    webhook_secret: str
    op_keys: dict[str, str]
    agent_urls: dict[str, str]
    budget_usd: float
    max_runs_per_task: int
    max_review_cycles: int
    max_concurrent_runs: int
    poll_interval_s: int
    run_timeout_s: int
    log_retention_days: int
    admin_key: str | None
    workspace_ids: tuple[str, ...]
    pricing_path: str
    state_db: str
    logs_dir: str
    op_host: str | None = None       # nom d'hôte public d'OpenProject, envoyé en en-tête Host

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ
        return cls(
            op_url=e["OPENPROJECT_URL"],
            project=e["OPENPROJECT_PROJECT"],
            tracking_task_id=_optional_int("OPENPROJECT_TRACKING_TASK_ID"),
            webhook_secret=e["WEBHOOK_SECRET"],
            op_keys={a: e[f"OP_KEY_{a.upper()}"] for a in ACCOUNTS},
            agent_urls={r: e[f"AGENT_URL_{r.upper()}"] for r in AGENT_ROLES},
            budget_usd=float(e.get("DEMO_BUDGET_USD", "50")),
            max_runs_per_task=int(e.get("MAX_RUNS_PER_TASK", "10")),
            max_review_cycles=int(e.get("MAX_REVIEW_CYCLES", "3")),
            max_concurrent_runs=int(e.get("MAX_CONCURRENT_RUNS", "3")),
            poll_interval_s=int(e.get("POLL_INTERVAL_S", "30")),
            run_timeout_s=int(e.get("RUN_TIMEOUT_S", "300")),
            log_retention_days=int(e.get("LOG_RETENTION_DAYS", "30")),
            admin_key=e.get("ANTHROPIC_ADMIN_KEY") or None,
            workspace_ids=tuple(w.strip() for w in e.get("ANTHROPIC_WORKSPACE_IDS", "").split(",") if w.strip()),
            pricing_path=e.get("PRICING_PATH", "/app/pricing.json"),
            state_db=e.get("STATE_DB", "/app/data/state.db"),
            logs_dir=e.get("LOGS_DIR", "/app/logs"),
            op_host=e.get("OPENPROJECT_HOST") or None,
        )
