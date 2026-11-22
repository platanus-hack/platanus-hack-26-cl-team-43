# Demo script and checklist

Flow: push a jailbreak file to a watched GitHub repo, click "poll now", the candidate
appears, it is tested, a WhatsApp alert arrives, open the detail page. Target: under 5 minutes.

Nothing here needs code changes. Every step below uses real commands, routes and settings.

## 1. Prerequisites checklist

- [ ] `.env` filled (names from `.env.example`): `DATABASE_URL`, `GITHUB_TOKEN`,
      `OPENROUTER_API_KEY`, `EXTRACTOR_MODEL`, `JUDGE_MODEL`, `DAILY_BUDGET_USD`,
      `RESEND_API_KEY` + `EMAIL_FROM` (fallback channel), `TWILIO_SID`, `TWILIO_TOKEN`,
      `TWILIO_WHATSAPP_FROM`, `ALLOWED_EMAILS`, `SECRET_KEY`, `BASE_URL`, `PORT`.
- [ ] `SECRET_KEY` is at least 32 characters
      (`python -c "import secrets; print(secrets.token_urlsafe(48))"`), otherwise `radar run` refuses to start.
- [ ] `BASE_URL` is the URL the audience opens (the deployed one on stage); alert links use it.
- [ ] `ALLOWED_EMAILS` contains every presenter's login email.
- [ ] Each team phone joined the Twilio WhatsApp sandbox: from that phone, WhatsApp the
      sandbox number with `join <your-sandbox-code>` (Twilio console, Messaging, Try it out,
      WhatsApp sandbox). Wait for the confirmation reply. Sandbox joins expire after 72 h,
      so re-join the morning of the demo. `TWILIO_WHATSAPP_FROM` is the sandbox number in E.164.
- [ ] Subscriber rows with `min_severity = 0` (Tier 2 alerts have severity 0, so the default
      of 1 would hide them). Run once per phone against the demo database:

```sql
insert into subscribers (email, phone, channels, min_severity, active)
values ('<team-email>', '<+E164-phone>', array['whatsapp','email'], 0, true);
```

- [ ] `uv run radar migrate && uv run radar seed-corpus` done (once per database).
- [ ] Dashboard login works for every presenter (magic link by email, so Resend must work).

## 2. Demo repo setup

1. Create a public repo under the team account, e.g. `<team>/demo-prompts`. Initial
   commit: a harmless `README.md` only (no jailbreak file yet).
2. Log in to the dashboard, open `/sources`, and in the watch form (field `full_name`,
   format `owner/repo`) add `<team>/demo-prompts`. It is stored in `watch_repos` with status `seed`.
3. Click "poll now" on `github_watchlist` (`POST /sources/github_watchlist/poll-now`).
   This is the first-sight run. Repos added here are `seed`, so it reads the whole tree and
   only the README is ingested. (Repos found by discovery are `auto`: their first sight only
   records the head commit and ingests nothing.)
   Confirm on `/sources` that `github_watchlist` shows a fresh last run and an empty `last_error`.
4. From here every push is picked up through the diff path (`compare old...new`), which is
   the path the demo exercises. Only added lines of `.md .mkd .markdown .txt .json .yaml .yml .csv`
   files count. Watchlist items jump ahead of any other raw-item backlog.

## 3. Choosing the demo prompt (safety)

Requirements for the file you push:

- Public and already known (a published DAN-style or role-play template), not something we wrote.
- A templated prompt with a goal slot (the extractor stores `goal_slot`), so the radar
  substitutes its own goals, never the file's.
- `template_severity` low. Never use a template whose text itself carries harmful content.
  Goals are only the canary and the low-severity policy goals in `config/goals.yaml`.
- Alerts and the dashboard feed never print prompt text on stage beyond the detail page; do not read it aloud.

