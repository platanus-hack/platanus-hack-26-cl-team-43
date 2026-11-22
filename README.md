# Jailbreak Radar

Watches public sources (GitHub repos and code search, Hugging Face datasets, papers) for
new LLM jailbreak prompts, extracts and deduplicates them, tests new ones against a set
of models through OpenRouter, and alerts subscribers when a prompt newly breaks a model.
A small web dashboard shows the feed, the per-model results, sources and spend.

One process runs everything: an APScheduler (collectors, raw-item processing every
minute, daily digest at 09:00 America/Santiago), a tester thread, and the web server.

## Local setup

Requires Docker and [uv](https://docs.astral.sh/uv/).

```sh
docker compose up -d          # Postgres + pgvector on localhost:5433
cp .env.example .env          # fill in the keys; SECRET_KEY must be >= 32 chars
uv sync
uv run radar migrate          # apply SQL migrations
uv run radar seed-corpus      # load the known in-the-wild corpus (baseline for novelty)
uv run radar poll github_discovery   # optional: run one collector once
uv run radar run              # scheduler + tester + web on $PORT (default 8000)
```

`migrate`, `seed-corpus` and `poll` only need `DATABASE_URL` (plus the token of the
source being polled); `radar run` needs the full configuration.

Tests: `uv run pytest` (uses the `radar_test` database from docker compose).

## Deploy

`docker build -t jailbreak-radar .` produces an image that runs `radar run` and bakes in
the embedding model. Run it anywhere that gives it a long-lived process (Railway, Fly,
a VM) with the `.env` variables set. The hosted Postgres must have `pgvector`
(Supabase or Neon). Migrations run automatically on start.

## Safety rules

- Alerts and digests never contain prompt text: only metadata (family, technique,
  which models broke, break rate, source domain) and a link to the access-controlled
  dashboard.
- Prompts are only ever tested with the harmless canary goal and low-severity policy
  goals in `config/goals.yaml`. Never add high-severity goals.
- The dashboard is login-only (`ALLOWED_EMAILS`), and model spend is capped per day by
  `DAILY_BUDGET_USD`.
