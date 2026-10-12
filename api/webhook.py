"""
api/webhook.py

Single-file Telegram bot for Vercel's Python serverless runtime.
Everything (cycle math + Telegram send + webhook handler) lives in one
file on purpose, so there's nothing else to wire up or import correctly.

Vercel automatically turns this file into a live endpoint at:
    https://<your-project>.vercel.app/api/webhook

Env var required (set in Vercel dashboard, see README):
    TELEGRAM_BOT_TOKEN

Env var optional but strongly recommended (see README):
    TELEGRAM_WEBHOOK_SECRET
        Shared secret that must match the `secret_token` you pass to Telegram's
        setWebhook call. When set, every POST must carry the matching
        X-Telegram-Bot-Api-Secret-Token header or it gets a 403.
        Leave it unset and the endpoint accepts posts from anyone -- fine for
        local testing, not for a bot that is live on the internet.

Env vars optional, enabling per-chat memory of the last date (see README):
    KV_REST_API_URL, KV_REST_API_TOKEN
        Upstash Redis credentials. When BOTH are set, the last date a chat
        sent is stored and reused by a bare /cycle30 or /cycle command.
        When either is missing, nothing is stored or looked up and every
        command needs its own date -- the bot works fine in both modes.
"""

import os
import re
import json
import hmac  # constant-time string comparison for the webhook secret check
from http.server import BaseHTTPRequestHandler
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"
DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")

# The one line of HELP_TEXT that must not lie: whether dates are remembered
# depends on whether optional storage is configured, so build_help_text() swaps
# this sentence at send time instead of hard-coding either version.
STATELESS_SENTENCE = "I don't store anything — send the date fresh each time."
STORAGE_SENTENCE = (
    "I remember the last date you send in each chat, so `/cycle30` (or `/cycle`) "
    "on its own reuses it. Send a new date any time to update it."
)

HELP_TEXT = (
    "👋 *Dina's Cycle Tracker*\n\n"
    "Send me the first day of the last period as a date, and I'll tell you "
    "roughly where in the cycle you are today.\n\n"
    "*Menu commands* (tap the ☰ menu, then add a date):\n"
    "`/cycle30 2026-06-20` — 30-day cycle, default 5-day period\n"
    "`/cycle30_6 2026-06-20` — 30-day cycle, 6-day period\n\n"
    "*Or just send a bare date:*\n"
    "`2026-06-20` — uses default 28-day cycle, 5-day period\n\n"
    "*Advanced (any cycle/period length):*\n"
    "`/cycle 2026-06-20 32 7` — 32-day cycle, 7-day period\n\n"
    + STATELESS_SENTENCE + "\n\n"
    "_This gives an estimate based on averages, not a medical or fertility diagnosis. "
    "For contraception, fertility planning, or irregular cycles, please talk to a doctor._"
)

# Menu command presets: command name -> (cycle_length, period_length, label)
PRESET_COMMANDS = {
    "cycle30": (30, 5, "30-day cycle"),
    "cycle30_6": (30, 6, "30-day cycle, 6-day period"),
}


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


# ---------- persistent storage (optional) ----------

# ---------------------------------------------------------------------------
# Optional persistent storage via Upstash Redis (from the Vercel Marketplace).
#
# We talk to it over plain HTTPS using the `requests` library we already
# depend on, so no extra package is required. Vercel injects two env vars:
#   KV_REST_API_URL    -> HTTPS endpoint of the database
#   KV_REST_API_TOKEN  -> secret token authorizing reads/writes
#
# If those vars are missing (storage not set up yet), the helper functions
# simply do nothing, so the bot keeps working in its current stateless mode.
# ---------------------------------------------------------------------------

KV_URL = os.environ.get("KV_REST_API_URL", "")
KV_TOKEN = os.environ.get("KV_REST_API_TOKEN", "")


def kv_enabled() -> bool:
    """True only when both Upstash env vars are present (storage is configured)."""
    return bool(KV_URL and KV_TOKEN)


def kv_command(*args):
    """
    Run one Redis command through Upstash's REST API.

    We POST a JSON array like ["SET", "user:123", "{...}"] to the base URL.
    Returns the command's result, e.g. "OK" for SET or the stored string for GET.
    """
    resp = requests.post(
        KV_URL,
        headers={
            "Authorization": f"Bearer {KV_TOKEN}",
            "Content-Type": "application/json",
        },
        json=list(args),   # Upstash expects the command as a JSON array
        timeout=5,
    )
    resp.raise_for_status()
    return resp.json().get("result")


