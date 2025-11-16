import os
import re
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
import json

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="$", intents=intents)

# we use a json file to keep track of whoever wants to be tracked down by this bot;
TRACKED_FILE = "tracked_users.json"
TIMERS_FILE = "active_timers.json"

def load_tracked_users():
    try:
        with open(TRACKED_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()

def save_tracked_users():
    with open(TRACKED_FILE, "w") as f:
        json.dump(list(tracked_users), f)

def load_active_timers():
    timers = {}
    try:
        with open(TIMERS_FILE, "r") as f:
            saved = json.load(f)
            for user_id, ts in saved.items():
                end_time = datetime.fromtimestamp(ts, tz=timezone.utc)
                if end_time > datetime.now(timezone.utc):
                    timers[int(user_id)] = end_time
    except FileNotFoundError:
        pass
    return timers

def save_active_timers():
    timers_to_save = {str(user): end.timestamp() for user, end in active_timers.items()}
    with open(TIMERS_FILE, "w") as f:
        json.dump(timers_to_save, f)

# ----------------------------
# Load on startup
# ----------------------------
tracked_users = load_tracked_users()
active_timers = load_active_timers()

# ----------------------------
# Cooldown detection
# ----------------------------
async def wait_for_toasty_numbers(channel, timeout=15.0):
    """
    Wait for the next bot message in the channel containing valid cooldown numbers.
    Returns total_seconds or None if timeout.
    Handles hours, minutes, and seconds.
    """
    def check(msg):
        return msg.channel == channel and msg.author.bot

    while True:
        try:
            msg = await bot.wait_for("message", check=check, timeout=timeout)
        except asyncio.TimeoutError:
            return None

        hours, minutes, seconds = 0, 0, 0

        h_match = re.search(r"(\d+)\s*hours?", msg.content, re.IGNORECASE)
        if h_match:
            hours = int(h_match.group(1))

        m_match = re.search(r"(\d+)\s*minutes?", msg.content, re.IGNORECASE)
        if m_match:
            minutes = int(m_match.group(1))

        s_match = re.search(r"(\d+)\s*seconds?", msg.content, re.IGNORECASE)
        if s_match:
            seconds = int(s_match.group(1))

        total_seconds = hours * 3600 + minutes * 60 + seconds
        if total_seconds > 0:
            return total_seconds

# ----------------------------
# Bot events and commands
# ----------------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is online!")

@bot.command(name="catchPokemon")
async def add_tracked_user(ctx):
    if ctx.author.id in tracked_users:
        await ctx.send(f"{ctx.author.mention}, you are already being tracked for Pokémon cooldowns!")
        return

    tracked_users.add(ctx.author.id)
    save_tracked_users()
    await ctx.send(f"{ctx.author.mention}, I'm onto you now;)")

@bot.event
async def on_message(message):
    if message.author.id == bot.user.id:
        return

    # Only respond to ;pokemon IF user is tracked
    if message.content.strip().lower() == ";pokemon" and message.author.id in tracked_users:
        user_id = message.author.id
        now = datetime.now(timezone.utc)

        # Check existing timer
        if user_id in active_timers:
            end_time = active_timers[user_id]
            remaining = end_time - now
            if remaining.total_seconds() > 0:
                await message.channel.send(
                    "Go play league of legends or fortnite and I will notify you when you can catch a pokemon;)"
                )
                return
            else:
                active_timers.pop(user_id, None)
                save_active_timers()

        # ---- WAIT FOR ALL TOASTY MESSAGES (supports multi-message responses) ----
        toasty_messages = []

        def toast_check(msg):
            return msg.channel == message.channel and msg.author.bot

        # Collect Toasty messages for up to 2 seconds after the first message
        try:
            first_msg = await bot.wait_for("message", check=toast_check, timeout=15.0)
            toasty_messages.append(first_msg)

            # Gather additional bot messages for up to 2 seconds
            start = datetime.now()
            while (datetime.now() - start).total_seconds() < 2:
                try:
                    m = await bot.wait_for("message", check=toast_check, timeout=0.5)
                    toasty_messages.append(m)
                except asyncio.TimeoutError:
                    break

        except asyncio.TimeoutError:
            await message.channel.send(f"{message.author.mention}, Toasty didn't respond... call Jason.")
            return

        # Combine message contents (sometimes catch is not in the first embed)
        combined = "\n".join(m.content for m in toasty_messages)


        # ---- SUCCESSFUL CATCH DETECTION ----
        success_patterns = [
            r"caught",
            r"you caught",
            r"successfully",
            r"caught a",
            r"you have caught",
        ]

        if any(re.search(pat, combined, re.IGNORECASE) for pat in success_patterns):
            # User CAUGHT a Pokémon
            total_seconds = 3 * 3600  # fixed 3 hour timer

            await message.channel.send(
                f"{message.author.mention}, you caught a Pokémon! Timer started — I will mention you in 3 hours!"
            )
        else:
            # Not a catch → Extract cooldown normally
            total_seconds = await wait_for_toasty_numbers(message.channel)
            if not total_seconds:
                await message.channel.send(
                    f"{message.author.mention}, Toasty gave me something weird... call Jason."
                )
                return

            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            seconds = total_seconds % 60

            await message.channel.send(
                f"{message.author.mention}, I will remind you in {hours} hours, {minutes} minutes and {seconds} seconds!"
            )

        # ---- START TIMER ----
        end_time = datetime.now(timezone.utc) + timedelta(seconds=total_seconds)
        active_timers[user_id] = end_time
        save_active_timers()

        async def cooldown_timer(user, channel, total_seconds):
            try:
                await asyncio.sleep(total_seconds)
                await channel.send(
                    f"{user.mention}, your cooldown is over! Catch them all!"
                )
            finally:
                active_timers.pop(user.id, None)
                save_active_timers()

        bot.loop.create_task(cooldown_timer(message.author, message.channel, total_seconds))

    await bot.process_commands(message)

bot.run(TOKEN)
