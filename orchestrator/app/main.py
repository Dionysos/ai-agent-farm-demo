"""Point d'entrée de l'orchestrateur.

webhook + polling -> file de tâches à synchroniser -> événements -> Runner (routage, agent, publication)
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI

from .agents import AgentClient
from .budget import Budget
from .config import ACCOUNTS, Settings
from .journal import Journal
from .openproject import OpenProjectClient
from .pricing import Pricing
from .publisher import Publisher
from .reconcile import reconcile_loop
from .runner import Runner
from .state import StateStore
from .sync import WorkPackageSync, poll_loop
from .webhook import build_router
from . import telemetry

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("orchestrator")
telemetry.setup()      # avant la création des clients httpx instrumentés

settings = Settings.from_env()
queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
clients = {a: OpenProjectClient(settings.op_url, settings.op_keys[a], host=settings.op_host) for a in ACCOUNTS}
store = StateStore(settings.state_db)
journal = Journal(settings.logs_dir, settings.log_retention_days)
agents = AgentClient(settings.agent_urls, settings.run_timeout_s)
budget = Budget(store, settings.budget_usd)
sync = WorkPackageSync(clients["orchestrator"], store, settings.project)
_running: set[asyncio.Task] = set()


async def worker(runner: Runner) -> None:
    while True:
        wp_id, source = await queue.get()
        try:
            for event in await sync.sync(wp_id):
                # une tâche asyncio par événement : le verrou par tâche OpenProject garde l'ordre,
                # le sémaphore limite les exécutions simultanées
                task = asyncio.create_task(runner.handle(event, source))
                _running.add(task)
                task.add_done_callback(_running.discard)
        except Exception:
            log.exception("Échec de la synchronisation de #%s", wp_id)
        finally:
            queue.task_done()


async def purge_loop() -> None:
    while True:
        removed = journal.purge()
        if removed:
            log.info("Journal : %s fichier(s) supprimé(s)", removed)
        await asyncio.sleep(86400)


@asynccontextmanager
async def lifespan(app: FastAPI):
    role_of: dict[int, str] = {}
    for account, client in clients.items():
        me = await client.me()
        role_of[me["id"]] = account
        log.info("Compte %s : %s (id %s)", account, me.get("login"), me["id"])

    publisher = Publisher(clients, await clients["orchestrator"].list_statuses())
    runner = Runner(settings=settings, clients=clients, publisher=publisher, agents=agents,
                    pricing=Pricing(settings.pricing_path), budget=budget, store=store,
                    journal=journal, role_of=role_of)

    if not store.get("demo_started_at"):
        store.set("demo_started_at", datetime.now(timezone.utc).isoformat())
    await sync.bootstrap()

    tasks = [
        asyncio.create_task(worker(runner)),
        asyncio.create_task(poll_loop(clients["orchestrator"], store, settings.project, queue,
                                      settings.poll_interval_s)),
        asyncio.create_task(reconcile_loop(settings, store, budget, publisher)),
        asyncio.create_task(purge_loop()),
    ]
    log.info("Orchestrateur prêt · budget %.2f USD · consommé %.2f USD", budget.budget_usd, budget.total)
    yield
    for t in tasks:
        t.cancel()
    for c in clients.values():
        await c.aclose()
    await agents.aclose()
    telemetry.shutdown()


app = FastAPI(lifespan=lifespan)
app.include_router(build_router(settings.webhook_secret, queue))
