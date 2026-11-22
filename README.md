# Cachai

<img src="./project-logo.png" alt="Project Logo" width="200" />

Platanus Hack 26: Santiago ft. Buk · Track: 🛡️ AI Security · team-43

- Mauricio Genta ([@5y5F4il](https://github.com/5y5F4il))
- Jaime Aza ([@Jjat00](https://github.com/Jjat00))
- Tomás Degese ([@tomileonel](https://github.com/tomileonel))
- Christian Rodrigo Rojas Rodriguez ([@Christian-Rojas-Rodriguez](https://github.com/Christian-Rojas-Rodriguez))
- Federico Hörl ([@fede-h](https://github.com/fede-h))

**¿Cachai qué modelo te están sirviendo?** Cachai es el auditor de modelos servidos como *token-as-a-service*: mide los endpoints que sirven un mismo modelo abierto y le dice a quien compra tokens cuáles se apartan de la referencia.

## El negocio

### Problema

- Decenas de proveedores sirven el mismo modelo abierto con el mismo nombre y precios muy distintos. GLM-5.3 tiene 47 endpoints en OpenRouter; 14 no declaran cuantización y el precio de salida varía 4,7 veces (API pública, 21-nov-2026).
- Quien compra no puede comprobar qué recibe: si el modelo es el declarado, si está degradado o si el proveedor agrega instrucciones propias.
- SOC 2 e ISO 27001 revisan procesos de la empresa. Ninguna certificación cubre qué modelo se sirve.
- Es un problema de seguridad de IA: una evaluación de seguridad vale para unos pesos y una configuración. Si no se verifica qué se sirve, no cubre lo desplegado.

### Cliente ideal

Equipos que ya gastan en tokens de modelos abiertos a través de varios proveedores y tienen agentes en producción. Lo usa quien elige proveedor (plataforma o infraestructura de IA) para decidir a cuál mandarle tráfico y a cuál sacarlo.

### Qué vendemos

Una ficha por endpoint, medida en forma continua, que separa tres niveles de evidencia:

| Nivel | Qué es | Ejemplo |
|---|---|---|
| E1, declarado | Lo afirma el proveedor | Cuantización, política de retención |
| E2, medido | Lo medimos desde afuera, con método y muestra | Tokens de sobra, exactitud, tool calls |
| E3, atestado | Evidencia criptográfica verificable | Pesos medidos dentro de un TEE |

Hoy el código cubre E1 y E2.

### Quién paga

Quien compra los tokens, no quien los vende. Así el auditado no financia su propia auditoría. Los proveedores se suman gratis: dan acceso y obtienen una ficha pública para mostrar.

### Competencia y cuña

No somos los primeros en verificar proveedores de inferencia.

| Quién | Qué hace | En qué nos diferenciamos |
|---|---|---|
| Artificial Analysis | Benchmarks públicos contra una referencia propia | Es una foto puntual |
| Benchlist | Atestación de benchmarks, USD 499 al mes | Lo paga el proveedor |
| OpenRouter Exacto | Calidad de tool calls sobre tráfico real | Es del intermediario y no publica una ficha |
| Tinfoil, Phala | Prueba criptográfica de los pesos | Solo en sus propios endpoints |

La cuña: auditoría continua, del lado del comprador, con pruebas que el proveedor no puede reconocer de antemano y con el nivel de evidencia de cada dato a la vista.

### Mercado

El gasto empresarial en modelos e inferencia fue de USD 94,2 mil millones a septiembre de 2026 (Menlo Ventures). Estimamos que unos USD 3,8 a 4,5 mil millones al año van a modelos abiertos por API. Cobrando entre 0,5 y 1 % del gasto del comprador, el techo sería de USD 20 a 45 millones al año. Las dos últimas cifras son estimaciones nuestras.

### Validación y lo que falta

- **Un comprador** dijo que lo compraría para elegir proveedores, que no conoce ningún certificado en el que confíe y que hace falta un estándar propio, no una ISO.
- **Un proveedor** contó que pierde ventas porque le piden certificaciones que no tiene.
- Es una entrevista por lado. No está validado cuánto pagarían ni quién firma la compra.
- El auditor corre de punta a punta contra un modelo de juguete y un proveedor tramposo propio. Todavía no corrió contra proveedores reales.

### Qué no prometemos

- "Sin desviación detectada" no significa "es el mismo modelo".
- No certificamos privacidad: desde afuera no se puede probar que un proveedor no mire ni guarde los datos.
- "Distinguible de la referencia" no es una acusación. Puede deberse a la plantilla, al parser o a la cuantización.

## Qué hay en este repo

| Carpeta | Qué es | Autor | Cómo se levanta |
|---|---|---|---|
| raíz (`src/radar`, `config/`, `tests/`) | **Jailbreak Radar**: recolecta prompts de jailbreak públicos, los prueba contra modelos y alerta | Mauricio | `docker compose up -d && uv sync && uv run radar migrate && uv run radar run` |
| [`src/radar/audit/`](src/radar/audit/) | **Cachai auditor**: mide los endpoints que sirven un mismo modelo open-weight y escribe una ficha con lo declarado y lo medido. Usa piezas del Radar y publica en el blog de CachAI | Christian | `uv run cachai endpoints z-ai/glm-5.3`; ver [docs/cachai-auditor.md](docs/cachai-auditor.md) |
| [`cachai/`](cachai/) | **CachAI**: plataforma multiagente que audita modelos nuevos y publica reportes en un blog | Federico | `cd cachai && API_PORT=8001 POSTGRES_PORT=5434 NEXT_PUBLIC_API_URL=http://localhost:8001 docker compose up -d --build` |

Las dos partes todavía corren por separado, cada una con su propia base de datos. Ambas usan el puerto 8000 por defecto; por eso CachAI se levanta con `API_PORT=8001`.

Lo que sigue en este README describe el Jailbreak Radar. El de CachAI está en [`cachai/README.md`](cachai/README.md).


Watches public sources (GitHub repos and code search, Hugging Face datasets, papers) for
new LLM jailbreak prompts, extracts and deduplicates them, tests new ones against a set
of models through OpenRouter, and alerts subscribers when a prompt newly breaks a model.
A small web dashboard shows the feed, the per-model results, sources and spend.

One process runs everything: an APScheduler (collectors, raw-item processing every
minute, daily digest at 09:00 America/Santiago), a tester thread, and the web server.

## Before going live: what you need to do

The code is built and tested (`uv run pytest`), but it has never run against the real
APIs. These steps need your accounts and keys, so nobody can do them for you. Do them
in order; `docs/demo.md` has the stage script and fallbacks.

### 1. Get the keys

| Variable | Where to get it | Required? |
|---|---|---|
| `GITHUB_TOKEN` | GitHub → Settings → Developer settings → fine-grained token, public repos read-only | yes |
| `OPENROUTER_API_KEY` | openrouter.ai → Keys. Add credits; a 401/402 pauses all testing | yes |
| `HF_TOKEN` | huggingface.co → Settings → Access Tokens (read) | optional; without it, or with a bad one, some datasets are silently skipped |
| `RESEND_API_KEY`, `EMAIL_FROM` | resend.com → API Keys; `EMAIL_FROM` must use a domain verified there | needed for login links and email alerts |
| `TWILIO_SID`, `TWILIO_TOKEN`, `TWILIO_WHATSAPP_FROM` | Twilio console → Messaging → Try WhatsApp (sandbox number) | needed for WhatsApp alerts |
| `SECRET_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(48))"` | yes, at least 32 characters, or the app won't start |

Login to the dashboard is by email magic link, so **without Resend nobody can log in**.

### 2. Fill in `.env`

```sh
cp .env.example .env
```

- Add the keys above.
- `EXTRACTOR_MODEL` and `JUDGE_MODEL` are empty in the template. Set both to a cheap,
  fast OpenRouter model id that supports JSON-schema output (a Haiku-class model).
  Check the exact id on openrouter.ai/models.
- `ALLOWED_EMAILS`: the team's emails, comma-separated.
- `BASE_URL`: the public URL once deployed (alert links point there).
- `DAILY_BUDGET_USD`: start at 20. It caps extraction and testing together.

### 3. Check the model list

`config/models.yaml` was filled from OpenRouter's catalog on 2026-11-21. Confirm these
two choices on openrouter.ai/models and correct them if needed:

- `google/gemini-4-neon`, picked as the "Gemini Pro" slot because Gemini 4 has no "Pro" model
- `openai/gpt-6.2-astra`, the non-pro variant

A model id that returns "not found" is switched off automatically. Changing its
`openrouter_id` in the yaml and restarting turns it back on.

### 4. Run it locally with real keys

Follow **Local setup** below. Then:

1. Log in at `http://localhost:8000/login` (the magic link arrives by email).
2. Open `/sources`. Every collector should have a recent run and no error. A GitHub
   or OpenRouter key problem shows up there or in the server log.
3. Within a few minutes, `/feed` should show new candidates moving from `queued` to
   a result.
4. Check the spend on `/`: it should stay well under the cap.

### 5. Set up alerts

1. On every team phone, join the Twilio WhatsApp sandbox: send the `join <code>` message
   shown in the Twilio console to the sandbox number.
2. Add each person as a subscriber with `min_severity = 0`, so they also get the
   "[open model]" alerts the demo may rely on. The exact SQL is in `docs/demo.md` §1.
   Phones go in E.164 format (`+569...`).

### 6. Prepare the demo repo

Details are in `docs/demo.md` §2–3.

1. Create a public GitHub repo for the demo and add it on `/sources` (it becomes a `seed`
   repo).
2. Choose a public, known jailbreak *template* with a fill-in slot and nothing harmful
   in its own text.
3. Dry-run it the day before on a separate scratch repo (not a branch: only the default
   branch is read). Note which models it breaks. If no frontier model breaks, the
   "[open model]" alert is the fallback.

### 7. Deploy and rehearse

1. Create a hosted Postgres with `pgvector` (Supabase or Neon) and set `DATABASE_URL`.
2. Build and deploy the Docker image (see **Deploy** below) with the full `.env`.
3. Run `radar seed-corpus` once against the hosted database.
4. Rehearse the stage script in `docs/demo.md` §4 against the deployed instance. Fill in
   the timings table in §7.
5. Don't restart or redeploy in the 15 minutes before going on stage: a test
   interrupted by a restart only resumes after 15 minutes.

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
  `DAILY_BUDGET_USD`. The cap covers extraction as well as testing: once it is reached,
  raw items wait unprocessed and test jobs wait queued until the next Santiago day.

## Watchlist behaviour

- First sight of a repo: `seed` repos (added by hand or on `/sources`) have their whole
  tree read; `auto` (discovery) and `from_paper` repos only record their head commit and
  emit later pushes. This keeps a flood of discovered repos from burying fresh pushes.
- Watchlist items are processed before other sources' backlog.
- A repo that errors is skipped for that run (WARNING log) without blocking the others;
  a GitHub rate limit ends the run but keeps the progress of repos already polled.
