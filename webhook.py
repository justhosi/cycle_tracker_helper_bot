"""
api/webhook.py

Single-file Telegram bot for Vercel's Python serverless runtime.
Everything (cycle math + Telegram send + webhook handler) lives in one
file on purpose, so there's nothing else to wire up or import correctly.

Vercel automatically turns this file into a live endpoint at:
    https://<your-project>.vercel.app/api/webhook

Env var required (set in Vercel dashboard, see README):
    TELEGRAM_BOT_TOKEN
"""

import os
import re
import json
from http.server import BaseHTTPRequestHandler
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"
DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")

HELP_TEXT = (
    "👋 *Cycle Tracker Bot*\n\n"
    "Send me the first day of the last period as a date, and I'll tell you "
    "roughly where in the cycle you are today.\n\n"
    "*Examples:*\n"
    "`2026-06-20` — uses default 28-day cycle, 5-day period\n"
    "`/cycle 2026-06-20 30` — custom 30-day cycle\n"
    "`/cycle 2026-06-20 30 6` — custom 30-day cycle, 6-day period\n\n"
    "I don't store anything — send the date fresh each time.\n\n"
    "_This gives an estimate based on averages, not a medical or fertility diagnosis. "
    "For contraception, fertility planning, or irregular cycles, please talk to a doctor._"
)


# ---------- cycle math ----------

@dataclass
class CycleStatus:
    day_in_cycle: int
    cycle_length: int
    phase: str
    phase_emoji: str
    description: str
    days_until_next_period: int
    estimated_next_period_date: date
    is_fertile_window: bool


def get_cycle_status(last_period_start, cycle_length=28, period_length=5, today=None):
    today = today or date.today()

    if last_period_start > today:
        raise ValueError("Last period date can't be in the future.")
    if cycle_length < 15 or cycle_length > 45:
        raise ValueError("Cycle length should realistically be between 15 and 45 days.")
    if period_length < 1 or period_length >= cycle_length:
        raise ValueError("Period length must be at least 1 day and shorter than the cycle.")

    days_since = (today - last_period_start).days
    day_in_cycle = (days_since % cycle_length) + 1

    ovulation_day = max(cycle_length - 14, period_length + 1)
    fertile_start = max(ovulation_day - 2, period_length + 1)
    fertile_end = min(ovulation_day + 1, cycle_length)

    if day_in_cycle <= period_length:
        phase, emoji, description = "Menstrual", "🩸", "Period is likely active."
    elif day_in_cycle < fertile_start:
        phase, emoji, description = "Follicular", "🌱", "Post-period, pre-fertile window. Estrogen is rising."
    elif fertile_start <= day_in_cycle <= fertile_end:
        phase, emoji, description = "Ovulation / Fertile window", "🥚", "Estimated fertile window, including likely ovulation."
    else:
        phase, emoji, description = "Luteal", "🌙", "Post-ovulation. PMS symptoms may appear later in this phase."

    days_until_next_period = cycle_length - day_in_cycle + 1
    estimated_next_period_date = today + timedelta(days=days_until_next_period)

    return CycleStatus(
        day_in_cycle=day_in_cycle,
        cycle_length=cycle_length,
        phase=phase,
        phase_emoji=emoji,
        description=description,
        days_until_next_period=days_until_next_period,
        estimated_next_period_date=estimated_next_period_date,
        is_fertile_window=(fertile_start <= day_in_cycle <= fertile_end),
    )


def format_status_message(status: CycleStatus) -> str:
    bar_length = 20
    filled = round((status.day_in_cycle / status.cycle_length) * bar_length)
    bar = "▓" * filled + "░" * (bar_length - filled)

    lines = [
        f"{status.phase_emoji} *{status.phase} phase*",
        f"Day *{status.day_in_cycle}* of {status.cycle_length}",
        f"`{bar}`",
        "",
        status.description,
        "",
        f"🗓 Next period estimated: *{status.estimated_next_period_date.strftime('%b %d, %Y')}* "
        f"(in {status.days_until_next_period} day{'s' if status.days_until_next_period != 1 else ''})",
    ]
    if status.is_fertile_window:
        lines.insert(1, "⚠️ Currently in the estimated fertile window.")

    lines.append("")
    lines.append("_This is an estimate based on averages, not a medical prediction._")
    return "\n".join(lines)


# ---------- telegram send ----------

def send_message(chat_id, text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"{TELEGRAM_API_BASE.format(token=token)}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)


# ---------- message parsing ----------

def parse_message(text):
    match = DATE_RE.search(text)
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    last_period_date = datetime(year, month, day).date()
    numbers = re.findall(r"\b(\d{1,2})\b", text[match.end():])
    cycle_length = int(numbers[0]) if len(numbers) >= 1 else 28
    period_length = int(numbers[1]) if len(numbers) >= 2 else 5
    return last_period_date, cycle_length, period_length


def handle_update(update):
    message = update.get("message")
    if not message or "text" not in message:
        return
    chat_id = message["chat"]["id"]
    text = message["text"].strip()

    if text in ("/start", "/help"):
        send_message(chat_id, HELP_TEXT)
        return

    parsed = parse_message(text)
    if not parsed:
        send_message(chat_id, "I couldn't find a date in that message. Try `YYYY-MM-DD`, e.g. `2026-06-20`.")
        return

    last_period_date, cycle_length, period_length = parsed
    try:
        status = get_cycle_status(last_period_date, cycle_length, period_length)
    except ValueError as e:
        send_message(chat_id, f"⚠️ {e}")
        return

    send_message(chat_id, format_status_message(status))


# ---------- Vercel entry point ----------

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            update = json.loads(body or b"{}")
            handle_update(update)
        except Exception as e:  # noqa: BLE001 - always answer Telegram with 200
            print(f"Error handling update: {e}")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def do_GET(self):
        # Handy for checking the endpoint is alive from a browser.
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Cycle bot webhook is running.")
