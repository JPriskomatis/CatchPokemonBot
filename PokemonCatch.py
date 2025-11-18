# toasty_reminder_bot.py
import os
import re
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Optional, Dict, Set

# -------------------------
# Configuration
# -------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("DISCORD_TOKEN not found in environment")

# Replace with the Toasty bot ID provided
TOASTY_ID = 208946659361554432

TRACKED_FILE = "tracked_users.json"
TIMERS_FILE = "active_timers.json"
DEBUG_LOG_FILE = "bot_debug.log"

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True

bot = commands.Bot(command_prefix="$", intents=intents)

# -------------------------
# Logging (file + console)
# -------------------------
logger = logging.getLogger("toasty_reminder")
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s")

# File handler
fh = logging.FileHandler(DEBUG_LOG_FILE, encoding="utf-8")
fh.setLevel(logging.DEBUG)
fh.setFormatter(formatter)
logger.addHandler(fh)

# Console handler
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(formatter)
logger.addHandler(ch)

logger.info("Logger initialized")

# -------------------------
# Persistence helpers
# -------------------------
def load_tracked_users() -> Set[int]:
    try:
        with open(TRACKED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            logger.debug(f"Loaded tracked users: {data}")
            return set(int(x) for x in data)
    except FileNotFoundError:
        logger.debug("No tracked_users.json found; starting with empty set")
        return set()

def save_tracked_users(tracked: Set[int]):
    with open(TRACKED_FILE, "w", encoding="utf-8") as f:
        json.dump(list(tracked), f)
    logger.debug(f"Saved tracked users: {list(tracked)}")

def load_active_timers() -> Dict[int, float]:
    """Return dict user_id -> timestamp (UTC epoch seconds) for timers still in future."""
    timers = {}
    try:
        with open(TIMERS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
            for user_id_str, ts in saved.items():
                try:
                    user_id = int(user_id_str)
                    end_time = datetime.fromtimestamp(float(ts), tz=timezone.utc)
                    if end_time > datetime.now(timezone.utc):
                        timers[user_id] = float(ts)
                        logger.debug(f"Loaded active timer for {user_id} -> {end_time.isoformat()}")
                    else:
                        logger.debug(f"Ignoring expired saved timer for {user_id} -> {end_time.isoformat()}")
                except Exception as e:
                    logger.exception(f"Error loading timer entry {user_id_str}: {e}")
    except FileNotFoundError:
        logger.debug("No active_timers.json found; starting with empty timers")
    return timers

def save_active_timers(timers: Dict[int, float]):
    # timers: user_id -> epoch timestamp (float)
    with open(TIMERS_FILE, "w", encoding="utf-8") as f:
        json.dump({str(uid): ts for uid, ts in timers.items()}, f)
    logger.debug(f"Saved active timers: {timers}")

# -------------------------
# In-memory state
# -------------------------
tracked_users: Set[int] = load_tracked_users()
# active_timers_map for persistence: user_id -> epoch timestamp (UTC)
active_timers_map: Dict[int, float] = load_active_timers()
# running asyncio tasks for timers: user_id -> Task
active_timer_tasks: Dict[int, asyncio.Task] = {}

logger.info(f"Startup: loaded {len(tracked_users)} tracked users and {len(active_timers_map)} pending timers")

# -------------------------
# Utility: extract text from message (content + embeds)
# -------------------------
def extract_message_text(msg: discord.Message) -> str:
    parts = []
    if msg.content:
        parts.append(msg.content)
    # For embeds, extract title + description + fields if present
    for e in msg.embeds:
        try:
            if getattr(e, "title", None):
                parts.append(str(e.title))
            if getattr(e, "description", None):
                parts.append(str(e.description))
            # some embeds have fields
            if getattr(e, "fields", None):
                for field in e.fields:
                    if getattr(field, "name", None):
                        parts.append(str(field.name))
                    if getattr(field, "value", None):
                        parts.append(str(field.value))
        except Exception:
            # defensive: some embed structures might be odd
            logger.exception("Error extracting embed text")
    combined = "\n".join(p for p in parts if p).strip()
    logger.debug(f"Extracted text from msg {msg.id} (author={getattr(msg.author,'id',None)}): {combined!r}")
    return combined

# -------------------------
# Utility: parse cooldown durations in many formats
# -------------------------
# Matches:
#  - "1h 2m 3s", "1 hr 2 min", "1 hour and 2 minutes"
#  - "01:23:45" (hh:mm:ss) or "12:34" (mm:ss)
#  - "1 hour", "2 minutes", "30s", "30 sec", "2 mins"
duration_regex_hms = re.compile(
    r"(?:(?P<hours>\d+)\s*(?:h|hr|hrs|hour|hours))?"
    r"[\s,]*(?:(?P<minutes>\d+)\s*(?:m|min|mins|minute|minutes))?"
    r"[\s,]*(?:(?P<seconds>\d+)\s*(?:s|sec|secs|second|seconds))?",
    re.IGNORECASE,
)

colon_time_regex = re.compile(r"\b(?:(\d{1,2}):(\d{2}):(\d{2}))\b")  # hh:mm:ss
colon_time_regex2 = re.compile(r"\b(?:(\d{1,2}):(\d{2}))\b")  # mm:ss or hh:mm depending on context

def parse_duration_from_text(text: str) -> Optional[int]:
    """Return total seconds or None if cannot parse."""
    if not text:
        return None

    text = text.strip()
    logger.debug(f"Parsing duration from text: {text!r}")

    # 1) Try hh:mm:ss first
    m = colon_time_regex.search(text)
    if m:
        h, mm, ss = map(int, m.groups())
        total = h * 3600 + mm * 60 + ss
        logger.debug(f"Parsed hh:mm:ss -> {total} seconds")
        return total

    # 2) Try hh:mm or mm:ss (ambiguous). We treat two-group colon as mm:ss if hours not present.
    m2 = colon_time_regex2.search(text)
    if m2:
        a, b = map(int, m2.groups())
        # If there's also a mention of 'hour' near the match, treat as h:m. Otherwise treat as m:s.
        # Heuristic: if the whole text contains 'hour' or 'hr' treat as hours:minutes
        context_lower = text.lower()
        if re.search(r"\b(h|hr|hour|hours)\b", context_lower):
            total = a * 3600 + b * 60
            logger.debug(f"Parsed hh:mm (heuristic hour word present) -> {total} seconds")
            return total
        else:
            total = a * 60 + b
            logger.debug(f"Parsed mm:ss -> {total} seconds")
            return total

    # 3) Try textual forms like "1 hour 2 minutes 3 seconds"
    m3 = duration_regex_hms.search(text)
    if m3:
        h = int(m3.group("hours")) if m3.group("hours") else 0
        mm = int(m3.group("minutes")) if m3.group("minutes") else 0
        s = int(m3.group("seconds")) if m3.group("seconds") else 0
        total = h * 3600 + mm * 60 + s
        if total > 0:
            logger.debug(f"Parsed textual h/m/s -> {total} seconds (h={h}, m={mm}, s={s})")
            return total

    # 4) Another pattern: "in 3 hours", "available in 5 mins", "Cooldown: 3 hours 12 minutes"
    # We'll look for any "<number> (hour|min|sec|...)" occurrences and sum them.
    tokens = re.findall(r"(\d+)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes|s|sec|secs|second|seconds)\b", text, re.IGNORECASE)
    if tokens:
        total = 0
        for num, unit in tokens:
            n = int(num)
            unit = unit.lower()
            if unit.startswith("h"):
                total += n * 3600
            elif unit.startswith("m"):
                total += n * 60
            else:
                total += n
        if total > 0:
            logger.debug(f"Parsed tokens -> {total} seconds from {tokens}")
            return total

    logger.debug("No duration parsed")
    return None

# -------------------------
# Wait for Toasty numbers (reads multiple messages + embeds)
# -------------------------
async def wait_for_toasty_numbers(channel: discord.abc.Messageable, timeout: float = 15.0) -> Optional[int]:
    """
    Wait for next bot message(s) from Toasty in this channel containing valid cooldown numbers.
    Returns total_seconds or None on timeout/failure.
    """
    def check(msg: discord.Message):
        return msg.channel == channel and getattr(msg.author, "id", None) == TOASTY_ID

    logger.debug(f"Waiting for Toasty messages in channel {getattr(channel,'id',None)} (timeout={timeout})")
    # Wait for first Toasty message
    try:
        first_msg = await bot.wait_for("message", check=check, timeout=timeout)
    except asyncio.TimeoutError:
        logger.debug("Timeout waiting for first Toasty message")
        return None

    messages = [first_msg]
    # Give a short window (2.5s) to collect subsequent bot messages (embeds etc.)
    start = datetime.now(timezone.utc)
    while (datetime.now(timezone.utc) - start).total_seconds() < 2.5:
        try:
            m = await bot.wait_for("message", check=check, timeout=0.5)
            messages.append(m)
        except asyncio.TimeoutError:
            break

    combined = "\n".join(extract_message_text(m) for m in messages if m)
    logger.debug(f"Combined Toasty text collected: {combined!r}")

    # First check success phrases
    success_patterns = [
        r"\bcaught\b",
        r"\byou caught\b",
        r"\bsuccessfully\b",
        r"\bcaught a\b",
        r"\byou have caught\b",
        r"\bgot it\b",
    ]
    if any(re.search(pat, combined, re.IGNORECASE) for pat in success_patterns):
        logger.debug("Detected a 'caught' success message from Toasty")
        return 3 * 3600  # fixed 3-hour cooldown for catches

    # Otherwise, try parse duration
    total = parse_duration_from_text(combined)
    if total:
        logger.debug(f"Parsed duration from Toasty: {total} seconds")
        return total

    logger.debug("Failed to parse duration or success from Toasty messages")
    return None

# -------------------------
# Timer task management
# -------------------------
async def cooldown_timer_task(user: discord.User, channel: discord.abc.Messageable, total_seconds: int):
    user_id = user.id
    try:
        logger.info(f"Timer started for user {user_id}: {total_seconds} seconds")
        await asyncio.sleep(total_seconds)
        # Send reminder
        try:
            await channel.send(f"{user.mention}, your cooldown is over! Catch them all!")
            logger.info(f"Sent cooldown message to user {user_id} in channel {getattr(channel,'id',None)}")
        except Exception:
            logger.exception(f"Failed to send reminder message to {user_id}")
    finally:
        # Clean up
        active_timer_tasks.pop(user_id, None)
        active_timers_map.pop(user_id, None)
        save_active_timers(active_timers_map)
        logger.debug(f"Timer cleaned up for user {user_id}")

def schedule_timer_for_user(user: discord.User, channel: discord.abc.Messageable, total_seconds: int):
    """Schedule a timer and keep a handle so we can cancel/reschedule."""
    user_id = user.id
    # Cancel existing task if any
    existing = active_timer_tasks.get(user_id)
    if existing and not existing.done():
        existing.cancel()
        logger.info(f"Cancelled existing timer task for user {user_id}")

    # compute end timestamp
    end_ts = (datetime.now(timezone.utc) + timedelta(seconds=total_seconds)).timestamp()
    active_timers_map[user_id] = end_ts
    save_active_timers(active_timers_map)

    # ➜ CREATE THE TIMER TASK HERE (NO MESSAGE OBJECT!)
    task = asyncio.create_task(cooldown_timer_task(user, channel, total_seconds))
    active_timer_tasks[user_id] = task
    logger.debug(f"Scheduled timer task for {user_id}, ends at {datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat()}")

# -------------------------
# Bot events and commands
# -------------------------
@bot.event
async def on_ready():
    logger.info(f"{bot.user} is online! Preparing pending timers...")
    # Recreate tasks for timers loaded from disk
    for user_id, end_ts in list(active_timers_map.items()):
        remaining = end_ts - datetime.now(timezone.utc).timestamp()
        if remaining > 0:
            # We don't have channel or user object here; we will not attempt to re-create accurate channel context.
            # Best-effort: try to find a DM channel for the user, otherwise the original channel context is lost.
            user = bot.get_user(user_id)
            if user is None:
                try:
                    user = await bot.fetch_user(user_id)
                except Exception:
                    logger.exception(f"Failed to fetch user {user_id} at startup, removing timer")
                    active_timers_map.pop(user_id, None)
                    continue

            # Create a DM channel to notify user (fallback) — user might prefer the original channel but we can't recover it reliably.
            try:
                dm = await user.create_dm()
                schedule_timer_for_user(user, dm, int(remaining))
                logger.info(f"Rescheduled timer for user {user_id} (remaining {int(remaining)}s) via DM")
            except Exception:
                logger.exception(f"Failed to create DM for user {user_id}; removing timer")
                active_timers_map.pop(user_id, None)
    save_active_timers(active_timers_map)

@bot.command(name="catchPokemon")
async def add_tracked_user(ctx: commands.Context):
    uid = ctx.author.id
    if uid in tracked_users:
        await ctx.send(f"{ctx.author.mention}, you are already being tracked for Pokémon cooldowns!")
        logger.debug(f"User {uid} attempted to add while already tracked")
        return

    tracked_users.add(uid)
    save_tracked_users(tracked_users)
    await ctx.send(f"{ctx.author.mention}, I'm onto you now ;)")
    logger.info(f"Added tracked user {uid}")

@bot.command(name="untrackPokemon")
async def remove_tracked_user(ctx: commands.Context):
    uid = ctx.author.id
    if uid not in tracked_users:
        await ctx.send(f"{ctx.author.mention}, you are not being tracked.")
        logger.debug(f"User {uid} attempted to untrack but wasn't tracked")
        return

    tracked_users.discard(uid)
    save_tracked_users(tracked_users)
    # Also cancel timer if present
    task = active_timer_tasks.pop(uid, None)
    if task and not task.done():
        task.cancel()
        logger.info(f"Cancelled active timer when user {uid} requested untrack")
    active_timers_map.pop(uid, None)
    save_active_timers(active_timers_map)

    await ctx.send(f"{ctx.author.mention}, you've been removed from tracking.")
    logger.info(f"Removed tracked user {uid}")

@bot.event
async def on_message(message: discord.Message):
    # Let commands still work
    await bot.process_commands(message)

    # Ignore our own messages
    if message.author.id == bot.user.id:
        return

    # Only respond to ;pokemon if the user is tracked
    if message.content.strip().lower() != ";pokemon":
        return

    uid = message.author.id
    if uid not in tracked_users:
        logger.debug(f"User {uid} used ;pokemon but is not tracked")
        return

    logger.info(f"Received ;pokemon from tracked user {uid} in channel {getattr(message.channel,'id',None)}")

    # If there's already an active timer for user, inform and skip
    if uid in active_timers_map:
        # compute remaining time
        remaining = active_timers_map[uid] - datetime.now(timezone.utc).timestamp()
        if remaining > 0:
            # format
            rem_td = timedelta(seconds=int(remaining))
            hours = rem_td.seconds // 3600 + rem_td.days * 24
            minutes = (rem_td.seconds % 3600) // 60
            seconds = rem_td.seconds % 60
            await message.channel.send(
                f"Bro you still have a timer: {hours}h {minutes}m {seconds}s remaining. I'll let you know when your time's up nibba"
            )
            logger.debug(f"User {uid} still on cooldown ({int(remaining)}s left)")
            return
        else:
            # expired, clear
            active_timers_map.pop(uid, None)
            save_active_timers(active_timers_map)
            task = active_timer_tasks.pop(uid, None)
            if task and not task.done():
                task.cancel()
            logger.debug(f"Cleared expired timer for {uid}")

    # Wait for Toasty messages (content + embeds)
    total_seconds = await wait_for_toasty_numbers(message.channel, timeout=20.0)
    if total_seconds is None:
        # Could not parse or Toasty didn't respond
        await message.channel.send(f"{message.author.mention}, Toasty didn't give me a usable response. Try again or call Jason.")
        logger.warning(f"Failed to get a usable response from Toasty for user {uid}")
        return

    # Confirm to user and schedule timer
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if total_seconds == 3 * 3600:
        # success-catch case
        await message.channel.send(f"Noice, I'll let you know when your timer is up.")
        logger.info(f"User {uid} caught a Pokémon (3h timer)")
    else:
        await message.channel.send(
            f"{message.author.mention}, I will remind you in {hours} hours, {minutes} minutes and {seconds} seconds!"
        )
        logger.info(f"User {uid} has cooldown {total_seconds} seconds ({hours}h {minutes}m {seconds}s)")

    # schedule task
    schedule_timer_for_user(message.author, message.channel, int(total_seconds))

# -------------------------
# Run bot
# -------------------------
if __name__ == "__main__":
    logger.info("Starting bot...")
    try:
        bot.run(TOKEN)
    except Exception:
        logger.exception("Bot crashed on run")
