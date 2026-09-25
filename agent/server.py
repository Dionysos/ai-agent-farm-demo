"""Service agent : expose Pi derrière une petite API HTTP.

POST /run     {"context": "...", "extra_instruction": "..."}  -> sortie de l'agent + usage
GET  /health  -> rôle, modèle, version de Pi

Pi tourne sans outils, sans session, sans extensions : il ne fait que produire du texte.
L'agent n'a aucun accès à OpenProject ; seul l'orchestrateur l'appelle.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s agent %(message)s")
log = logging.getLogger("agent")

ROLE = os.environ["AGENT_ROLE"]
MODEL = os.environ["MODEL"]
PROVIDER = os.getenv("PROVIDER", "anthropic")   # fournisseur Pi déclaré dans models.json
THINKING = os.getenv("THINKING", "off")
TIMEOUT_S = int(os.getenv("RUN_TIMEOUT_S", "300"))
SYSTEM_PROMPT = Path(os.getenv("PROMPT_PATH", "/app/prompt.md")).read_text(encoding="utf-8")
PI_DIR = Path(os.environ.get("PI_CODING_AGENT_DIR", "/tmp/pi-agent"))
MAX_BODY = 2_000_000

# ---------------------------------------------------------------- OpenTelemetry (optionnel)
OTEL = os.getenv("OTEL_SDK_DISABLED", "true").strip().lower() not in ("true", "1", "yes")
tracer = None
if OTEL:
    try:
        from opentelemetry import propagate, trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.trace import SpanKind, Status, StatusCode

        _provider = TracerProvider(resource=Resource.create({
            "service.name": os.getenv("OTEL_SERVICE_NAME", f"agent-{ROLE}"),
            "deployment.environment": "demo", "agent.role": ROLE}))
        _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(_provider)
        tracer = trace.get_tracer("agent")
    except ImportError:
        log.warning("OpenTelemetry demandé mais non installé : traces désactivées")


def _epoch_ns() -> int:
    return time.time_ns()


MESSAGE = ("Traite la demande décrite dans le fichier joint. "
           "Réponds uniquement par l'objet JSON demandé dans tes instructions, sans texte autour.")


def setup_pi() -> str:
    PI_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("settings.json", "models.json"):
        shutil.copy(f"/app/pi/{name}", PI_DIR / name)
    out = subprocess.run(["pi", "--version"], capture_output=True, text=True, timeout=30)
    return out.stdout.strip() or out.stderr.strip()


PI_VERSION = setup_pi()


def parse_output(text: str) -> dict | None:
    """Extrait l'objet JSON de la réponse (tolère des balises ```json autour)."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        value = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def run_pi(context: str, extra: str | None, span=None) -> dict:
    """Lance Pi et lit ses événements JSONL au fil de l'eau.

    La lecture en flux permet d'horodater chaque appel au modèle (message_start -> message_end)
    et d'en faire un span llm.call avec ses vraies durées.
    """
    started = time.monotonic()
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    result = {"role": ROLE, "model": MODEL, "text": "", "output": None, "usage": usage,
              "pi_cost_usd": 0.0, "retries": 0, "llm_calls": 0, "duration_ms": 0, "error": None}
    last = None
    call_started_ns: int | None = None

    with tempfile.TemporaryDirectory(dir="/tmp") as workdir:
        ctx_file = Path(workdir) / "contexte.md"
        ctx_file.write_text(context, encoding="utf-8")
        message = MESSAGE + (f"\n\n{extra}" if extra else "")
        cmd = ["pi", "--mode", "json", "--no-session", "--no-tools", "--no-extensions",
               "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files",
               "--offline", "--provider", PROVIDER, "--model", MODEL, "--thinking", THINKING,
               "--system-prompt", SYSTEM_PROMPT, f"@{ctx_file}", message]
        stderr_file = Path(workdir) / "stderr.log"
        with open(stderr_file, "w", encoding="utf-8") as err:
            # stdout en binaire : l'itération ne coupe alors que sur \n (pas sur \r ni U+2028)
            proc = subprocess.Popen(cmd, cwd=workdir, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=err)
            timed_out = threading.Event()

            def kill() -> None:
                timed_out.set()
                proc.kill()
            timer = threading.Timer(TIMEOUT_S, kill)
            timer.start()
            try:
                for raw in proc.stdout:            # Pi : JSONL délimité par \n uniquement
                    line = raw.decode("utf-8", errors="replace")
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type")
                    msg = event.get("message") or {}

                    if etype == "auto_retry_start":
                        result["retries"] += 1
                        if span is not None:
                            span.add_event("pi.auto_retry", {"attempt": event.get("attempt", 0),
                                                             "error": str(event.get("errorMessage", ""))[:200]})
                    elif etype == "message_start" and msg.get("role") == "assistant":
                        call_started_ns = _epoch_ns()
                    elif etype == "message_end" and msg.get("role") == "assistant":
                        u = msg.get("usage") or {}
                        usage["input"] += u.get("input", 0)
                        usage["output"] += u.get("output", 0)
                        usage["cache_read"] += u.get("cacheRead", 0)
                        usage["cache_write"] += u.get("cacheWrite", 0)
                        result["pi_cost_usd"] += (u.get("cost") or {}).get("total", 0.0)
                        result["model"] = msg.get("responseModel") or msg.get("model") or MODEL
                        result["llm_calls"] += 1
                        _llm_span(msg, u, call_started_ns or _epoch_ns(), _epoch_ns())
                        call_started_ns = None
                        last = msg
                proc.wait()
            finally:
                timer.cancel()
        stderr_tail = stderr_file.read_text(encoding="utf-8", errors="replace").strip()[-500:]

    result["duration_ms"] = int((time.monotonic() - started) * 1000)
    if timed_out.is_set():
        result["error"] = f"délai dépassé ({TIMEOUT_S} s)"
        return result
    if last is None:
        result["error"] = f"aucune réponse de Pi (code {proc.returncode}) : {stderr_tail}"
        return result
    if last.get("stopReason") in ("error", "aborted"):
        result["error"] = last.get("errorMessage") or f"arrêt {last.get('stopReason')}"
    result["text"] = "".join(c.get("text", "") for c in last.get("content", []) if c.get("type") == "text")
    result["output"] = parse_output(result["text"])
    return result


