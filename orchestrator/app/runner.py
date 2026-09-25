"""Exécution d'un agent pour un événement : routage, garde-fous, appel, publication, coût."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from .agents import AgentClient, AgentError
from .budget import Budget
from .config import Settings
from .context import LABELS, MAX_SUMMARY, build_context
from .journal import Journal
from .openproject import OpenProjectClient, WorkPackage, mention, normalize
from .pricing import Pricing, Usage
from .publisher import AgentOutput, Publisher, is_info, latest_summary
from .router import Decision, route
from .state import StateStore
from .sync import Event
from .telemetry import mark_error, set_attrs, span, trace_id

log = logging.getLogger(__name__)

RETRY_INSTRUCTION = ("Ta réponse précédente était invalide ({problem}). Réponds uniquement par "
                     "l'objet JSON demandé, avec les champs comment, next_status et summary.")


def validate(output: dict | None, mode: str) -> tuple[AgentOutput | None, str | None]:
    if not isinstance(output, dict):
        return None, "aucun objet JSON trouvé"
    comment = output.get("comment")
    if not isinstance(comment, str) or not comment.strip():
        return None, "champ comment vide ou absent"
    next_status = output.get("next_status")
    if next_status is not None and not isinstance(next_status, str):
        return None, "next_status doit être une chaîne ou null"
    if mode == "reply":
        next_status = None                 # une réponse à une mention ne change jamais le statut
    summary = output.get("summary") if isinstance(output.get("summary"), str) else ""
    return AgentOutput(comment.strip(), next_status or None, summary.strip()), None


class Runner:
    def __init__(self, *, settings: Settings, clients: dict[str, OpenProjectClient],
                 publisher: Publisher, agents: AgentClient, pricing: Pricing, budget: Budget,
                 store: StateStore, journal: Journal, role_of: dict[int, str]):
        self._s = settings
        self._orch = clients["orchestrator"]
        self._publisher = publisher
        self._agents = agents
        self._pricing = pricing
        self._budget = budget
        self._store = store
        self._journal = journal
        self._role_of = role_of            # id utilisateur -> rôle (agents et orchestrateur)
        self._sem = asyncio.Semaphore(settings.max_concurrent_runs)
        self._locks: dict[int, asyncio.Lock] = {}

    # ------------------------------------------------------------ entrée

    async def handle(self, event: Event, source: str | None = None) -> None:
        wp = event.work_package
        if self._s.tracking_task_id and wp.id == self._s.tracking_task_id:
            return                         # aucun agent sur la tâche de suivi du crédit

        with span("event.receive", event__source=source, event__kind=event.kind, event__key=event.key,
                  openproject__work_package_id=wp.id, openproject__status=wp.status_name,
                  openproject__activity_id=event.comment.id if event.comment else None) as root:
            with span("routing.decide") as s:
                author, mentioned = None, []
                if event.kind == "comment":
                    author = self._role_of.get(event.comment.author_id, "humain")
                    mentioned = [self._role_of[u] for u in event.comment.mentioned_user_ids
                                 if u in self._role_of]
                decision = route(event.kind, normalize(wp.status_name), author, mentioned, self._budget.mode())
                set_attrs(s, routing__rule=decision.rule, routing__action=decision.action,
                          routing__agent=decision.agent, routing__mode=decision.mode,
                          routing__reason=decision.reason or None, routing__author=author,
                          routing__budget_mode=self._budget.mode())
            log.info("#%s %s -> %s %s %s %s", wp.id, event.kind, decision.rule, decision.action,
                     decision.agent or "", decision.reason)
            if decision.action == "ignore":
                return

            async with self._locks.setdefault(wp.id, asyncio.Lock()):
                try:
                    await self._apply(decision, event)
                except Exception as exc:
                    log.exception("Échec du traitement de #%s", wp.id)
                    root.record_exception(exc)
                    mark_error(root, str(exc))
                    try:
                        await self._publisher.block(wp.id, f"Erreur interne de l'orchestrateur : {exc}")
                    except Exception:
                        log.exception("Impossible de signaler l'erreur sur #%s", wp.id)

    async def _apply(self, decision: Decision, event: Event) -> None:
        wp = event.work_package
        if decision.action == "transition":
            await self._orch.update_work_package(wp.id, status_id=self._publisher.status_id(decision.target_status))
            return
        if decision.action == "block":
            await self._publisher.block(wp.id, f"{decision.reason.capitalize()} : plus aucun agent ne sera lancé.")
            return
        if decision.action == "hold":
            await self._publisher.error(wp.id, f"{decision.reason.capitalize()}. Mentionnez "
                                               f"@{LABELS[decision.agent]} en commentaire pour continuer.")
            return

        # L'événement est-il encore pertinent ? (il a pu attendre le verrou)
        fresh = await self._orch.get_work_package(wp.id)
        if decision.mode == "step" and fresh.status_id != wp.status_id:
            log.info("#%s : statut changé depuis l'événement, exécution ignorée", wp.id)
            return
        if self._store.number(f"runs:{wp.id}") >= self._s.max_runs_per_task:
            await self._publisher.block(wp.id, f"Limite de {self._s.max_runs_per_task} exécutions "
                                               "d'agent atteinte pour cette tâche.")
            return

        async with self._sem:
            await self._execute(decision, fresh, event)

    # ------------------------------------------------------------ exécution

    async def _execute(self, decision: Decision, wp: WorkPackage, event: Event) -> None:
        role, mode = decision.agent, decision.mode
        label = LABELS[role]
        cycle = int(self._store.number(f"refusals:{wp.id}")) + 1
        all_comments = await self._orch.list_comments(wp.id)
        summary = latest_summary(all_comments, self._role_of)
        comments = [c for c in all_comments if not is_info(c, self._role_of)]

        if mode == "reply":
            trigger = f"Commentaire de l'humain qui te mentionne :\n\n{event.comment.raw}"
        else:
            trigger = (f"La tâche vient de passer au statut « {wp.status_name} ». "
                       "Réalise ton étape du workflow.")
        with span("context.build", agent__role=role, context__comments=len(comments)) as s:
            context = build_context(wp=wp, role=role, mode=mode, trigger=trigger, comments=comments,
                                    role_of=self._role_of, summary=summary, cycle=cycle,
                                    max_cycles=self._s.max_review_cycles)
            s.set_attribute("context.chars", len(context))

        self._store.incr(f"runs:{wp.id}")
        started = time.monotonic()
        record = {"wp_id": wp.id, "agent": role, "mode": mode, "rule": decision.rule,
                  "event": event.key, "cycle": cycle, "trace_id": trace_id(), "context": context}
        usage, model, output, problem, text, retries = Usage(), "", None, None, "", 0

        with span("agent.run", agent__role=role, agent__mode=mode, agent__cycle=cycle,
                  openproject__work_package_id=wp.id) as run_span:
            try:
                result = await self._agents.run(role, context)
                usage, model, text, retries = usage + result.usage, result.model, result.text, result.retries
                output, problem = validate(result.output, mode)
                if problem:
                    log.warning("#%s sortie invalide de %s (%s), nouvel essai", wp.id, role, problem)
                    run_span.add_event("sortie_invalide", {"problem": problem})
                    result = await self._agents.run(role, context,
                                                    extra_instruction=RETRY_INSTRUCTION.format(problem=problem))
                    usage, text = usage + result.usage, result.text
                    retries += result.retries
                    output, problem = validate(result.output, mode)
            except AgentError as exc:
                usage, model = usage + exc.usage, model or exc.model or ""
                run_span.record_exception(exc)
                mark_error(run_span, str(exc))
                self._set_run_attrs(run_span, model, usage, retries, started, "erreur_agent")
                await self._record_cost(role, wp, cycle, model, usage)
                await self._publisher.block(wp.id, f"L'agent **{label}** a échoué : {exc}")
                self._journal.write({**record, "result": "erreur_agent", "error": str(exc),
                                     "usage": usage.__dict__, "duration_ms": self._ms(started)})
                return
            self._set_run_attrs(run_span, model, usage, retries, started, "sortie_invalide" if problem else "ok")
            if problem:
                mark_error(run_span, problem)

        cost = await self._record_cost(role, wp, cycle, model, usage)
        record.update(model=model, usage=usage.__dict__, cost_usd=cost, raw_response=text,
                      duration_ms=self._ms(started))
        if problem:
            await self._publisher.block(wp.id, f"Réponse de **{label}** non conforme après deux essais "
                                               f"({problem}).")
            self._journal.write({**record, "result": "sortie_invalide", "error": problem})
            return

        # Limite des cycles de revue (spec, section 4)
        if role == "reviewer" and output.next_status and normalize(output.next_status) == "developpement":
            if cycle >= self._s.max_review_cycles:
                output.next_status = "A valider"
                output.comment += ("\n\n_Limite de cycles de revue atteinte : "
                                   "les remarques restantes sont soumises à l'humain._")
            else:
                self._store.incr(f"refusals:{wp.id}")

        # Retour à l'humain : on le mentionne pour qu'il soit notifié
        if output.next_status and normalize(output.next_status) == "a valider" and wp.author_id:
            name = wp.raw["_links"]["author"].get("title", "auteur")
            output.comment = f"{mention(wp.author_id, name)}\n\n{output.comment}"

        step = wp.status_name if mode == "step" else "réponse à l'humain"
        header = f"**{label}** · {step} · cycle {cycle}"
        with span("openproject.publish", agent__role=role, openproject__work_package_id=wp.id,
                  openproject__requested_status=output.next_status) as s:
            comment_id, applied = await self._publisher.publish_agent_output(
                role, wp.id, wp.status_name, header, output)
            set_attrs(s, openproject__comment_id=comment_id, openproject__applied_status=applied)
            if output.summary:
                merged = f"{summary.strip()}\n[{label}] {output.summary}".strip()
                await self._publisher.save_summary(wp.id, merged[-MAX_SUMMARY:])

        self._journal.write({**record, "result": "ok", "next_status": output.next_status})

    async def _record_cost(self, role: str, wp: WorkPackage, cycle: int, model: str, usage: Usage) -> float:
        if usage == Usage():
            return 0.0
        with span("cost.record", agent__role=role, gen_ai__response__model=model or None) as s:
            cost = await self._record_cost_inner(role, wp, cycle, model, usage)
            set_attrs(s, cost__usd=cost, cost__demo_total_usd=self._budget.total,
                      cost__budget_usd=self._budget.budget_usd)
            return cost

    async def _record_cost_inner(self, role: str, wp: WorkPackage, cycle: int, model: str,
                                 usage: Usage) -> float:
        try:
            cost = self._pricing.cost(model, usage)
        except KeyError as exc:
            log.error("%s : coût non calculé", exc)
            await self._publisher.error(wp.id, f"Tarif inconnu pour {model} : coût non comptabilisé.")
            return 0.0

        total, wp_total, crossed = self._budget.add(wp.id, cost)
        budget = self._budget.budget_usd
        now = datetime.now(timezone.utc)
        line = (f"**{LABELS[role]}** · tâche #{wp.id} · cycle {cycle} · {now:%Y-%m-%d %H:%M} UTC\n"
                f"Modèle : {model} · entrée {usage.input} · sortie {usage.output} · "
                f"cache lu {usage.cache_read} · cache écrit {usage.cache_write}\n"
                f"Coût : {cost:.4f} USD · Cumul démo : {total:.2f} USD / {budget:.2f} USD "
                f"({total / budget * 100:.1f} %)")
        if trace_id():
            line += f"\nTrace : `{trace_id()}`"
        await self._publisher.record_cost(self._s.tracking_task_id, line, wp_id=wp.id,
                                          wp_total_usd=wp_total, consumed_usd=total,
                                          remaining_usd=budget - total)
        for threshold in crossed:
            await self._alert(threshold, total)
        return cost

    async def _alert(self, threshold: float, total: float) -> None:
        effect = {0.5: "Aucun changement de fonctionnement.",
                  0.8: "L'enchaînement automatique est suspendu : seules les mentions déclenchent un agent.",
                  1.0: "Plus aucun agent ne sera lancé."}[threshold]
        text = (f"🚨 **Budget démo à {int(threshold * 100)} %** · {total:.2f} USD / "
                f"{self._budget.budget_usd:.2f} USD. {effect}")
        log.warning(text)
        if self._s.tracking_task_id:
            tracking = await self._orch.get_work_package(self._s.tracking_task_id)
            if tracking.author_id:
                name = tracking.raw["_links"]["author"].get("title", "auteur")
                text = f"{mention(tracking.author_id, name)} {text}"
            await self._publisher.tracking_comment(self._s.tracking_task_id, text)

    def _set_run_attrs(self, s, model: str, usage: Usage, retries: int, started: float, result: str) -> None:
        cost = None
        if model and usage != Usage():
            try:
                cost = self._pricing.cost(model, usage)
            except KeyError:
                pass
        set_attrs(s, gen_ai__provider__name="anthropic", gen_ai__response__model=model or None,
                  gen_ai__usage__input_tokens=usage.input, gen_ai__usage__output_tokens=usage.output,
                  gen_ai__usage__cache_read__input_tokens=usage.cache_read,
                  gen_ai__usage__cache_creation__input_tokens=usage.cache_write,
                  agent__retries=retries, agent__duration_ms=self._ms(started),
                  agent__result=result, cost__usd=cost)

    @staticmethod
    def _ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)
