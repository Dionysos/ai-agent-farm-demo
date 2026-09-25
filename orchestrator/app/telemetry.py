"""OpenTelemetry pour l'orchestrateur : traces exportées en OTLP/HTTP vers Jaeger.

Désactivé par défaut (OTEL_SDK_DISABLED=true) : l'API OpenTelemetry reste alors un no-op
sans coût, et le code instrumenté fonctionne à l'identique.

Arbre d'une exécution (spec, section 9) :

  event.receive                         (orchestrateur)
  ├── routing.decide
  ├── context.build
  ├── agent.run
  │   └── POST /run                     (client httpx instrumenté, propage traceparent)
  │       └── pi.run                    (service agent)
  │           └── llm.call ×n           (un par appel au modèle, attributs gen_ai.*)
  ├── cost.record
  └── openproject.publish
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

log = logging.getLogger(__name__)

tracer = trace.get_tracer("orchestrator")


def enabled() -> bool:
    return os.getenv("OTEL_SDK_DISABLED", "true").strip().lower() not in ("true", "1", "yes")


def setup() -> None:
    if not enabled():
        log.info("OpenTelemetry désactivé (OTEL_SDK_DISABLED)")
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # OTLPSpanExporter lit OTEL_EXPORTER_OTLP_ENDPOINT et ajoute /v1/traces
    provider = TracerProvider(resource=Resource.create({
        "service.name": os.getenv("OTEL_SERVICE_NAME", "orchestrator"),
        "deployment.environment": "demo",
    }))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    log.info("OpenTelemetry actif → %s", os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))


def shutdown() -> None:
    provider = trace.get_tracer_provider()
    if hasattr(provider, "shutdown"):
        provider.shutdown()


def instrument_httpx_client(client) -> None:
    """Instrumente un client httpx : span HTTP + propagation de traceparent vers le service appelé."""
    if not enabled():
        return
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    HTTPXClientInstrumentor.instrument_client(client)


def trace_id() -> str | None:
    """Id de la trace courante (32 caractères hex), à reporter dans le journal et la tâche de suivi."""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


@contextmanager
def span(name: str, **attributes) -> Iterator[Span]:
    """Span avec attributs (les None sont ignorés) ; une exception marque le span en erreur."""
    with tracer.start_as_current_span(name, record_exception=False, set_status_on_exception=False) as s:
        set_attrs(s, **attributes)
        try:
            yield s
        except Exception as exc:
            s.record_exception(exc)
            s.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
            raise


def set_attrs(s: Span, **attributes) -> None:
    for key, value in attributes.items():
        if value is not None:
            s.set_attribute(key.replace("__", "."), value)


def mark_error(s: Span, message: str) -> None:
    s.set_status(Status(StatusCode.ERROR, message[:200]))
