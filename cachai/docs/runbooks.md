# CachAI — Operational & VPS Deployment Runbooks

Actionable operational procedures for deploying, securing, maintaining, and troubleshooting the CachAI stack (`postgres`, `litellm`, `agent-core`, and `web-blog`).

---

## 1. Deploying to a Fresh Ubuntu VPS

### Prerequisites

- **OS**: Ubuntu 22.04 LTS or 24.04 LTS (minimum 2 vCPU, 4 GB RAM, 20 GB SSD).
- **DNS**: An `A` (and optional `AAAA`) record pointing your production domain (e.g., `cachai.example.com`) to the VPS public IPv4/IPv6 address.
- **Firewall (UFW)**: Allow only SSH (`22`), HTTP (`80`), and HTTPS (`443`):

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
sudo ufw status verbose
```

- **Docker Engine & Compose v2 plugin**:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg git
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
newgrp docker
docker compose version
```

### Clone Repository & Configure `.env`

```bash
git clone https://github.com/<org>/cachai.git /opt/cachai
cd /opt/cachai
cp .env.example .env
chmod 600 .env
```

Generate strong secrets before editing `.env`:

```bash
openssl rand -hex 24   # POSTGRES_PASSWORD
openssl rand -hex 32   # LITELLM_MASTER_KEY (prefix with sk-)
openssl rand -hex 32   # INTERNAL_API_TOKEN
```

Edit `/opt/cachai/.env` for production:

```dotenv
# Production Domain (used by Caddy automatic TLS in docker-compose.prod.yml)
DOMAIN=cachai.example.com

# PostgreSQL
POSTGRES_USER=cachai
POSTGRES_PASSWORD=<generated-pg-password>
POSTGRES_DB=cachai
POSTGRES_PORT=5432
DATABASE_URL=postgresql://cachai:<generated-pg-password>@postgres:5432/cachai

# LiteLLM Proxy & 50X AI
LITELLM_MASTER_KEY=sk-<generated-master-key>
LITELLM_BASE_URL=http://litellm:4000
FIFTYX_API_KEY=<your-50x-ai-api-key>
OPENAI_API_KEY=
ANTHROPIC_API_KEY=

# Agent Core & Budget Guardrails
INTERNAL_API_TOKEN=<generated-internal-token>
DAILY_BUDGET_USD=5.00
MONTHLY_BUDGET_USD=100.00
ESTIMATED_AUDIT_COST_USD=0.25
AUDIT_INTERVAL_MINUTES=60
DEMO_MODE=false

# Ephemeral Sandbox (docker-py)
SANDBOX_IMAGE=python:3.11-slim
SANDBOX_MEM_LIMIT=128m
SANDBOX_TIMEOUT_SECONDS=15

# Web Blog (Next.js)
INTERNAL_API_URL=http://agent-core:8000
NEXT_PUBLIC_API_URL=https://cachai.example.com
```

> **Note on budget variables**: `agent-core` currently enforces `DAILY_BUDGET_USD` (default `5.00`) in `agent-core/app/config.py` and queries LiteLLM `/key/info` for `max_budget`. Keep `MONTHLY_BUDGET_USD` aligned with your LiteLLM virtual key / provider spend limit when running in production. Set `DEMO_MODE=false` to enable live upstream inference through `litellm`.

### Start the Production Stack

Pre-pull the ephemeral sandbox image so the first audit does not hit `SANDBOX_TIMEOUT_SECONDS` while downloading:

```bash
docker pull python:3.11-slim
```

Start the stack using the production Compose overlay (`docker-compose.prod.yml`, which adds Caddy automatic TLS using `DOMAIN=...` and avoids exposing `postgres`, `litellm`, and `agent-core` directly on public interfaces):

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

> If `docker-compose.prod.yml` is not yet present in your branch, provision a Caddy reverse proxy overlay binding ports `80:80` and `443:443` with `DOMAIN=${DOMAIN}` forwarding `/api/*` and `/health` to `agent-core:8000` and all other paths to `web-blog:3000`, while overriding `postgres`, `litellm`, and `agent-core` ports to `127.0.0.1` only.

### Post-Deploy Verification

