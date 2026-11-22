# CachAI — Continuous LLM Red-Teaming Platform (MVP)

Autonomous multi-agent platform for continuous LLM security auditing, budget-controlled execution, isolated sandbox scoring, and safe Markdown report publishing.

## Architecture

- **`postgres`**: PostgreSQL 16 storing model releases, evaluation datasets, evaluation results, and blog posts.
- **`litellm`**: LiteLLM Proxy centralizing model routing, API keys, and spend tracking.
- **`agent-core`**: FastAPI + LangGraph (4 modular agents: `scraper`, `orchestrator`, `accountant`, `auditor`) + APScheduler + `docker-py` ephemeral sandboxes.
- **`web-blog`**: Next.js (App Router) + Tailwind CSS + sanitized `react-markdown` public report viewer.

## Quick Start

1. Copy environment variables:
   ```bash
   cp .env.example .env
   ```
2. Start the entire stack:
   ```bash
   docker compose up --build
   ```
3. Trigger a full multi-agent audit cycle manually for the demo:
   ```bash
   curl -X POST http://localhost:8000/api/trigger-demo
   ```
4. Open the public audit blog at [http://localhost:3000](http://localhost:3000).

## Key Endpoints (`agent-core` on `:8000`)

- `GET /health` — Service health check.
- `GET /api/posts` — Public list of `published` reports.
- `GET /api/posts/{slug}` — Public detail view of a `published` report.
- `POST /api/posts` — Internal report publishing endpoint (requires `X-Internal-Token` header).
- `POST /api/trigger-demo` — Manually triggers one full LangGraph audit cycle.
