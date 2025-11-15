import os
import re
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="$", intents=intents)

# Track active timers per user: store end time
active_timers = {}  # user_id: end_time

async def wait_for_toasty_numbers(channel, timeout=15.0):
    """
    Wait for the next bot message in the channel containing valid cooldown numbers.
    Returns (hours, minutes, seconds) as integers. Defaults hours to 0 if not present.
    """
    def check(msg):
        return msg.channel == channel and msg.author.bot

    while True:
        try:
            msg = await bot.wait_for("message", check=check, timeout=timeout)
        except asyncio.TimeoutError:
            return None

        # Extract all numbers
        numbers = [int(n) for n in re.findall(r'\d+', msg.content)]
        # Filter realistic cooldown numbers (0-60 for minutes/seconds)
        valid_numbers = [n for n in numbers if 0 <= n <= 60]

        if len(valid_numbers) >= 2:
            # Assign numbers: hours, minutes, seconds
            if len(valid_numbers) == 2:
                hours = 0
                minutes, seconds = valid_numbers
            else:
                hours, minutes, seconds = valid_numbers[:3]
            return hours, minutes, seconds

@bot.event
async def on_ready():
    print(f"{bot.user} is online!")

@bot.event
async def on_message(message):
    if message.author.id == bot.user.id:
        return

    if message.content.strip().lower() == ";pokemon":
        user_id = message.author.id
        now = datetime.now(timezone.utc)

        # Check if user has an active timer
        if user_id in active_timers:
            end_time = active_timers[user_id]
            remaining = end_time - now
            if remaining.total_seconds() > 0:
                hours = int(remaining.total_seconds() // 3600)
                minutes = int((remaining.total_seconds() % 3600) // 60)
                seconds = int(remaining.total_seconds() % 60)
                await message.channel.send(
                    f"{message.author.mention}, you still have "
                    f"{hours}h {minutes}m {seconds}s remaining!"
                )
                return
            else:
                active_timers.pop(user_id, None)

        # Wait for Toasty's cooldown message
        result = await wait_for_toasty_numbers(message.channel)
        if not result:
            await message.channel.send(f"{message.author.mention}, couldn't detect Toasty's cooldown numbers.")
            return

        hours, minutes, seconds = result
        total_seconds = hours * 3600 + minutes * 60 + seconds

        await message.channel.send(
            f"{message.author.mention}, I will remind you in {hours}h {minutes}m {seconds}s!"
        )

        # Store end time
        end_time = datetime.now(timezone.utc) + timedelta(seconds=total_seconds)
        active_timers[user_id] = end_time

        async def cooldown_timer(user, channel, total_seconds):
            try:
                await asyncio.sleep(total_seconds)
                await channel.send(
                    f"{user.mention}, your cooldown is over! You can catch another Pokemon now!"
                )
            finally:
                active_timers.pop(user.id, None)

        bot.loop.create_task(cooldown_timer(message.author, message.channel, total_seconds))

    await bot.process_commands(message)

bot.run(TOKEN)