1. **Container health**:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
   ```
   Confirm `cachai-postgres` and `cachai-litellm` report `(healthy)`, and `cachai-agent-core` and `cachai-web-blog` are `Up`.

2. **Agent Core health & API**:
   ```bash
   curl -fsS http://127.0.0.1:8000/health
   # Expected: {"status":"ok"}
   curl -fsS http://127.0.0.1:8000/api/budget
   ```

3. **HTTPS, Public Blog, & RSS Feed**:
   ```bash
   curl -fsSI "https://${DOMAIN}/"
   curl -fsSI "https://${DOMAIN}/leaderboard"
   curl -fsSI "https://${DOMAIN}/telemetry"
   curl -fsSI "https://${DOMAIN}/rss.xml"
   ```
   Confirm HTTP `200` and a valid TLS certificate issued for `${DOMAIN}` (verify `/rss.xml` when the RSS route is enabled in `web-blog`).

4. **Optional end-to-end smoke check** (non-destructive against an already-running stack):
   ```bash
   API_URL="http://127.0.0.1:8000" WEB_URL="https://${DOMAIN}" ./scripts/e2e_smoke.sh --no-up
   ```

---

## 2. Configuring & Rotating Secrets (`FIFTYX_API_KEY` & `INTERNAL_API_TOKEN`)

### 50X AI Model Configuration

50X AI is routed through the `litellm` service (`cachai-litellm`) via `litellm/config.yaml`:

- **Base URL**: `https://api.ai.50x.energy/v1`
- **Model**: `deepseek-ai/DeepSeek-V4.1-Flash-1017` (also aliased as `50x-deepseek-flash` in `litellm/config.yaml`)
- **Credential plumbing**:
  - `FIFTYX_API_KEY` is injected into `litellm` (`os.environ/FIFTYX_API_KEY` in `litellm/config.yaml`).
  - `agent-core` authenticates to `litellm` (`http://litellm:4000`) using `LITELLM_MASTER_KEY` (`Authorization: Bearer ${LITELLM_MASTER_KEY}`), so `FIFTYX_API_KEY` does not need to be exposed to `agent-core` unless a direct provider integration is enabled.
  - `INTERNAL_API_TOKEN` is injected into `agent-core` and validated with constant-time comparison (`secrets.compare_digest`) on `POST /api/posts` via the `X-Internal-Token` header.

### Rotating `FIFTYX_API_KEY` (Low-Downtime)

1. Issue a new API key in the 50X AI console (keep the old key active during cutover).
2. Update `FIFTYX_API_KEY` in `/opt/cachai/.env`.
3. Recreate only the `litellm` container (and `agent-core` if it also consumes `FIFTYX_API_KEY` directly):
   ```bash
   cd /opt/cachai
   docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-deps --force-recreate litellm
   ```
4. Verify LiteLLM liveness and 50X AI completion through the proxy:
   ```bash
   curl -fsS http://127.0.0.1:4000/health/liveliness
   curl -fsS http://127.0.0.1:4000/v1/chat/completions \
     -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
     -H "Content-Type: application/json" \
     -d '{
       "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
       "messages": [{"role": "user", "content": "Reply with OK."}],
       "max_tokens": 8
     }'
   ```
5. Revoke the old key in the 50X AI console once the test completion succeeds.

### Rotating `INTERNAL_API_TOKEN` (Low-Downtime)

1. Generate a new 256-bit hex token:
   ```bash
   openssl rand -hex 32
   ```
2. Update `INTERNAL_API_TOKEN=<new-token>` in `/opt/cachai/.env` (and any external automation calling `POST /api/posts`).
3. Ensure no audit cycle is mid-publish (check `audit_runs` for `status = 'running'`), then recreate `agent-core`:
   ```bash
   cd /opt/cachai
   docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-deps --force-recreate agent-core
   ```
4. Verify health and token enforcement:
   ```bash
   # Service healthy
   curl -fsS http://127.0.0.1:8000/health

   # Old/invalid token MUST return HTTP 401
   curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8000/api/posts \
     -H "X-Internal-Token: invalid-token" \
     -H "Content-Type: application/json" \
     -d '{}'
   ```
   Confirm the invalid token returns `401` and that `agent-core` logs show no `401` errors on the next audit publish.

---

## 3. Database Migrations (Alembic) & PostgreSQL Backups

### Running Alembic Migrations

Alembic configuration lives at `agent-core/alembic.ini` and `agent-core/alembic/` (baseline revision: `0001_initial_schema`). `alembic/env.py` reads `DATABASE_URL` from `app.config.get_settings().database_url` and automatically adapts `postgresql://` to SQLAlchemy's `postgresql+psycopg://` driver.

> **Important**: `agent-core/Dockerfile` currently copies only `./app` into `/app`. Mount `alembic.ini` and `alembic/` when invoking Alembic via Docker Compose (or include them in the image build if customized).

