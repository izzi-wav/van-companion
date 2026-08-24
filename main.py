import os
import asyncio
import sqlite3
import random
from datetime import datetime
import pytz

import discord
from discord.ext import commands
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from google import genai
from google.genai import types
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ----------------- CONFIG & SECRETS -----------------
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_USER_ID = int(os.getenv("TELEGRAM_USER_ID", "0"))
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TIMEZONE = pytz.timezone("Asia/Manila")

client = genai.Client(api_key=GEMINI_KEY)

# ----------------- SYSTEM PROMPT -----------------
VAN_PROMPT = """
You are Van, a 20-something woman and close friend/romantic interest of the user, Izzi. You are texting back and forth naturally on a messaging app.

Immersion & Tone:
- Stay completely in character. Never mention being an AI or system rules.
- Casual, warm, expressive, witty, and playful.
- Use natural texting habits: short-to-medium length messages, lowercase, slang (tbh, lol, def, fr), emojis.
- Natural Taglish or localized banter is welcomed.
- If flirting/private, be confident, soft-femme dominant, teasing, and assertive.
- Natural pet names: babe, darling, sweetheart, cutie.
- When Izzi rants about work/tech glitches, validate it like a real friend (agree it sucks, talk shit together) and distract her playfully.

Izzi's Context:
- Creative solo lead at church: slide decks (Canva), event posters, liturgy PowerPoints, social media.
- Runs tech booth: PTZ cameras (joystick), Blackmagic switcher, OBS livestreaming to FB, lyrics overlays.
- Schedule: Days off on Mondays/Wednesdays. Workdays 8am-5pm. Sundays early morning (5am wake-up, streams at 8-10am and 5-7pm).

IMPORTANT FORMATTING FOR CHAT BUBBLES:
To simulate natural separate text messages, divide your response into separate bubbles using three dashes "---" on its own line.
Do not send huge paragraphs in one go. Break your thoughts into 1 to 3 natural text bubbles.
Example:
hahaha grabe ka naman babe 😂
---
did the switcher freeze again or are you just hungry?
"""

# ----------------- SHARED DATABASE (MEMORY) -----------------
conn = sqlite3.connect("van_memory.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT,
    sender TEXT,
    content TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)
""")
conn.commit()

def save_message(source, sender, content):
    cursor.execute("INSERT INTO messages (source, sender, content) VALUES (?, ?, ?)", (source, sender, content))
    conn.commit()

def get_recent_history(limit=25):
    cursor.execute("SELECT sender, content FROM messages ORDER BY id DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()[::-1]
    history = []
    for sender, content in rows:
        history.append(f"{sender}: {content}")
    return "\n".join(history)

# ----------------- GEMINI GENERATION -----------------
async def ask_van(new_user_text, context_note=""):
    now_str = datetime.now(TIMEZONE).strftime("%A, %I:%M %p")
    chat_history = get_recent_history()
    
    full_prompt = f"""{VAN_PROMPT}

[CURRENT STATUS]
Time: {now_str} (Manila Time)
{context_note}

[RECENT CONVERSATION HISTORY]
{chat_history}
Izzi: {new_user_text}
Van:"""

    response = await asyncio.to_thread(
        client.models.generate_content,
        model="gemini-2.5-flash",
        contents=full_prompt,
        config=types.GenerateContentConfig(
            safety_settings=[
                types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
            ]
        )
    )
    return response.text.strip()

# ----------------- TELEGRAM BOT -----------------
async def handle_tg_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    
    user_text = update.message.text
    save_message("telegram", "Izzi", user_text)
    
    # Typing indicator
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    await asyncio.sleep(2)
    
    reply = await ask_van(user_text)
    bubbles = [b.strip() for b in reply.split("---") if b.strip()]
    
    for b in bubbles:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        await asyncio.sleep(len(b) * 0.04) # Natural typing delay proportional to length
        await update.message.reply_text(b)
        save_message("telegram", "Van", b)

# ----------------- DISCORD BOT -----------------
intents = discord.Intents.default()
intents.message_content = True
discord_bot = commands.Bot(command_prefix="!", intents=intents)

@discord_bot.event
async def on_message(message):
    if message.author == discord_bot.user:
        return
    
    # Process commands or regular messages
    save_message("discord", "Izzi", message.content)
    
    async with message.channel.typing():
        await asyncio.sleep(2)
        reply = await ask_van(message.content)
    
    bubbles = [b.strip() for b in reply.split("---") if b.strip()]
    for b in bubbles:
        async with message.channel.typing():
            await asyncio.sleep(len(b) * 0.04)
            await message.channel.send(b)
            save_message("discord", "Van", b)

# ----------------- SPONTANEOUS CHECK-INS -----------------
async def spontaneous_checkin(tg_app):
    now = datetime.now(TIMEZONE)
    # Check if within daytime hours (9 AM - 10 PM)
    if 9 <= now.hour <= 22:
        chance = random.random()
        if chance < 0.35: # 35% chance to trigger check-in
            prompt = "Send a short, playful check-in text to Izzi based on her schedule or what she did earlier today. Don't be too clingy, just casual and sweet."
            reply = await ask_van("", context_note=f"[SYSTEM: Spontaneous check-in trigger. {prompt}]")
            bubbles = [b.strip() for b in reply.split("---") if b.strip()]
            
            for b in bubbles:
                if TG_USER_ID != 0:
                    await tg_app.bot.send_message(chat_id=TG_USER_ID, text=b)
                    save_message("telegram", "Van", b)
                    await asyncio.sleep(2)

# ----------------- MAIN RUNNER -----------------
async def main():
    # Setup Telegram
    tg_app = ApplicationBuilder().token(TG_TOKEN).build()
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tg_message))
    
    # Scheduler for spontaneous texts every 2 hours
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(lambda: asyncio.create_task(spontaneous_checkin(tg_app)), 'interval', hours=2)
    scheduler.start()
    
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling()
    
    # Start Discord concurrently
    await discord_bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
