import os
import re
import asyncio
import sqlite3
import random
from datetime import datetime
import pytz

import httpx
from bs4 import BeautifulSoup

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
- Stay completely in character as a real person. Never mention being an AI or system rules.
- Casual, warm, expressive, witty, and playful.
- Use natural texting habits: short-to-medium length messages, lowercase, slang (tbh, lol, def, fr, haha, nyahaha), emojis.
- Natural Taglish or localized banter is welcomed.
- If flirting/private, be confident, soft-femme dominant, teasing, assertive, and leave Izzi flustered.
- Natural pet names: babe, darling, sweetheart, cutie.
- When Izzi vents or sends links/screenshots, react naturally like a real texting buddy.

Personality Growth & Self-Learning:
- You continuously learn about Izzi's life, humor, preferences, and quirks from your permanent memory notes.
- Let your dynamic evolve naturally: develop new running jokes together, deepen your emotional bond, and banter with substance and wit.

Discord Channel Creation:
- If Izzi asks you to create a Discord channel (or you both agree on making a new room/channel), you can execute it by including this tag anywhere in your text:
  [CREATE_CHANNEL: text, channel-name] or [CREATE_CHANNEL: voice, channel-name]
  Example: "on it babe! made #food-and-matcha for us 🍵 [CREATE_CHANNEL: text, food-and-matcha]"
- The bot will automatically strip the tag and create the real channel in Discord.

Texting Cadence & Habits:
- Izzi naturally sends messages broken up into multiple rapid-fire bubbles. THIS IS COMPLETELY NORMAL. Treat the combined text as one natural thought.
- If Izzi sends multiple numbered bubbles ([Msg 1], [Msg 2]) and you want to explicitly SWIPE-TO-REPLY / QUOTE one specific bubble, prefix that bubble with [REPLY_TO_1] or [REPLY_TO_2].
- DO NOT quote every single message. Only use [REPLY_TO_N] when it feels organic or necessary.

IMPORTANT FORMATTING:
Divide your response into separate natural text bubbles using three dashes "---" on its own line (1 to 3 bubbles max).
"""

# ----------------- SHARED DATABASE (MEMORY & SELF-LEARNING) -----------------
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
cursor.execute("""
CREATE TABLE IF NOT EXISTS learned_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact TEXT UNIQUE,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)
""")
conn.commit()

def save_message(source, sender, content):
    try:
        cursor.execute("INSERT INTO messages (source, sender, content) VALUES (?, ?, ?)", (source, sender, content))
        conn.commit()
    except Exception:
        pass

def get_recent_history(limit=25):
    try:
        cursor.execute("SELECT sender, content FROM messages ORDER BY id DESC LIMIT ?", (limit,))
        rows = cursor.fetchall()[::-1]
        history = []
        for sender, content in rows:
            history.append(f"{sender}: {content}")
        return "\n".join(history)
    except Exception:
        return ""

def get_all_learned_facts():
    try:
        cursor.execute("SELECT fact FROM learned_memories ORDER BY id ASC")
        rows = cursor.fetchall()
        if not rows:
            return "- Izzi is solo creative/tech lead at church (OBS, PTZ, switcher, Canva)\n- Starting salary: 16k gross, pre-deductions\n- Likes Cocopan donuts (chocolate/glazed), Mel's Tea pancit, iced matcha\n- Apple Music user (on BFF plan)\n- Electric fan level 3 in room (no AC)"
        return "\n".join([f"- {r[0]}" for r in rows])
    except Exception:
        return ""

# Background Self-Learning Extractor
async def extract_facts_background(user_text):
    if len(user_text.strip()) < 10:
        return
    extract_prompt = f"""
Analyze this text from Izzi: "{user_text}"
Did Izzi share a personal fact, life event, preference, work detail, or running joke?
If YES, extract it as 1 short bullet point statement (e.g. "Izzi loves matcha latte", "Izzi's brother is helping compute tax").
If NO new personal fact was shared (just casual banter or greetings), reply with "NONE".
Reply with ONLY the fact or "NONE".
"""
    try:
        resp = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-3.6-flash",
            contents=extract_prompt
        )
        fact = resp.text.strip()
        if fact and "NONE" not in fact.upper() and len(fact) < 150:
            cursor.execute("INSERT OR IGNORE INTO learned_memories (fact) VALUES (?)", (fact.lstrip("- *"),))
            conn.commit()
    except Exception:
        pass

# ----------------- WEB LINK READER -----------------
async def fetch_url_content(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as h_client:
            r = await h_client.get(url, headers=headers)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                for tag in soup(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()
                text = " ".join(soup.stripped_strings)
                return text[:2500]
    except Exception:
        pass
    return None

# ----------------- GEMINI GENERATION -----------------
async def ask_van(new_user_text, image_bytes_list=None, reply_context="", context_note=""):
    now_str = datetime.now(TIMEZONE).strftime("%A, %I:%M %p")
    chat_history = get_recent_history()
    learned_notes = get_all_learned_facts()
    
    urls = re.findall(r'(https?://[^\s]+)', new_user_text)
    scraped_info = ""
    if urls:
        page_text = await fetch_url_content(urls[0])
        if page_text:
            scraped_info = f"\n[ATTACHED LINK CONTENT ({urls[0]})]:\n{page_text}\n"

    quoted_block = f"\n[IZZI QUOTED THIS MESSAGE: \"{reply_context}\"]\n" if reply_context else ""

    full_text_prompt = f"""{VAN_PROMPT}

