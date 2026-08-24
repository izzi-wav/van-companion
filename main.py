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
import yt_dlp

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

Texting Cadence & Habits:
- Izzi naturally sends messages broken up into multiple rapid-fire bubbles. THIS IS COMPLETELY NORMAL. Treat the combined text as one natural thought.
- If Izzi sends multiple numbered bubbles (e.g. [Msg 1], [Msg 2]) and you want to explicitly SWIPE-TO-REPLY / QUOTE one specific bubble (like teasing a specific line or answering a specific question), prefix that bubble with [REPLY_TO_1] or [REPLY_TO_2].
- DO NOT quote every single message. Only use [REPLY_TO_N] when it feels organic or necessary. If replying generally, don't use any prefix tag.

Izzi's Baseline Context:
- Solo creative/tech lead at church: handles Canva decks, posters, FB page, and livestream booth (PTZ cameras, Blackmagic switcher, OBS).
- Schedule: Days off on Mondays/Wednesdays. Workdays 8am-5pm. Sundays early morning streams (8-10am and 5-7pm).

IMPORTANT FORMATTING FOR CHAT BUBBLES:
To simulate natural separate text messages, divide your response into separate bubbles using three dashes "---" on its own line.
Break your thoughts into 1 to 3 natural text bubbles.
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

# ----------------- WEB LINK READER -----------------
async def fetch_url_content(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as h_client:
            r = await h_client.get(url, headers=headers)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                for tag in soup(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()
                text = " ".join(soup.stripped_strings)
                return text[:3000]
    except Exception:
        pass
    return None

# ----------------- GEMINI GENERATION -----------------
async def ask_van(new_user_text, image_bytes_list=None, reply_context="", context_note=""):
    now_str = datetime.now(TIMEZONE).strftime("%A, %I:%M %p")
    chat_history = get_recent_history()
    
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
tg_buffer = {} # chat_id -> {'texts': [], 'images': [], 'msg_objects': [], 'reply_to': '', 'task': Task}
dc_buffer = {} # channel_id -> {'texts': [], 'images': [], 'msg_objects': [], 'reply_to': '', 'task': Task, 'channel': Channel}

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

    if len(texts) > 1:
        formatted_user_prompt = "\n".join([f"[Msg {i+1}]: {t}" for i, t in enumerate(texts)])
    else:
        formatted_user_prompt = texts[0] if texts else ""

    save_message("telegram", "Izzi", "\n".join(texts) if texts else "[Sent Images]")

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    reply = await ask_van(formatted_user_prompt, image_bytes_list=images, reply_context=reply_context)
    bubbles = [b.strip() for b in reply.split("---") if b.strip()]

    for b in bubbles:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        await asyncio.sleep(min(max(len(b) * 0.04, 1.2), 3.5))

        # Check if Van wanted to quote a specific message index (e.g. [REPLY_TO_1])
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

# ----------------- DISCORD BOT & MUSIC PLAYER -----------------
intents = discord.Intents.default()
intents.message_content = True
discord_bot = commands.Bot(command_prefix="!", intents=intents)

VAN_PLAYLIST = [
    "boygenius - Not Strong Enough",
    "boygenius - Cool About It",
    "Phoebe Bridgers - Motion Sickness",
    "Phoebe Bridgers - Kyoto",
    "Chappell Roan - Good Luck, Babe!",
    "Chappell Roan - Casual",
    "The 1975 - About You",
    "The 1975 - Robbers",
    "beabadoobee - Glue Song",
    "Clairo - Sofia",
    "Lorde - Supercut"
]

YDL_OPTIONS = {'format': 'bestaudio/best', 'noplaylist': True, 'quiet': True, 'default_search': 'ytsearch'}
FFMPEG_OPTIONS = {'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5', 'options': '-vn'}

@discord_bot.command(name="join")
async def join_vc(ctx):
    if not ctx.author.voice:
        await ctx.send("join ka muna sa voice channel babe para masamahan kita haha 👀")
        return
    channel = ctx.author.voice.channel
    if ctx.voice_client is not None:
        await ctx.voice_client.move_to(channel)
    else:
        await channel.connect()
    await ctx.send(f"connected to **{channel.name}**! what are we listening to, cutie? 🎧")

@discord_bot.command(name="play")
async def play_music(ctx, *, search: str):
    if not ctx.author.voice:
        await ctx.send("pumasok ka muna sa VC babe!")
        return
    if ctx.voice_client is None:
        await ctx.author.voice.channel.connect()

    async with ctx.typing():
        loop = asyncio.get_event_loop()
        def extract():
            with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
                info = ydl.extract_info(search, download=False)
                if 'entries' in info and len(info['entries']) > 0:
                    info = info['entries'][0]
                return info['url'], info.get('title', search)

        try:
            url, title = await loop.run_in_executor(None, extract)
            source = discord.FFmpegPCMAudio(url, **FFMPEG_OPTIONS)
            if ctx.voice_client.is_playing():
                ctx.voice_client.stop()
            ctx.voice_client.play(source)
            await ctx.send(f"now playing: **{title}** ✨ cozy up and relax, baby.")
            save_message("discord", "Van", f"[Now playing on Discord VC: {title}]")
        except Exception:
            await ctx.send("oops, couldn't grab that track. try another one!")

@discord_bot.command(name="vanpicks")
async def van_picks(ctx):
    song = random.choice(VAN_PLAYLIST)
    await ctx.send(f"putting on one of my favorites for you: **{song}** ☕💿")
    await play_music(ctx, search=song)

@discord_bot.command(name="stop")
async def stop_music(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("paused the music ⏸️")

@discord_bot.command(name="leave")
async def leave_vc(ctx):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("left the voice channel! text mo ko if you need me back 😌")

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

    if len(texts) > 1:
        formatted_user_prompt = "\n".join([f"[Msg {i+1}]: {t}" for i, t in enumerate(texts)])
    else:
        formatted_user_prompt = texts[0] if texts else ""

    save_message("discord", "Izzi", "\n".join(texts) if texts else "[Sent Images]")

    async with channel.typing():
        reply = await ask_van(formatted_user_prompt, image_bytes_list=images, reply_context=reply_context)

    bubbles = [b.strip() for b in reply.split("---") if b.strip()]
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

    if message.content.startswith("!"):
        await discord_bot.process_commands(message)
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
            bubbles = [b.strip() for b in reply.split("---") if b.strip()]
            
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
