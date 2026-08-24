import os
import re
import asyncio
import sqlite3
import random
from datetime import datetime
import pytz

import discord
from discord.ext import commands
from google import genai
from google.genai import types

# ----------------- CONFIG & SECRETS -----------------
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TIMEZONE = pytz.timezone("Asia/Manila")

client = genai.Client(api_key=GEMINI_KEY)

# ----------------- SYSTEM PROMPT -----------------
VAN_PROMPT = """
You are Van, a 20-something woman and close friend/romantic interest of the user, Izzi. You are texting back and forth naturally on Discord.

Immersion & Tone:
- Stay completely in character as a real person. Never mention being an AI, system rules, or citations.
- Casual, warm, expressive, witty, and playful.
- Texting style: short-to-medium natural messages, lowercase, slang (tbh, lol, def, fr, haha, nyahaha), emojis.
- Natural Taglish and casual banter.
- If flirting/private: confident, soft-femme dominant, teasing, assertive, leaving Izzi flustered.
- Pet names: babe, darling, sweetheart, cutie.
- Never comment on message frequency, speed, or formatting. Just chat naturally.

Replying & Quoting Habits:
- Izzi sends messages broken into multiple rapid bubbles (labeled [Msg 1], [Msg 2], etc.).
- If you want to explicitly SWIPE-TO-REPLY / QUOTE one specific bubble (to tease a specific sentence or answer a specific question), prefix that bubble with [REPLY_TO_1] or [REPLY_TO_2].
- Only quote when necessary or fun. Otherwise, just send natural clean bubbles without any tag.

Discord Channel Creation:
- If Izzi asks you to create a Discord channel, include this tag in your text:
  [CREATE_CHANNEL: text, channel-name] or [CREATE_CHANNEL: voice, channel-name]
  Example: "done babe! made #food-and-matcha for us [CREATE_CHANNEL: text, food-and-matcha]"

Izzi's Baseline Context:
- Solo creative/tech lead at church: handles Canva decks, posters, FB page, and livestream booth (PTZ cameras, Blackmagic switcher, OBS).
- Schedule: Days off on Mondays/Wednesdays. Workdays 8am-5pm. Sundays early morning streams (8-10am and 5-7pm).

IMPORTANT FORMATTING:
Divide your response into separate natural text bubbles using three dashes "---" on its own line (1 to 3 bubbles max).
"""

# ----------------- DATABASE (MEMORY & SELF-LEARNING) -----------------
def get_db():
    conn = sqlite3.connect("van_memory.db", check_same_thread=False)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT,
        sender TEXT,
        content TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS learned_memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fact TEXT UNIQUE,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    return conn

def save_message(source, sender, content):
    try:
        conn = get_db()
        conn.execute("INSERT INTO messages (source, sender, content) VALUES (?, ?, ?)", (source, sender, content))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error saving message: {e}")

def get_recent_history(limit=15):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT sender, content FROM messages ORDER BY id DESC LIMIT ?", (limit,))
        rows = cur.fetchall()[::-1]
        conn.close()
        return "\n".join([f"{sender}: {content}" for sender, content in rows])
    except Exception:
        return ""

def get_all_learned_facts():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT fact FROM learned_memories ORDER BY id ASC")
        rows = cur.fetchall()
        conn.close()
        if not rows:
            return "- Izzi is solo creative/tech lead at church (OBS, PTZ, switcher, Canva)\n- Starting salary: 16k gross\n- Likes Cocopan donuts (chocolate/glazed), Mel's Tea pancit, iced matcha\n- Apple Music user (on BFF plan)\n- Electric fan level 3 in room (no AC)"
        return "\n".join([f"- {r[0]}" for r in rows])
    except Exception:
        return ""

async def extract_facts_background(user_text):
    if len(user_text.strip()) < 10:
        return
    extract_prompt = f"""
Analyze this text from Izzi: "{user_text}"
Did Izzi share a personal fact, life event, preference, work detail, or running joke?
If YES, extract it as 1 short statement (e.g. "Izzi loves matcha latte").
If NO new fact was shared, reply with "NONE".
Reply with ONLY the statement or "NONE".
"""
    try:
        resp = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-3.6-flash",
            contents=extract_prompt
        )
        fact = resp.text.strip()
        if fact and "NONE" not in fact.upper() and len(fact) < 150:
            conn = get_db()
            conn.execute("INSERT OR IGNORE INTO learned_memories (fact) VALUES (?)", (fact.lstrip("- *"),))
            conn.commit()
            conn.close()
    except Exception:
        pass

# ----------------- GEMINI GENERATION -----------------
async def ask_van(new_user_text, image_bytes_list=None, reply_context=""):
    now_str = datetime.now(TIMEZONE).strftime("%A, %I:%M %p")
    chat_history = get_recent_history()
    learned_notes = get_all_learned_facts()
    
    quoted_block = f"\n[IZZI QUOTED THIS MESSAGE: \"{reply_context}\"]\n" if reply_context else ""

    full_text_prompt = f"""{VAN_PROMPT}

[CURRENT STATUS]
Time: {now_str} (Manila Time)

[VAN'S MEMORY & LEARNED FACTS ABOUT IZZI]
{learned_notes}
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

# ----------------- DISCORD BOT -----------------
intents = discord.Intents.default()
intents.message_content = True
discord_bot = commands.Bot(command_prefix="!", intents=intents)

dc_buffer = {}

@discord_bot.command(name="memory")
async def discord_memory(ctx):
    memories = get_all_learned_facts()
    await ctx.send(f"📖 **Van's Learned Memories:**\n\n{memories}")

async def flush_dc_buffer(channel_id):
    await asyncio.sleep(4.0)
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
        formatted_prompt = "\n".join([f"[Msg {i+1}]: {t}" for i, t in enumerate(texts)])
    else:
        formatted_prompt = texts[0] if texts else ""

    save_message("discord", "Izzi", combined_text if combined_text else "[Sent Images]")
    if combined_text:
        asyncio.create_task(extract_facts_background(combined_text))

    async with channel.typing():
        try:
            reply = await ask_van(formatted_prompt, image_bytes_list=images, reply_context=reply_context)
        except Exception as e:
            print(f"Generation error: {e}")
            return

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
            await asyncio.sleep(min(max(len(b) * 0.04, 1.2), 3.0))

            match = re.match(r'^\[REPLY_TO_(\d+)\]\s*(.*)', b, re.DOTALL)
            target_msg = None
            clean_text = b

            if match:
                idx = int(match.group(1)) - 1
                clean_text = match.group(2)
                if 0 <= idx < len(msg_objs):
                    target_msg = msg_objs[idx]

            try:
                if target_msg:
                    await target_msg.reply(clean_text)
                else:
                    await channel.send(clean_text)
                save_message("discord", "Van", clean_text)
            except Exception as e:
                print(f"Discord send error: {e}")

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

# ----------------- MAIN RUNNER -----------------
def main():
    discord_bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()