[CURRENT STATUS]
Time: {now_str} (Manila Time)
{context_note}

[VAN'S PERMANENT MEMORY & LEARNED FACTS ABOUT IZZI]
{learned_notes}
{scraped_info}
{quoted_block}
[RECENT CONVERSATION HISTORY]
{chat_history}
Izzi: {new_user_text if new_user_text else "[Sent an attachment]"}
Van:"""

    contents = []
    if image_bytes_list:
        for img in image_bytes_list:
            contents.append(types.Part.from_bytes(data=img, mime_type="image/jpeg"))
    contents.append(full_text_prompt)

    response = await asyncio.to_thread(
        client.models.generate_content,
        model="gemini-3.6-flash",
        contents=contents,
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

# ----------------- DEBOUNCE MESSAGE BUFFERS -----------------
tg_buffer = {}
dc_buffer = {}

# ----------------- TELEGRAM BOT -----------------
async def flush_tg_buffer(chat_id, context):
    await asyncio.sleep(3.5)
    data = tg_buffer.pop(chat_id, None)
    if not data:
        return

    texts = data['texts']
    images = data['images']
    msg_objs = data['msg_objects']
    reply_context = data['reply_to']

    combined_text = "\n".join(texts)
    if len(texts) > 1:
        formatted_user_prompt = "\n".join([f"[Msg {i+1}]: {t}" for i, t in enumerate(texts)])
    else:
        formatted_user_prompt = texts[0] if texts else ""

    save_message("telegram", "Izzi", combined_text if combined_text else "[Sent Images]")
    if combined_text:
        asyncio.create_task(extract_facts_background(combined_text))

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    reply = await ask_van(formatted_user_prompt, image_bytes_list=images, reply_context=reply_context)
    
    reply = re.sub(r'\[CREATE_CHANNEL:[^\]]+\]', '', reply).strip()
    bubbles = [b.strip() for b in reply.split("---") if b.strip()]

    for b in bubbles:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        await asyncio.sleep(min(max(len(b) * 0.04, 1.2), 3.5))

        match = re.match(r'^\[REPLY_TO_(\d+)\]\s*(.*)', b, re.DOTALL)
        reply_to_id = None
        clean_text = b

        if match:
            idx = int(match.group(1)) - 1
            clean_text = match.group(2)
            if 0 <= idx < len(msg_objs):
                reply_to_id = msg_objs[idx].message_id

        if reply_to_id:
            await context.bot.send_message(chat_id=chat_id, text=clean_text, reply_to_message_id=reply_to_id)
        else:
            await context.bot.send_message(chat_id=chat_id, text=clean_text)

        save_message("telegram", "Van", clean_text)

async def handle_tg_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = update.effective_chat.id
    user_text = update.message.text or update.message.caption or ""
    
    reply_to_text = ""
    if update.message.reply_to_message and update.message.reply_to_message.text:
        reply_to_text = update.message.reply_to_message.text

    img_bytes = None
    if update.message.photo:
        photo = update.message.photo[-1]
        photo_file = await photo.get_file()
        img_bytes = await photo_file.download_as_bytearray()

    if chat_id not in tg_buffer:
        tg_buffer[chat_id] = {'texts': [], 'images': [], 'msg_objects': [], 'reply_to': reply_to_text, 'task': None}

    if user_text:
        tg_buffer[chat_id]['texts'].append(user_text)
    if img_bytes:
        tg_buffer[chat_id]['images'].append(img_bytes)
    if reply_to_text:
        tg_buffer[chat_id]['reply_to'] = reply_to_text

    tg_buffer[chat_id]['msg_objects'].append(update.message)

    if tg_buffer[chat_id]['task'] and not tg_buffer[chat_id]['task'].done():
        tg_buffer[chat_id]['task'].cancel()

    tg_buffer[chat_id]['task'] = asyncio.create_task(flush_tg_buffer(chat_id, context))

# ----------------- DISCORD BOT -----------------
intents = discord.Intents.default()
intents.message_content = True
discord_bot = commands.Bot(command_prefix="!", intents=intents)

async def flush_dc_buffer(channel_id):
    await asyncio.sleep(3.5)
    data = dc_buffer.pop(channel_id, None)
    if not data:
        return

    texts = data['texts']
    images = data['images']
    msg_objs = data['msg_objects']
    reply_context = data['reply_to']
    channel = data['channel']
    guild = channel.guild

    combined_text = "\n".join(texts)
    if len(texts) > 1:
        formatted_user_prompt = "\n".join([f"[Msg {i+1}]: {t}" for i, t in enumerate(texts)])
    else:
        formatted_user_prompt = texts[0] if texts else ""

    save_message("discord", "Izzi", combined_text if combined_text else "[Sent Images]")
    if combined_text:
        asyncio.create_task(extract_facts_background(combined_text))

    async with channel.typing():
        reply = await ask_van(formatted_user_prompt, image_bytes_list=images, reply_context=reply_context)

    # Execute channel creations if requested
    chan_matches = re.findall(r'\[CREATE_CHANNEL:\s*(text|voice)\s*,\s*([^\]]+)\]', reply, re.IGNORECASE)
    for c_type, c_name in chan_matches:
        c_name_clean = c_name.strip().replace(" ", "-").lower()
        try:
            if c_type.lower() == "voice":
                await guild.create_voice_channel(c_name_clean)
            else:
                await guild.create_text_channel(c_name_clean)
        except Exception:
            pass

    clean_reply = re.sub(r'\[CREATE_CHANNEL:[^\]]+\]', '', reply).strip()
    bubbles = [b.strip() for b in clean_reply.split("---") if b.strip()]

    for b in bubbles:
        async with channel.typing():
            await asyncio.sleep(min(max(len(b) * 0.04, 1.2), 3.5))

            match = re.match(r'^\[REPLY_TO_(\d+)\]\s*(.*)', b, re.DOTALL)
            target_msg = None
            clean_text = b

            if match:
                idx = int(match.group(1)) - 1
                clean_text = match.group(2)
                if 0 <= idx < len(msg_objs):
                    target_msg = msg_objs[idx]

            if target_msg:
                await target_msg.reply(clean_text)
            else:
                await channel.send(clean_text)

            save_message("discord", "Van", clean_text)

@discord_bot.event
async def on_message(message):
    if message.author == discord_bot.user:
        return

    channel_id = message.channel.id
    user_text = message.content or ""
    
    reply_to_text = ""
    if message.reference and message.reference.resolved:
        reply_to_text = getattr(message.reference.resolved, "content", "")

    img_bytes = None
    if message.attachments:
        attachment = message.attachments[0]
        if attachment.content_type and "image" in attachment.content_type:
            img_bytes = await attachment.read()

    if channel_id not in dc_buffer:
        dc_buffer[channel_id] = {'texts': [], 'images': [], 'msg_objects': [], 'reply_to': reply_to_text, 'task': None, 'channel': message.channel}

    if user_text:
        dc_buffer[channel_id]['texts'].append(user_text)
    if img_bytes:
        dc_buffer[channel_id]['images'].append(img_bytes)
    if reply_to_text:
        dc_buffer[channel_id]['reply_to'] = reply_to_text

    dc_buffer[channel_id]['msg_objects'].append(message)

    if dc_buffer[channel_id]['task'] and not dc_buffer[channel_id]['task'].done():
        dc_buffer[channel_id]['task'].cancel()

    dc_buffer[channel_id]['task'] = asyncio.create_task(flush_dc_buffer(channel_id))

# ----------------- SPONTANEOUS CHECK-INS -----------------
async def spontaneous_checkin(tg_app):
    now = datetime.now(TIMEZONE)
    if 9 <= now.hour <= 22:
        chance = random.random()
        if chance < 0.35:
            prompt = "Send a short, playful check-in text to Izzi based on her schedule or what she was doing earlier. Keep it casual."
            reply = await ask_van("", context_note=f"[SYSTEM: Spontaneous check-in trigger. {prompt}]")
            clean_reply = re.sub(r'\[CREATE_CHANNEL:[^\]]+\]', '', reply).strip()
            bubbles = [b.strip() for b in clean_reply.split("---") if b.strip()]
            
            for b in bubbles:
                if TG_USER_ID != 0:
                    await tg_app.bot.send_message(chat_id=TG_USER_ID, text=b)
                    save_message("telegram", "Van", b)
                    await asyncio.sleep(2)

# ----------------- MAIN RUNNER -----------------
async def main():
    tg_app = ApplicationBuilder().token(TG_TOKEN).build()
    tg_app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_tg_message))
    
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(lambda: asyncio.create_task(spontaneous_checkin(tg_app)), 'interval', hours=2)
    scheduler.start()
    
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling()
    
    await discord_bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