```bash
cd /opt/cachai

# Check current applied revision
docker compose run --rm --no-deps \
  -v "$(pwd)/agent-core/alembic:/app/alembic:ro" \
  -v "$(pwd)/agent-core/alembic.ini:/app/alembic.ini:ro" \
  agent-core alembic current

# Inspect migration history
docker compose run --rm --no-deps \
  -v "$(pwd)/agent-core/alembic:/app/alembic:ro" \
  -v "$(pwd)/agent-core/alembic.ini:/app/alembic.ini:ro" \
  agent-core alembic history --verbose

# Apply all pending migrations
docker compose run --rm --no-deps \
  -v "$(pwd)/agent-core/alembic:/app/alembic:ro" \
  -v "$(pwd)/agent-core/alembic.ini:/app/alembic.ini:ro" \
  agent-core alembic upgrade head
```

If a database was bootstrapped exclusively from `db/init.sql` (which runs automatically only on an empty `postgres_data` volume) and does not yet have an `alembic_version` row, stamp it once before running newer migrations:

```bash
docker compose run --rm --no-deps \
  -v "$(pwd)/agent-core/alembic:/app/alembic:ro" \
  -v "$(pwd)/agent-core/alembic.ini:/app/alembic.ini:ro" \
  agent-core alembic stamp 0001_initial_schema
```

### Logical PostgreSQL Backups (`pg_dump`)

Create a backup directory owned by the deploy user:

```bash
sudo mkdir -p /var/backups/cachai
sudo chown "$USER":"$USER" /var/backups/cachai
chmod 700 /var/backups/cachai
```

Run an on-demand compressed custom-format dump (`-Fc`, recommended for `pg_restore`):

```bash
set -a && source /opt/cachai/.env && set +a
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
docker compose -f /opt/cachai/docker-compose.yml exec -T postgres \
  pg_dump -U "${POSTGRES_USER:-cachai}" -d "${POSTGRES_DB:-cachai}" -Fc \
  > "/var/backups/cachai/cachai_${TIMESTAMP}.dump"
```

Plain gzipped SQL alternative:

```bash
docker compose -f /opt/cachai/docker-compose.yml exec -T postgres \
  pg_dump -U "${POSTGRES_USER:-cachai}" -d "${POSTGRES_DB:-cachai}" --no-owner --no-acl \
  | gzip -9 > "/var/backups/cachai/cachai_${TIMESTAMP}.sql.gz"
```

### Automated Daily Backup (Cron)

Add a daily 03:15 UTC cron job (`crontab -e`) with 14-day retention:

```cron
15 3 * * * /bin/bash -c 'set -euo pipefail; set -a; source /opt/cachai/.env; set +a; TS=$(date -u +\%Y\%m\%dT\%H\%M\%SZ); docker compose -f /opt/cachai/docker-compose.yml exec -T postgres pg_dump -U "${POSTGRES_USER:-cachai}" -d "${POSTGRES_DB:-cachai}" -Fc > "/var/backups/cachai/cachai_${TS}.dump"; find /var/backups/cachai -type f -name "cachai_*.dump" -mtime +14 -delete' >> /var/log/cachai-backup.log 2>&1
```

### Restore Verification Procedure

Verify backups regularly without touching the live `cachai` database by restoring into a temporary database:

```bash
set -a && source /opt/cachai/.env && set +a
LATEST_DUMP="$(ls -1t /var/backups/cachai/cachai_*.dump | head -n 1)"

# 1. Create temporary database
docker compose -f /opt/cachai/docker-compose.yml exec -T postgres \
  psql -U "${POSTGRES_USER:-cachai}" -d postgres \
  -c "DROP DATABASE IF EXISTS cachai_restore_check;" \
  -c "CREATE DATABASE cachai_restore_check;"

# 2. Restore dump into temporary database
docker compose -f /opt/cachai/docker-compose.yml exec -T postgres \
  pg_restore -U "${POSTGRES_USER:-cachai}" -d cachai_restore_check --no-owner --no-acl \
  < "${LATEST_DUMP}"

# 3. Verify core tables and row counts
docker compose -f /opt/cachai/docker-compose.yml exec -T postgres \
  psql -U "${POSTGRES_USER:-cachai}" -d cachai_restore_check -c "
    SELECT 'model_releases' AS tbl, COUNT(*) FROM model_releases
    UNION ALL SELECT 'eval_datasets', COUNT(*) FROM eval_datasets
    UNION ALL SELECT 'eval_results', COUNT(*) FROM eval_results
    UNION ALL SELECT 'blog_posts', COUNT(*) FROM blog_posts
    UNION ALL SELECT 'audit_runs', COUNT(*) FROM audit_runs;
  "

# 4. Drop temporary database
docker compose -f /opt/cachai/docker-compose.yml exec -T postgres \
  psql -U "${POSTGRES_USER:-cachai}" -d postgres \
  -c "DROP DATABASE cachai_restore_check;"
```