def save_user(chat_id, data: dict) -> None:
    """Persist one user's data as a small JSON blob under key 'user:<chat_id>'."""
    if not kv_enabled():
        return  # storage not configured -> stay stateless, don't crash
    kv_command("SET", f"user:{chat_id}", json.dumps(data))


def load_user(chat_id):
    """Return the stored dict for a user, or None if we've never seen them."""
    if not kv_enabled():
        return None
    raw = kv_command("GET", f"user:{chat_id}")
    return json.loads(raw) if raw else None


def remember_last_period(chat_id, last_period_date, cycle_length, period_length) -> bool:
    """
    Best-effort write of one chat's last period date (plus the lengths used with
    it), stored as an ISO string because JSON has no date type.

    Swallows every storage error on purpose: a Redis outage, a bad token or a
    quota error must never stop the bot from replying. Worst case the date
    simply isn't remembered and the user is asked for it again next time.

    Returns True only when the write really succeeded, so the caller never
    promises "saved for this chat" about a write that quietly failed.
    """
    if not kv_enabled():
        return False
    try:
        save_user(chat_id, {
            "last_period": last_period_date.isoformat(),
            "cycle_length": cycle_length,
            "period_length": period_length,
        })
        return True
    except Exception as e:  # noqa: BLE001 - storage is optional, never fatal
        print(f"Storage write failed (continuing statelessly): {e}")
        return False


def recall_last_period(chat_id):
    """
    Best-effort read of a chat's remembered date.

    Returns (last_period_date, cycle_length, period_length), or None when
    storage is off, nothing was ever saved, or the stored blob is unreadable.
    Never raises, for the same reason as remember_last_period() above.
    """
    if not kv_enabled():
        return None
    try:
        saved = load_user(chat_id) or {}
        iso = saved.get("last_period")
        if not iso:
            return None
        return (
            date.fromisoformat(iso),
            int(saved.get("cycle_length") or 28),
            int(saved.get("period_length") or 5),
        )
    except Exception as e:  # noqa: BLE001 - corrupt data shouldn't break replies
        print(f"Storage read failed (continuing statelessly): {e}")
        return None


def build_help_text() -> str:
    """
    Help text for /start and /help, with the remembering sentence matched to
    reality: claiming "I don't store anything" while quietly saving dates would
    be false, and promising memory when storage is off would be worse.
    """
    note = STORAGE_SENTENCE if kv_enabled() else STATELESS_SENTENCE
    return HELP_TEXT.replace(STATELESS_SENTENCE, note)


def missing_date_reply(command, label):
    """
    Reply for a bare /command that arrived with no date in it.

    Two flavours, because the fix differs: with storage on the user sends a date
    once and never again, while with storage off they need one every time.
    """
    example = f"/{command} 2026-06-20"
    if kv_enabled():
        return (
            f"I don't have a date saved for this chat yet. Send the first day of "
            f"your last period once, e.g. `{example}`, and after that `/{command}` "
            f"on its own will reuse it."
        )
    if label:
        return f"For the {label}, send the date too, e.g. `{example}`."
    return f"Send the date too, e.g. `{example}`."


# ---------- message parsing ----------

def extract_date(text):
    """Find a YYYY-MM-DD date anywhere in the text. Returns a date or None."""
    match = DATE_RE.search(text)
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    return datetime(year, month, day).date()


def parse_freeform(text):
    """
    Parse a bare date or a generic '/cycle DATE [cycle_len] [period_len]'
    message, defaulting to a 28-day cycle / 5-day period.
    """
    match = DATE_RE.search(text)
    if not match:
        return None
    last_period_date = extract_date(text)
    numbers = re.findall(r"\b(\d{1,2})\b", text[match.end():])
    cycle_length = int(numbers[0]) if len(numbers) >= 1 else 28
    period_length = int(numbers[1]) if len(numbers) >= 2 else 5
    return last_period_date, cycle_length, period_length


def split_command(text):
    """
    If text starts with a Telegram command (e.g. '/cycle30 2026-06-20' or
    '/cycle30@YourBot 2026-06-20'), return (command_name, rest_of_text).
    Otherwise return (None, text).
    """
    if not text.startswith("/"):
        return None, text
    parts = text.split(maxsplit=1)
    command = parts[0][1:].split("@")[0].lower()
    rest = parts[1] if len(parts) > 1 else ""
    return command, rest