Dry run on a scratch *repo* (the watchlist reads only a repo's default branch, so a branch is never seen). Do this the day before, and again right before going on stage:

1. Create a separate public scratch repo, add it on `/sources` (see section 2), and push the candidate file to its default branch.
2. Locally: `uv run radar poll github_watchlist` (prints `github_watchlist: N new raw items`),
   then `uv run radar run`, wait for the raw item to be processed (runs every minute) and tested.
3. Open the candidate detail page and record in the table below which models it broke.
4. Pick a prompt that breaks at least one model. Frontier (Tier 1) break gives a normal
   alert; if only open models break, the alert is prefixed `[open model]` (Tier 2). That is
   the accepted fallback: we alert on open models. Say so out loud on stage.
5. Reset the demo database state (or use a fresh repo) so the stage run is novel: a prompt
   already seen is deduplicated and will not create a new candidate.

Dry-run record:

| Prompt file | Source URL | Template severity | Models broken (Tier 1) | Models broken (Tier 2) |
|---|---|---|---|---|
|  |  |  |  |  |

## 4. Stage script (target under 5 min)

| # | Action | Say | On screen | Time |
|---|---|---|---|---|
| 1 | Show `/feed` and `/sources` | "The radar watches public repos, datasets and papers for new jailbreaks." | Feed, counters, sources table with last runs | 0:00 |
| 2 | Push the jailbreak file to `<team>/demo-prompts` on GitHub | "Someone just published a new jailbreak template." | GitHub commit page | 0:30 |
| 3 | `/sources`, click "poll now" on `github_watchlist` | "Normally this runs every 10 minutes; I'll trigger it." | Redirect back to `/sources`, last run updates in seconds | 1:00 |
| 4 | Open `/feed` (rows refresh by htmx) | "It was extracted, deduplicated and scored for novelty." | New candidate row, status `queued` then `testing` | 1:30 |
| 5 | Wait for testing (raw items are processed each minute) | "Now it is tested against 8 models with harmless goals: a canary token and mild policy goals." | Status `testing`, then `broke N models` | 2:30 |
| 6 | Phones light up | "Subscribers get metadata and a link, never the prompt." | WhatsApp alert (`[open model]` prefix if Tier 2 only) | 3:30 |
| 7 | Tap the alert link or open `/candidates/<id>` | "Per-model results, goal by goal, verdicts from the judge." | Detail page: status, novelty, family, per-model stage and verdicts | 4:00 |
| 8 | Close | "New jailbreak found, tested and alerted in under five minutes." |  | 4:30 |

Tip: do step 2 before the talk starts and have the commit page ready, to save 30 s.
The poll itself takes seconds; the long poles are extraction/embedding (up to 1 min tick)
and the tester.

## 5. Fallbacks

- **No model breaks**: the candidate still appears. Open its detail page and show the
  per-model results and the status `no break`. Message: "tested against 8 models, none
  broke; the radar stays quiet, no alert noise". Then switch to a pre-tested candidate that did break.
- **WhatsApp delayed or Twilio failing**: the email channel fires too (subscriber has both
  channels). Failures are recorded in the `alerts` table as `failed: ...`:
  `select channel, status, sent_at from alerts order by id desc limit 5;`. Open the
  candidate detail page directly from the feed. Check the phone has an active sandbox join.
- **Budget paused**: the candidate shows `paused by budget`. The cap counts extraction too,
  so once it is hit new raw items also stay unprocessed (no candidate appears at all). Raise
  `DAILY_BUDGET_USD` (e.g. to 50.0) in the environment and restart the process; processing
  and the queue resume by themselves.
- **Collector error**: `/sources` shows the message in `last_error` for that collector.
  A 403 usually means a bad or rate-limited `GITHUB_TOKEN`. Fix the token, restart, poll again.
- **Poll-now seems to do nothing**: if the collector is already running, the click is skipped. The redirect still looks like success; the only trace is a WARNING in the server output ("maximum number of running instances reached"). Wait ~10 s and click again.
- **Nothing is novel** (prompt already deduplicated): use a fresh repo or a lightly reworded template.

## 6. Deploy notes

```sh
docker build -t jailbreak-radar .
docker run --rm --env-file .env -e PORT=8000 -p 8000:8000 jailbreak-radar
```

- The hosted Postgres must have `pgvector` (Supabase or Neon). Set `DATABASE_URL` to it.
- `radar run` migrates on start. Run `radar seed-corpus` once against the hosted DB
  (`docker run --rm --env-file .env jailbreak-radar uv run radar seed-corpus`); without it
  the novelty baseline is empty.
- Set `PORT` to what the platform expects (the image defaults to 8000), and `BASE_URL` to the public URL.
- Needs a long-lived process (Railway, Fly, a VM), not a serverless runtime.
- Shutdown caveat: on SIGTERM the process may skip the graceful shutdown (the scheduler
  and tester are stopped with `wait=False`). Jobs left `running` are reclaimed by `claim_next` only after 15 minutes, so that
  candidate will not resume for up to 15 minutes. Rule: do not restart or redeploy within
  15 minutes before the stage run. For rehearsals only, force it:
  `update test_jobs set status='queued', locked_at=null where status='running';`
- Repeat the whole stage script against the deployed instance before demo day.

## 7. Rehearsal timings

| Step | Local rehearsal | Deployed rehearsal | Notes |
|---|---|---|---|
| Push to GitHub |  |  |  |
| Poll now to raw item |  |  |  |
| Raw item to candidate in feed |  |  |  |
| Candidate to tested (Tier 1) |  |  |  |
| Candidate to tested (Tier 2) |  |  |  |
| WhatsApp alert received |  |  |  |
| Detail page opened |  |  |  |
| Total (target < 5:00) |  |  |  |