---

## 4. Incident Response

### 4.1 Budget Circuit Breaker Tripped

**How it works**:
- `BudgetCircuitBreaker` (`agent-core/app/services/circuit_breaker.py`) is an in-memory singleton (`closed` -> `open` -> `half_open`) that opens after `3` consecutive upstream HTTP `429`/`5xx` failures or a single spend spike `> $2.00 USD`, with a `60s` recovery timeout.
- `accountant_node` (`agent-core/app/agents/accountant.py`) also rejects runs whenever remaining budget (`DAILY_BUDGET_USD` or LiteLLM `/key/info` `max_budget`) is below `ESTIMATED_AUDIT_COST_USD` (default `0.25`).

**Symptoms**:
- `agent-core` logs show:
  - `Accountant decision: Rejected (circuit breaker open): ...`, or
  - `Accountant decision: Rejected (litellm-proxy|local-db-fallback): remaining $... < estimated $...`
- `audit_runs` records complete with `budget_approved = FALSE` and `evaluations_count = 0`.
- `GET /api/budget` reports `utilization_pct` near `100.0` or `remaining_usd < ESTIMATED_AUDIT_COST_USD`.

**Diagnosis**:

1. Check live budget telemetry and LiteLLM key spend:
   ```bash
   curl -fsS http://127.0.0.1:8000/api/budget
   curl -fsS http://127.0.0.1:4000/key/info \
     -H "Authorization: Bearer ${LITELLM_MASTER_KEY}"
   ```
2. Query PostgreSQL spend and run frequency to distinguish legitimate usage from a runaway loop:
   ```bash
   docker compose exec -T postgres psql -U "${POSTGRES_USER:-cachai}" -d "${POSTGRES_DB:-cachai}" -c "
     SELECT
       COUNT(*) AS evals_24h,
       COALESCE(SUM(cost_usd), 0) AS spend_24h_usd
     FROM eval_results
     WHERE created_at >= NOW() - INTERVAL '24 hours';

     SELECT trigger_source, status, budget_approved, COUNT(*) AS runs
     FROM audit_runs
     WHERE started_at >= NOW() - INTERVAL '24 hours'
     GROUP BY trigger_source, status, budget_approved
     ORDER BY runs DESC;
   "
   ```
3. Inspect `agent-core` and `litellm` logs for upstream `429`/`5xx` bursts or repeated `/api/trigger-demo` calls:
   ```bash
   docker compose logs --tail=150 agent-core litellm
   ```

**Safe Resolution**:
- **If runaway loop / external abuse**: block abusive callers at Caddy/firewall, verify `AUDIT_INTERVAL_MINUTES` is not set too low (default `60`), and wait until spend stabilizes before resetting.
- **If tripped by transient upstream `429`/`5xx`**: wait `60s` for the circuit breaker to transition to `half_open` automatically and probe on the next run, or restart `agent-core` after confirming 50X AI / LiteLLM health:
  ```bash
  docker compose -f docker-compose.yml -f docker-compose.prod.yml restart agent-core
  ```
- **If legitimate usage hit the cap**: raise `DAILY_BUDGET_USD` (and `MONTHLY_BUDGET_USD` / LiteLLM key `max_budget` if configured) in `/opt/cachai/.env` and recreate `agent-core`:
  ```bash
  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-deps --force-recreate agent-core
  ```

---

### 4.2 Stuck Sandbox Container

**How it works**:
- `evaluate_in_sandbox` (`agent-core/app/services/sandbox.py`) launches ephemeral `python:3.11-slim` (`SANDBOX_IMAGE`) containers via `/var/run/docker.sock` with `network_disabled=True`, `mem_limit=${SANDBOX_MEM_LIMIT}` (`128m`), `pids_limit=64`, `read_only=True`, `cap_drop=["ALL"]`, `security_opt=["no-new-privileges:true"]`, and `container.wait(timeout=${SANDBOX_TIMEOUT_SECONDS})` (`15s`).
- Normally containers are force-removed in a `finally` block, but an abrupt `agent-core` crash (`SIGKILL`/OOM) or Docker daemon stall can leave orphaned containers behind.

**Diagnosis & Cleanup**:

1. List running or exited sandbox containers:
   ```bash
   docker ps -a --filter "ancestor=${SANDBOX_IMAGE:-python:3.11-slim}" \
     --format "table {{.ID}}\t{{.Image}}\t{{.Status}}\t{{.RunningFor}}\t{{.Names}}"
   ```