def handle_update(update):
    message = update.get("message")
    if not message or "text" not in message:
        return
    chat_id = message["chat"]["id"]
    text = message["text"].strip()

    command, rest = split_command(text)

    if command in ("start", "help"):
        send_message(chat_id, build_help_text())
        return

    # Work out this message's date and lengths. `from_memory` tracks whether the
    # date came out of storage instead of the message: we say so in the reply,
    # and we skip re-saving something we just read.
    from_memory = False

    if command in PRESET_COMMANDS:
        # Preset commands carry their own cycle/period lengths, so only the date
        # can come from memory -- and only when the user left it out.
        cycle_length, period_length, label = PRESET_COMMANDS[command]
        last_period_date = extract_date(rest)
        if not last_period_date:
            remembered = recall_last_period(chat_id)
            if remembered:
                last_period_date, _, _ = remembered  # preset lengths win
                from_memory = True
            else:
                send_message(chat_id, missing_date_reply(command, label))
                return

    elif command == "cycle" and extract_date(rest) is None:
        # Bare `/cycle` states neither a date nor lengths, so reuse both from
        # memory -- that is what makes it a useful "same as last time" shortcut.
        remembered = recall_last_period(chat_id)
        if not remembered:
            send_message(chat_id, missing_date_reply("cycle", None))
            return
        last_period_date, cycle_length, period_length = remembered
        from_memory = True

    else:
        # A bare date, or `/cycle <date> [cycle_len] [period_len]`.
        parsed = parse_freeform(text)
        if not parsed:
            # Text with no date in it. Deliberately NOT answered from memory: a
            # stray "hi" should never produce a cycle report, so remembered
            # dates are only ever used by the explicit /commands handled above.
            send_message(
                chat_id,
                "I couldn't find a date in that message. Try `YYYY-MM-DD`, e.g. `2026-06-20`.",
            )
            return
        last_period_date, cycle_length, period_length = parsed

    try:
        status = get_cycle_status(last_period_date, cycle_length, period_length)
    except ValueError as e:
        # Nonsense numbers: tell the user, and remember nothing invalid.
        send_message(chat_id, f"⚠️ {e}")
        return

    reply = format_status_message(status)

    if from_memory:
        # Name the date we reused, otherwise the user cannot tell whether they
        # are looking at today's info or something they typed weeks ago.
        date_note = f"_Using the date saved for this chat: {last_period_date.isoformat()}_"
        if (date.today() - last_period_date).days >= status.cycle_length:
            date_note += (
                "\n_Heads up: that date is more than one cycle old, so treat this "
                "as a rough guess and send a fresh date when you can._"
            )
        reply = f"{date_note}\n\n{reply}"
    else:
        # A fresh, valid date: remember it so the next bare command can reuse
        # it. Only promise "saved" when the write actually went through.
        if remember_last_period(chat_id, last_period_date, cycle_length, period_length):
            reply += (
                "\n\n_Saved for this chat — next time you can send the command "
                "without a date._"
            )

    send_message(chat_id, reply)


# ----------------------------------------------------------------------------
# Security: verify the request genuinely came from Telegram.
# When you register the webhook you pass `secret_token`; Telegram then includes
# it in every request as the header "X-Telegram-Bot-Api-Secret-Token".
# We compare it to our own env var and reject anything that doesn't match.
# This stops strangers from pretending to be Telegram and abusing the endpoint.
# ----------------------------------------------------------------------------
def _is_from_telegram(headers) -> bool:
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if not expected:
        return True  # no secret configured -> don't lock anyone out (dev only)

    # `or ""` turns a missing header (None) into an empty string, so the
    # comparison below always gets two strings instead of blowing up on None.
    supplied = headers.get("X-Telegram-Bot-Api-Secret-Token") or ""

    # Compare as BYTES, not str. Two reasons:
    #   * hmac.compare_digest is constant-time: it never short-circuits on the
    #     first mismatching character, so the time it takes leaks no hint about
    #     how much of the secret an attacker guessed correctly. Plain `==` does
    #     short-circuit, which is a (minor) timing side channel.
    #   * The str form of compare_digest raises TypeError on non-ASCII input,
    #     and HTTP headers can smuggle non-ASCII bytes in. Encoding to bytes
    #     means a junk header simply fails to match instead of raising an error
    #     outside our try/except in do_POST.
    return hmac.compare_digest(supplied.encode(), expected.encode())


# ---------- Vercel entry point ----------

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Refuse anyone who isn't Telegram *before* touching the payload, so a
        # random visitor can never reach the cycle logic or the bot token.
        # 403 (not 200) on purpose: it shows up loudly in the Vercel logs, and
        # only impostors get it -- a real Telegram request always carries the
        # header, so real traffic is unaffected.
        if not _is_from_telegram(self.headers):
            print("Rejected request: missing/incorrect X-Telegram-Bot-Api-Secret-Token")
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": false, "error": "forbidden"}')
            return

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
