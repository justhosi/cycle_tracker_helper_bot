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

Dates are always `YYYY-MM-DD`. Dates in the future are rejected, cycle length must
be 15–45 days, and period length must be shorter than the cycle.

## Files

| File | Purpose |
| --- | --- |
| `api/webhook.py` | Everything: cycle math, Telegram send, webhook handler |
| `requirements.txt` | Only dependency is `requests` |
| `.gitignore` | Keeps `.env`, `.vercel`, and `__pycache__/` out of git |

## How it is wired up

Vercel uses file-based routing, so the file's location *is* its URL:

```
api/webhook.py  ->  https://<your-project>.vercel.app/api/webhook
```

> The `<your-project>` above is a **placeholder** — replace it with your real
> Vercel project name (or your custom domain) before using that URL anywhere.

The file exposes `handler`, a `BaseHTTPRequestHandler`: `POST` handles Telegram
updates and always returns `200` (so Telegram does not retry), and `GET` returns a
plain "Cycle bot webhook is running." line you can check in a browser.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Deploy this repo to Vercel and note the resulting domain.
3. In Vercel, add the environment variable `TELEGRAM_BOT_TOKEN` under
   **Project → Settings → Environment Variables**, then redeploy.
4. Point Telegram at the endpoint (run this once, replacing the placeholders):
   ```bash
   curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
        -d "url=https://<your-project>.vercel.app/api/webhook"
   ```
5. Send `/start` to your bot to check it answers.

## Notes

- **Stateless.** The bot stores nothing; send the date fresh each message.
- **Estimates only.** Based on average cycle lengths, not a medical or fertility
  diagnosis. For contraception or irregular cycles, talk to a doctor.
