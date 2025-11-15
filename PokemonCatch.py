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

# ----------------------------
# Persistent storage functions
# ----------------------------
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
    await ctx.send(f"{ctx.author.mention}, you are now being tracked for Pokémon cooldowns!")

@bot.event
async def on_message(message):
    if message.author.id == bot.user.id:
        return

    if message.content.strip().lower() == ";pokemon" and message.author.id in tracked_users:
        user_id = message.author.id

        now = datetime.now(timezone.utc)  # was just datetime.now()
        if user_id in active_timers:
            end_time = active_timers[user_id]
            remaining = end_time - now
            if remaining.total_seconds() > 0:
                hours = int(remaining.total_seconds() // 3600)
                minutes = int((remaining.total_seconds() % 3600) // 60)
                seconds = int(remaining.total_seconds() % 60)
                await message.channel.send(
                    f"{message.author.mention}, you still have {hours} hours, {minutes} minutes and {seconds} seconds remaining!"
                )
                return
            else:
                active_timers.pop(user_id, None)
                save_active_timers()

        total_seconds = await wait_for_toasty_numbers(message.channel)
        if not total_seconds:
            await message.channel.send(f"{message.author.mention}, couldn't detect Toasty's cooldown numbers.")
            return

        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60

        await message.channel.send(
            f"{message.author.mention}, I will remind you in {hours} hours, {minutes} minutes and {seconds} seconds!"
        )

        end_time = datetime.now(timezone.utc) + timedelta(seconds=total_seconds)
        active_timers[user_id] = end_time
        save_active_timers()

        async def cooldown_timer(user, channel, total_seconds):
            try:
                await asyncio.sleep(total_seconds)
                await channel.send(
                    f"{user.mention}, your cooldown is over! You can catch another Pokémon now!"
                )
            finally:
                active_timers.pop(user.id, None)
                save_active_timers()

        bot.loop.create_task(cooldown_timer(message.author, message.channel, total_seconds))

    await bot.process_commands(message)

bot.run(TOKEN)
