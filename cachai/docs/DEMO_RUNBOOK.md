# Cachai — Hackathon Demo & Instant Replay Runbook

This runbook provides a fast, repeatable path to boot the full Cachai stack, seed deterministic demo data, verify system health, and run a live autonomous audit in under 60 seconds.

## Prerequisites

- **Docker & Docker Compose** (v2 `docker compose` or v1 `docker-compose` — auto-detected by `Makefile`)
- **Environment file**: copy `.env.example` to `.env` if you do not already have one:
  ```bash
  cp .env.example .env
  ```
  `DEMO_MODE=true` is enabled by default so you can run the entire demo without external LLM API keys. Optionally provide `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` for live upstream calls.
- **`uv`** (for running the local `pytest` suite or local `demo-seed` fallback)
- **Node.js 18+** *(optional)* — only needed if you run `make record-demo` to capture a browser walkthrough video.

## 60-Second Instant Replay Flow

1. **Start the full stack** (PostgreSQL, LiteLLM proxy, `agent-core`, and `web-blog`):
   ```bash
   make up
   ```
2. **Seed deterministic demo data** (pre-populates audited benchmark posts plus 2 unaudited models ready for a live run):
   ```bash
   make demo-seed
   ```
3. **Run the end-to-end smoke check** to confirm API health, seeded posts, and frontend availability:
   ```bash
   make smoke
   ```
4. **Open the web blog** in your browser:
   - [http://localhost:3000](http://localhost:3000) — browse published audit reports, scores, and execution artifacts.
5. **Trigger a live audit** on the 2 remaining unaudited models:
   ```bash
   curl -X POST http://localhost:8000/api/trigger-demo
   ```
   Refresh [http://localhost:3000](http://localhost:3000) to watch the newly audited models appear with their benchmark verdicts.

## Makefile Quick Reference

| Target | Command | Description |
| --- | --- | --- |
| `make help` | Default target | Lists all available Make targets and descriptions. |
| `make up` | `$(COMPOSE) up -d --build` | Builds images and starts all services in detached mode. |
| `make down` | `$(COMPOSE) down` | Stops and removes running containers (preserves database volume). |
| `make test` | `pytest -v` via `uv` | Runs the full `agent-core` unit and integration test suite. |
| `make demo-seed` | `python -m app.cli demo-seed` | Seeds deterministic demo data inside the running `agent-core` container, or falls back to local `uv` execution if the container is not running. |
| `make smoke` | `./scripts/e2e_smoke.sh --no-up` | Runs the end-to-end smoke test against the already-running stack. |
| `make record-demo` | `node scripts/record_demo.mjs` | Runs the Playwright script to record a browser demo walkthrough. |

## Troubleshooting

- **Re-running `make demo-seed` is safe**: the seed command is idempotent and can be executed multiple times without duplicating records.
- **Check API health directly**:
  ```bash
  curl -fsS http://localhost:8000/health
  ```
- **Inspect container logs** if a service fails to become healthy:
  ```bash
  docker compose logs -f agent-core web-blog
  ```
- **Reset the database completely** (wipes the `postgres_data` volume for a clean slate):
  ```bash
  docker compose down -v
  make up
  make demo-seed
  ```
- **Port conflicts**: ensure ports `3000` (`web-blog`), `4000` (`litellm`), `5432` (`postgres`), and `8000` (`agent-core`) are free on your host before running `make up`.
