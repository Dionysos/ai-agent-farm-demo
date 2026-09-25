# AI Agents Demo on OpenProject

Demo of an ai agent farm built with Pi.dev.

Three agents (Scout, Coder, Reviewer) driven from OpenProject and run by Pi on the Claude API.

```
agent/           agent service: HTTP server (Python stdlib) wrapping the Pi CLI
prompts/         system prompts for the 3 agents
orchestrator/    webhook, polling, routing, context, publishing, credit tracking
nginx/           TLS reverse proxy
```

## Model

> [!WARNING]
> The three agents run on `claude-haiku-4-5-20251001`, **supported until October 15, 2026**.
> After that date, agent runs will fail. To switch models, update all three together:
> `agent/pi/models.json` (model declared to Pi), `orchestrator/pricing.json` (price per million tokens,
> then set `verified` to `true`), and `MODEL_SCOUT` / `MODEL_CODER` / `MODEL_REVIEWER` in `.env`.

## Installation

1. **Scaleway instance**: Docker + docker compose, key-based SSH, security group with only port 443 open,
   restricted to Cloudflare IP ranges, and SSH limited to my IP.
2. **Cloudflare**: TODO
3. **Configuration**: `cp .env.example .env`, then fill in at least `OPENPROJECT_SECRET_KEY_BASE`
   (`openssl rand -hex 64`) and `WEBHOOK_SECRET` (`openssl rand -hex 32`). The other keys come in steps 5 and 6.
4. **First start, OpenProject only**:
```bash
   docker compose up -d openproject nginx
```
5. **OpenProject setup**
6. **Claude Platform**: one workspace per agent, each with its own key (`ANTHROPIC_KEY_*`) and a spend limit
7. **Full start and check**

## Operations

| Task | Command |
| --- | --- |
| Agent logs | `docker compose logs -f coder` |
| Run log | `docker compose exec orchestrator sh -c 'tail -n 5 /app/logs/runs-*.jsonl'` |
| Reset state (counters, costs) | `docker compose stop orchestrator && docker volume rm agents-openproject_orch_data` |