2. Check CPU/memory/PID usage and logs of any stuck container (`<cid>`):
   ```bash
   docker stats --no-stream <cid>
   docker logs --tail=50 <cid>
   docker inspect --format '{{.State.Status}} oom={{.State.OOMKilled}} pids={{.HostConfig.PidsLimit}} mem={{.HostConfig.Memory}}' <cid>
   ```
3. Force-remove orphaned or hung sandbox containers (never remove `cachai-*` service containers):
   ```bash
   docker rm -f <cid>
   ```
   To bulk-remove only exited `python:3.11-slim` sandbox containers:
   ```bash
   docker ps -aq --filter "ancestor=${SANDBOX_IMAGE:-python:3.11-slim}" --filter "status=exited" \
     | xargs -r docker rm -f
   ```
4. Verify Docker daemon health and sandbox limits:
   ```bash
   sudo systemctl status docker --no-pager
   docker info --format 'ContainersRunning={{.ContainersRunning}} MemTotal={{.MemTotal}}'
   docker compose exec -T agent-core env | grep -E '^SANDBOX_'
   ```
   Confirm `SANDBOX_MEM_LIMIT=128m`, `SANDBOX_TIMEOUT_SECONDS=15`, and that `pids_limit=64` remains intact in `agent-core/app/services/sandbox.py`.

---

### 4.3 Failed Audit Run Recovery

**How it works**:
- `run_audit_cycle` (`agent-core/app/agents/graph.py`) acquires the session-level PostgreSQL advisory lock `842901` (`AUDIT_CYCLE_LOCK_ID` in `agent-core/app/services/audit_runs.py`) and records lifecycle state in `audit_runs` (`running`, `completed`, `failed`, `skipped`).
- If `agent-core` crashes mid-cycle, the PostgreSQL session usually drops and releases lock `842901`, but the `audit_runs` row can remain `status = 'running'`, or a stalled DB connection can hold lock `842901` and cause subsequent runs to return `"Audit cycle already in progress"`.

**Diagnosis**:

1. Inspect `agent-core` logs:
   ```bash
   docker compose logs --tail=200 agent-core
   ```
2. Inspect recent `audit_runs` rows and check if advisory lock `842901` is currently held:
   ```bash
   docker compose exec -T postgres psql -U "${POSTGRES_USER:-cachai}" -d "${POSTGRES_DB:-cachai}" -c "
     SELECT id, trigger_source, model_id, status, budget_approved, evaluations_count,
            published_post_slug, error_summary, started_at, finished_at
     FROM audit_runs
     ORDER BY started_at DESC
     LIMIT 10;

     SELECT l.pid, a.state, a.query_start, a.application_name, a.query
     FROM pg_locks l
     JOIN pg_stat_activity a ON a.pid = l.pid
     WHERE l.locktype = 'advisory' AND l.objid = 842901;
   "
   ```
3. Verify LiteLLM and 50X AI connectivity:
   ```bash
   curl -fsS http://127.0.0.1:4000/health/liveliness
   curl -fsS http://127.0.0.1:8000/health
   ```

**Recovery**:

1. **Release a stale advisory lock** (ONLY if you confirmed no active audit is legitimately running):
   ```bash
   docker compose exec -T postgres psql -U "${POSTGRES_USER:-cachai}" -d "${POSTGRES_DB:-cachai}" \
     -c "SELECT pg_terminate_backend(<pid>);"
   ```
2. **Mark orphaned `running` rows as `failed`** (for rows whose worker crashed):
   ```bash
   docker compose exec -T postgres psql -U "${POSTGRES_USER:-cachai}" -d "${POSTGRES_DB:-cachai}" -c "
     UPDATE audit_runs
     SET status = 'failed',
         error_summary = COALESCE(NULLIF(error_summary, ''), 'Marked failed during incident recovery after worker interruption'),
         finished_at = NOW()
     WHERE status = 'running'
       AND started_at < NOW() - INTERVAL '10 minutes';
   "
   ```
3. **Re-queue an unaudited model** (only if a model was flipped to `audited = TRUE` before publishing failed):
   ```bash
   docker compose exec -T postgres psql -U "${POSTGRES_USER:-cachai}" -d "${POSTGRES_DB:-cachai}" \
     -c "UPDATE model_releases SET audited = FALSE WHERE id = <model_id>;"
   ```
4. **Safely re-trigger one audit cycle and verify**:
   ```bash
   curl -fsS -X POST http://127.0.0.1:8000/api/trigger-demo
   ```
   Confirm the JSON response shows `"status": "completed"`, `"errors": []`, and a non-null `"published_post_slug"`, then verify the post appears in `GET /api/posts`.
