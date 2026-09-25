"""Vérification de la configuration avant la démo.

    docker compose exec orchestrator python -m app.preflight

Contrôle les comptes OpenProject, le projet, les statuts, les champs personnalisés,
la tâche de suivi, les agents et la table de tarifs. Code de sortie 1 si un point bloque.
"""
from __future__ import annotations

import asyncio
import sys

from .agents import AgentClient
from .config import ACCOUNTS, AGENT_ROLES, Settings
from .openproject import OpenProjectClient, normalize
from .pricing import Pricing
from .publisher import CONSUMED_FIELD, REMAINING_FIELD, SUMMARY_FIELD, WP_COST_FIELD
from .reconcile import fetch_cost_usd

REQUIRED_STATUSES = ("Nouveau", "Analyse", "Développement", "Revue", "À valider", "Terminé", "Bloqué")
failures = 0


def check(ok: bool, label: str, detail: str = "") -> bool:
    global failures
    print(f"{'✅' if ok else '❌'} {label}{f' — {detail}' if detail else ''}")
    failures += 0 if ok else 1
    return ok


async def main() -> int:
    s = Settings.from_env()
    clients = {a: OpenProjectClient(s.op_url, s.op_keys[a], host=s.op_host) for a in ACCOUNTS}
    orch = clients["orchestrator"]

    print("\n— OpenProject")
    for account, client in clients.items():
        try:
            me = await client.me()
            check(True, f"compte {account}", f"{me.get('login')} (id {me['id']})")
        except Exception as exc:
            check(False, f"compte {account}", str(exc))

    try:
        project = await orch.get_project(s.project)
        check(True, "projet", project.get("name", s.project))
    except Exception as exc:
        check(False, "projet", str(exc))

    statuses = await orch.list_statuses()
    for name in REQUIRED_STATUSES:
        check(normalize(name) in statuses, f"statut « {name} »")

    wps = await orch.project_work_packages(s.project, page_size=10)
    sample = next((w for w in wps if w.id != s.tracking_task_id), None)
    if sample:
        fields = await orch.custom_fields(sample)
        for name in (SUMMARY_FIELD, WP_COST_FIELD):
            check(normalize(name) in fields, f"champ « {name} » sur les tâches", f"vérifié sur #{sample.id}")
    else:
        print("⚠️  aucune tâche de travail : créez-en une pour vérifier les champs « Résumé agents » et « Coût agents »")

    if check(s.tracking_task_id is not None, "OPENPROJECT_TRACKING_TASK_ID renseigné"):
        try:
            tracking = await orch.get_work_package(s.tracking_task_id)
            check(True, "tâche de suivi", f"#{tracking.id} {tracking.subject}")
            fields = await orch.custom_fields(tracking)
            for name in (CONSUMED_FIELD, REMAINING_FIELD):
                check(normalize(name) in fields, f"champ « {name} » sur la tâche de suivi")
        except Exception as exc:
            check(False, "tâche de suivi", str(exc))

    print("\n— Agents")
    agents = AgentClient(s.agent_urls, s.run_timeout_s)
    pricing = Pricing(s.pricing_path)
    for role in AGENT_ROLES:
        try:
            h = await agents.health(role)
            check(True, f"agent {role}", f"{h['model']} · Pi {h['pi_version']}")
            try:
                price = pricing.price_for(h["model"])
                check(True, f"tarif {h['model']}", "" if price.get("verified") else "⚠️ non vérifié")
            except KeyError as exc:
                check(False, f"tarif {h['model']}", str(exc))
        except Exception as exc:
            check(False, f"agent {role}", str(exc))
    await agents.aclose()

    print("\n— Claude Platform")
    if s.admin_key:
        from datetime import datetime, timezone
        try:
            usd = await fetch_cost_usd(s.admin_key, datetime.now(timezone.utc), s.workspace_ids)
            check(True, "clé admin (rapport de coûts)", f"{usd:.2f} USD aujourd'hui")
        except Exception as exc:
            check(False, "clé admin (rapport de coûts)", str(exc))
    else:
        print("ℹ️  pas de clé admin : rapprochement quotidien désactivé")

    for c in clients.values():
        await c.aclose()
    print(f"\n{'Tout est prêt.' if not failures else f'{failures} point(s) à corriger.'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
