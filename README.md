# Dina's Cycle Tracker Bot

A tiny Telegram bot that lives in a **single file** and runs as a Vercel Python
serverless function. Send it the first day of the last period; it replies with the
current cycle day, phase, a progress bar, and the estimated next period date.

## Commands

| You send | What it means |
| --- | --- |
| `/start` or `/help` | Show the help text / menu |
| `/cycle30 2026-06-20` | 30-day cycle, default 5-day period |
| `/cycle30_6 2026-06-20` | 30-day cycle, 6-day period |
| `/cycle 2026-06-20 32 7` | Any cycle length, any period length |
| `2026-06-20` | Bare date → defaults to a 28-day cycle, 5-day period |
| `/cycle30` (no date) | Reuses the last date saved for this chat |
| `/cycle` (no date) | Reuses the saved date *and* its cycle/period lengths |

Dates are always `YYYY-MM-DD`. Dates in the future are rejected, cycle length must
be 15–45 days, and period length must be shorter than the cycle. "Today" means
today in **Turkey** (`Europe/Istanbul`), not on the server, which runs in UTC.

## Files

| File | Purpose |
| --- | --- |
| `api/webhook.py` | Everything: cycle math, Telegram send, webhook handler |
| `vercel.json` | Tells Vercel to use **file-based `/api` functions** (not a framework preset) |
| `requirements.txt` | Only dependency is `requests` |
| `.gitignore` | Keeps `.env`, `.vercel`, and `__pycache__/` out of git |

## How it is wired up

Vercel uses file-based routing, so the file's location *is* its URL:

```
api/webhook.py  ->  https://<your-project>.vercel.app/api/webhook
```

> The `<your-project>` above is a **placeholder** — replace it with your real
> Vercel project name (or your custom domain) before using that URL anywhere.

The file exposes `handler`, a `BaseHTTPRequestHandler`. `POST` first checks the
secret header (see step 4 below), then handles the update and returns `200` so
Telegram does not retry; a request that fails the check gets `403` and is never
processed. `GET` returns a plain "Cycle bot webhook is running." line you can
check in a browser.

## Deployment gotcha: the "No python entrypoint found" error

If a deploy fails with:

```
Error: No python entrypoint found in default locations, but found potential
entrypoints: api/webhook.py (variable: handler)
```

…then Vercel is treating the project as a **Python framework app** instead of a
folder of file-based `/api` functions. A framework preset takes precedence over
`/api` files, and framework mode only looks for entrypoints in a few fixed
locations (`app.py`, `index.py`, `server.py`, `main.py`, `wsgi.py`, `asgi.py`,
or the same names inside `src/` or `app/`) that must define `app` or
`application`. It saw our `handler` in `api/webhook.py` but refused it because
that file is not an approved entrypoint location.

There are two ways to fix it — **pick one**:

1. **Config (done for you).** `vercel.json` sets `"framework": null`, which is
   the "Other" preset. This overrides the dashboard's Framework Preset and makes
   each `.py` file under `api/` its own Vercel Function. Nothing else changes.
2. **Dashboard.** Go to **Project → Settings → Build and Development Settings →
   Framework Preset** and select **Other**, then redeploy.

The `functions` block in `vercel.json` additionally sets this function's memory
and max duration; it is optional and only cosmetic/tuning.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Deploy this repo to Vercel and note the resulting domain.
   - Make sure the **Framework Preset** is **Other** (or leave `vercel.json`
     in place, which forces it for you).
3. In Vercel, add the environment variable `TELEGRAM_BOT_TOKEN` under
   **Project → Settings → Environment Variables**, then redeploy.
4. Optional but recommended: also add `TELEGRAM_WEBHOOK_SECRET` in Vercel — a
   random string of letters, digits, `_` and `-` (no spaces). Telegram will echo
   it back on every request, and posts without it are rejected with a `403`.
5. Optional — per-chat memory. Add an Upstash Redis database from the Vercel
   Marketplace (**Storage → Create → Upstash**); it injects its credentials into
   the project. With those present, the last date each chat sends is saved, so a
   bare `/cycle30` or `/cycle` reuses it instead of asking again. Worth reading
   the next paragraph before you rely on it.
6. Point Telegram at the endpoint. Pass the same secret as `secret_token`, or
   omit that flag if you skipped step 4:
   ```bash
   curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
        -d "url=https://<your-project>.vercel.app/api/webhook" \
        -d "secret_token=<TELEGRAM_WEBHOOK_SECRET>"
   ```
7. Send `/start` to your bot to check it answers.

> Storage env vars: the code reads `KV_REST_API_URL` and `KV_REST_API_TOKEN`.
> Depending on which integration version Vercel installs, the variables may
> arrive under those names or as `UPSTASH_REDIS_REST_URL` /
> `UPSTASH_REDIS_REST_TOKEN`. Check **Project → Settings → Environment
> Variables** and rename the two constants at the top of the storage section in
> `api/webhook.py` if they differ. A mismatch is silent: nothing is stored, no
> error is raised, and the bot keeps working — it just never remembers dates.

> Warning: `TELEGRAM_WEBHOOK_SECRET` and Telegram's `secret_token` must match. If
> they don't (or you set the env var but registered without the flag), every
> update is rejected and the bot goes quiet — the Vercel logs will show
> `Rejected request: missing/incorrect X-Telegram-Bot-Api-Secret-Token`.

## Notes

- **Memory is optional.** With Upstash credentials configured (step 5), the last
  date a chat sends is saved and reused by bare `/cycle30` / `/cycle` commands;
  a reply that reused one says which date it used. Without storage the bot keeps
  nothing and every command needs its own date. Either way a failed storage call
  never blocks a reply — it just falls back to asking for the date.
- **Estimates only.** Based on average cycle lengths, not a medical or fertility
  diagnosis. For contraception or irregular cycles, talk to a doctor.