def _llm_span(msg: dict, u: dict, start_ns: int, end_ns: int) -> None:
    """Un span par appel au modèle, conventions sémantiques GenAI d'OpenTelemetry."""
    if tracer is None:
        return
    attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": PROVIDER,
        "gen_ai.request.model": MODEL,
        "gen_ai.response.model": msg.get("responseModel") or msg.get("model") or MODEL,
        "gen_ai.usage.input_tokens": u.get("input", 0),
        "gen_ai.usage.output_tokens": u.get("output", 0),
        "gen_ai.usage.cache_read.input_tokens": u.get("cacheRead", 0),
        "gen_ai.usage.cache_creation.input_tokens": u.get("cacheWrite", 0),
        "gen_ai.response.finish_reasons": [msg.get("stopReason", "")],
        "agent.role": ROLE,
    }
    if msg.get("responseId"):
        attrs["gen_ai.response.id"] = msg["responseId"]
    s = tracer.start_span("llm.call", kind=SpanKind.CLIENT, start_time=start_ns, attributes=attrs)
    if msg.get("stopReason") in ("error", "aborted"):
        s.set_status(Status(StatusCode.ERROR, str(msg.get("errorMessage", ""))[:200]))
    s.end(end_time=end_ns)


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok", "role": ROLE, "model": MODEL, "pi_version": PI_VERSION})
        else:
            self._send(404, {"error": "introuvable"})

    def do_POST(self) -> None:
        if self.path != "/run":
            return self._send(404, {"error": "introuvable"})
        length = int(self.headers.get("Content-Length", 0))
        if not 0 < length <= MAX_BODY:
            return self._send(413, {"error": "corps absent ou trop gros"})
        try:
            payload = json.loads(self.rfile.read(length))
            context = payload["context"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return self._send(400, {"error": "JSON attendu avec un champ 'context'"})

        traceparent = self.headers.get("traceparent", "-")
        log.info("run démarré (trace %s, contexte %d car.)", traceparent, len(context))
        if tracer is not None:
            parent = propagate.extract({k.lower(): v for k, v in self.headers.items()})
            cm = tracer.start_as_current_span("pi.run", context=parent, kind=SpanKind.SERVER,
                                              attributes={"agent.role": ROLE, "gen_ai.request.model": MODEL,
                                                          "context.chars": len(context),
                                                          "pi.version": PI_VERSION,
                                                          "agent.format_retry": bool(payload.get("extra_instruction"))})
        else:
            cm = nullcontext(None)
        with cm as span:
            try:
                result = run_pi(context, payload.get("extra_instruction"), span)
            except Exception as exc:  # garde-fou : l'orchestrateur voit toujours une réponse
                log.exception("échec de l'exécution")
                if span is not None:
                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
                return self._send(500, {"error": str(exc)})
            if span is not None:
                u = result["usage"]
                span.set_attributes({"gen_ai.response.model": result["model"],
                                     "gen_ai.usage.input_tokens": u["input"],
                                     "gen_ai.usage.output_tokens": u["output"],
                                     "agent.llm_calls": result["llm_calls"],
                                     "agent.retries": result["retries"],
                                     "agent.output_valid": result["output"] is not None})
                if result["error"]:
                    span.set_status(Status(StatusCode.ERROR, result["error"][:200]))
        log.info("run terminé en %d ms, usage %s, erreur %s",
                 result["duration_ms"], result["usage"], result["error"])
        self._send(200, result)

    def log_message(self, fmt: str, *args) -> None:  # journal HTTP silencieux
        pass


if __name__ == "__main__":
    log.info("Agent %s prêt (modèle %s, %s)", ROLE, MODEL, PI_VERSION)
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
