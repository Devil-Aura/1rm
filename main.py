import os import re import io import time import shutil import asyncio import logging from datetime import datetime from typing import Dict, Any, Optional, Tuple, List

from pyrogram import Client, filters from pyrogram.types import ( Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ) from pyrogram.errors import FloodWait

======================

Config from ENV (no DB)

======================

API_ID = int(os.getenv("API_ID", "22768311")) API_HASH = os.getenv("API_HASH", "702d8884f48b42e865425391432b3794") BOT_TOKEN = os.getenv("BOT_TOKEN", "")

OWNER_ID = int(os.getenv("OWNER_ID", "6040503076"))

Comma-separated list of admin user IDs (optional)

ADMIN_IDS = set() _admin_env = os.getenv("ADMIN_IDS", "5469101870").strip() if _admin_env: for _v in _admin_env.split(","): _v = _v.strip() if _v.isdigit(): ADMIN_IDS.add(int(_v))

Log channel (forward/copy final renamed media exactly as sent to user)

LOG_CHANNEL = int(os.getenv("LOG_CHANNEL", "-1003058967184"))

Work directory

WORKDIR = os.getenv("WORKDIR", "/tmp/suto_rename") os.makedirs(WORKDIR, exist_ok=True)

FFmpeg path

FFMPEG = shutil.which("ffmpeg") FFPROBE = shutil.which("ffprobe")

======================

Logging

======================

logging.basicConfig( level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", ) logger = logging.getLogger("suto-rename")

======================

In-memory stores (lost on restart)

======================

USER_THUMBS: Dict[int, str] = {}  # user_id -> thumbnail file path

Auto-rename formats

user_id -> list of rule dicts {format, trigger, channels:set[int] or None, thumb_path or None}

AUTO_RULES: Dict[int, List[Dict[str, Any]]] = {}

Interactive states (per user)

SESS: Dict[int, Dict[str, Any]] = {}  # ephemeral flow data

======================

Helpers — Access control

======================

def is_owner_or_admin(user_id: int) -> bool: return (user_id == OWNER_ID) or (user_id in ADMIN_IDS)

def access_only_owner_admin(func): async def wrapper(client: Client, message: Message, *args, **kwargs): if not message.from_user: return if not is_owner_or_admin(message.from_user.id): return await message.reply_text("This bot is private. Access denied.") return await func(client, message, *args, **kwargs) return wrapper

======================

Utils — Progress bar (zip-style)

(Integrated to resemble your Auto-Rename.zip behaviour and look)

======================

PROG_BAR_SIZE = 20

def _humanbytes(size: float) -> str: # classic human bytes if size is None: return "0 B" power = 2**10 n = 0 Dic_powerN = {0: 'B', 1: 'KB', 2: 'MB', 3: 'GB', 4: 'TB'} while size > power and n < 4: size /= power n += 1 return f"{size:.2f} {Dic_powerN[n]}"

def _time_formatter(milliseconds: int) -> str: seconds = int(milliseconds / 1000) minutes, sec = divmod(seconds, 60) hours, minutes = divmod(minutes, 60) days, hours = divmod(hours, 24) result = ((str(days) + "d, ") if days else "") + 
((str(hours) + "h, ") if hours else "") + 
((str(minutes) + "m, ") if minutes else "") + 
((str(sec) + "s") if sec else "") return result or "0s"

async def progress_for_pyrogram(current: int, total: int, ud_type: str, message: Message, start: float): """ Progress callback compatible with Pyrogram's download/upload. Mimics the cadence and layout used in your zip. """ now = time.time() diff = now - start if diff <= 0: diff = 0.001 # Update every ~5s or on completion if int(diff) % 5 == 0 or current == total: percentage = current * 100 / total if total else 0 speed = current / diff elapsed_time = int(diff * 1000) time_to_completion = int(((total - current) / speed) * 1000) if speed > 0 else 0 estimated_total_time = elapsed_time + time_to_completion

elapsed_fmt = _time_formatter(elapsed_time)
    eta_fmt = _time_formatter(estimated_total_time)
    filled = int(PROG_BAR_SIZE * percentage / 100)
    bar = "■" * filled + "□" * (PROG_BAR_SIZE - filled)

    try:
        await message.edit_text(
            f"{ud_type}\n"
            f"`[{bar}] {percentage:.2f}%`\n"
            f"**Done:** {_humanbytes(current)} / {_humanbytes(total)}\n"
            f"**Speed:** {_humanbytes(speed)}/s\n"
            f"**ETA:** {eta_fmt} | **Elapsed:** {elapsed_fmt}"
        )
    except Exception:
        pass

======================

Utils — Metadata (zip-style integration)

We apply minimal but robust metadata using ffmpeg

and preserve streams. Works for videos and (generic) files.

======================

async def add_metadata(input_path: str, output_path: str, user_id: int) -> None: """ Add/refresh container/title metadata using ffmpeg (if present). The goal is to mirror your zip's behaviour: apply best-possible metadata for videos/audios/subtitles. If ffmpeg missing, we fallback to raw copy. """ if not FFMPEG: shutil.copy2(input_path, output_path) return

# We'll set generic metadata fields; users often want title derived from filename
base = os.path.basename(output_path)
title_guess = os.path.splitext(base)[0]

cmd = [
    FFMPEG,
    "-hide_banner", "-loglevel", "error",
    "-y",
    "-i", input_path,
    "-map", "0",
    "-c", "copy",
    "-metadata", f"title={title_guess}",
    "-metadata", f"encoder=SutoRenameBot",
    output_path,
]

proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
_, err = await proc.communicate()
if proc.returncode != 0:
    logger.warning("ffmpeg metadata copy failed, fallback to plain copy: %s", err.decode(errors='ignore'))
    shutil.copy2(input_path, output_path)

======================

Filename utilities

======================

EP_RE = re.compile(r"(?:E|EP|Ep|ep)(\d{1,3})") Q_RE = re.compile(r"(\b(?:480p|720p|1080p|1440p|2160p|2K|4K)\b)", re.I)

def extract_episode_quality(name: str) -> Tuple[Optional[str], Optional[str]]: ep = None q = None m = EP_RE.search(name) if m: ep = m.group(1) mq = Q_RE.search(name) if mq: q = mq.group(1) return ep, q

def apply_format(fmt: str, filename: str) -> str: ep, q = extract_episode_quality(filename) out = fmt out = out.replace("episode", ep or "01") out = out.replace("quality", q or "480p") return out

def safe_filename(name: str) -> str: # prevent forbidden characters name = name.replace("/", "-").replace("\", "-") name = re.sub(r"[\r\n\t]", " ", name).strip() return name

======================

Thumbnail helpers

======================

async def save_user_thumbnail(user_id: int, msg: Message) -> Optional[str]: try: start = time.time() p = await msg.download(file_name=os.path.join(WORKDIR, f"thumb_{user_id}.jpg"), progress=progress_for_pyrogram, progress_args=("Downloading thumbnail…", msg.reply_to_message or msg, start)) USER_THUMBS[user_id] = p return p except Exception as e: logger.error("Thumbnail save failed: %s", e) return None

======================

Bot setup

======================

app = Client("suto_rename_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workdir=WORKDIR)

======================

Commands — Thumbnail management

======================

@app.on_message(filters.command(["addthumbnail", "add_thumb", "addthumbnail@"])) @access_only_owner_admin async def cmd_add_thumbnail(client: Client, message: Message): if not message.reply_to_message or not (message.reply_to_message.photo or message.reply_to_message.document): return await message.reply_text("Reply to a photo to set as thumbnail.") target = message.reply_to_message.photo or message.reply_to_message.document temp = await message.reply_text("Saving thumbnail…") p = await target.download(file_name=os.path.join(WORKDIR, f"thumb_{message.from_user.id}.jpg"), progress=progress_for_pyrogram, progress_args=("Downloading thumbnail…", temp, time.time())) USER_THUMBS[message.from_user.id] = p await temp.edit_text("✅ Thumbnail saved.")

@app.on_message(filters.command(["delthumbnail", "del_thumb"])) @access_only_owner_admin async def cmd_del_thumbnail(client: Client, message: Message): p = USER_THUMBS.pop(message.from_user.id, None) if p and os.path.exists(p): try: os.remove(p) except Exception: pass await message.reply_text("🗑️ Thumbnail deleted.")

@app.on_message(filters.command(["showthumbnail", "show_thumb"])) @access_only_owner_admin async def cmd_show_thumbnail(client: Client, message: Message): p = USER_THUMBS.get(message.from_user.id) if not p or not os.path.exists(p): return await message.reply_text("No thumbnail set.") await message.reply_photo(p, caption="Current thumbnail")

======================

Manual /rename — works for videos & files

======================

@app.on_message(filters.command("rename")) @access_only_owner_admin async def cmd_rename(client: Client, message: Message): if len(message.command) < 2: return await message.reply_text("Usage: Reply a media with /rename New Name", quote=True) if not message.reply_to_message or not (message.reply_to_message.video or message.reply_to_message.document or message.reply_to_message.audio): return await message.reply_text("Reply to a video/file/audio to rename.")

new_name = message.text.split(" ", 1)[1].strip()
new_name = safe_filename(new_name)

media = message.reply_to_message.video or message.reply_to_message.document or message.reply_to_message.audio
suffix = os.path.splitext(media.file_name or new_name)[1]
if suffix and not new_name.lower().endswith(suffix.lower()):
    out_name = f"{new_name}{suffix}"
else:
    out_name = new_name

# Download
status = await message.reply_text("Starting download…")
start = time.time()
dl_path = await message.reply_to_message.download(file_name=os.path.join(WORKDIR, out_name), progress=progress_for_pyrogram, progress_args=("Downloading…", status, start))

# ffmpeg metadata stage => into a temp file, then send
temp_out = os.path.join(WORKDIR, f"meta_{int(time.time())}_{out_name}")
await status.edit_text("Applying metadata…")
await add_metadata(dl_path, temp_out, message.from_user.id)

thumb = USER_THUMBS.get(message.from_user.id)

# Send back (bold filename in caption)
caption = f"**{out_name}**"
await status.edit_text("Uploading…")
up_start = time.time()
sent = None
try:
    if message.reply_to_message.video:
        sent = await message.reply_video(
            video=temp_out,
            caption=caption,
            thumb=thumb if thumb and os.path.exists(thumb) else None,
            progress=progress_for_pyrogram,
            progress_args=("Uploading…", status, up_start),
        )
    else:
        sent = await message.reply_document(
            document=temp_out,
            caption=caption,
            thumb=thumb if thumb and os.path.exists(thumb) else None,
            progress=progress_for_pyrogram,
            progress_args=("Uploading…", status, up_start),
        )
except FloodWait as e:
    await asyncio.sleep(e.value)
finally:
    await status.delete()

# Log copy to LOG_CHANNEL (exact same as user gets)
if LOG_CHANNEL and sent:
    try:
        await sent.copy(LOG_CHANNEL)
    except Exception as e:
        logger.warning("Log copy failed: %s", e)

# Cleanup
for p in (dl_path, temp_out):
    try:
        if p and os.path.exists(p):
            os.remove(p)
    except Exception:
        pass

======================

/autorename flow — guided setup

======================

AUTOKEYS = { "AWAIT_FMT": "await_fmt", "AWAIT_TRIGGER": "await_trigger", "AWAIT_TARGET": "await_target", "AWAIT_THUMB": "await_thumb", }

@app.on_message(filters.command("autorename")) @access_only_owner_admin async def cmd_autorename(client: Client, message: Message): uid = message.from_user.id SESS[uid] = {"state": AUTOKEYS["AWAIT_FMT"], "channels": set()} txt = ( "📝 Send your custom rename format.\n\n" "Example: Naruto Shippuden S02 - EPepisode - quality [Dual Audio] - @CrunchyRollChannel\n\n" "📌 Available Variables:\n• episode - Episode number\n• quality - Video quality\n\n/cancel - Cancel this process" ) await message.reply_text(txt)

@app.on_message(filters.command("cancel")) @access_only_owner_admin async def cmd_cancel(client: Client, message: Message): uid = message.from_user.id SESS.pop(uid, None) await message.reply_text("❌ Process cancelled.")

@app.on_message(filters.command("seeformat")) @access_only_owner_admin async def cmd_seeformat(client: Client, message: Message): uid = message.from_user.id rules = AUTO_RULES.get(uid, []) if not rules: return await message.reply_text("No saved formats yet.") lines = [] for i, r in enumerate(rules, 1): ch = r.get("channels") ch_txt = "Not Set" if not ch else ", ".join(str(x) for x in ch) lines.append( f"{i}.\n" f"📝 Format: {r['format']}\n" f"🔑 Trigger: {r['trigger']}\n" f"📡 Channels: {ch_txt}\n" f"🖼️ Thumbnail: {'Yes' if r.get('thumb_path') else 'No'}" ) await message.reply_text("\n\n".join(lines))

State driver for /autorename

@app.on_message(filters.private & ~filters.command(["autorename", "cancel", "seeformat", "addthumbnail", "delthumbnail", "showthumbnail", "rename"])) @access_only_owner_admin async def autorename_state_driver(client: Client, message: Message): uid = message.from_user.id st = SESS.get(uid) if not st: return  # ignore regular messages

state = st.get("state")
if state == AUTOKEYS["AWAIT_FMT"]:
    fmt = message.text.strip() if message.text else None
    if not fmt:
        return await message.reply_text("Send text format.")
    st["format"] = fmt
    st["state"] = AUTOKEYS["AWAIT_TRIGGER"]
    await message.reply_text(
        "🔑 Send the trigger word that should activate this format.\n\n"
        "Example: naruto, anime, movies\n\n"
        "💡 Note: If no trigger matches, you'll be prompted to rename manually.\n\n"
        "/cancel - Cancel this process"
    )
    return

if state == AUTOKEYS["AWAIT_TRIGGER"]:
    trig = (message.text or "").strip()
    if not trig:
        return await message.reply_text("Send a valid trigger word.")
    st["trigger"] = trig
    st["state"] = AUTOKEYS["AWAIT_TARGET"]
    await message.reply_text(
        "📥 ❪ SET TARGET CHAT ❫\n\n"
        "➡️ Forward a message from your target chat where this format will be applied.\n\n"
        "Available Options:\n"
        "• Forward a message - Add specific channel\n"
        "• /no - Skip adding any channel (apply to all)\n"
        "• /done - Finish adding channels\n"
        "• /cancel - Cancel the process"
    )
    return

if state == AUTOKEYS["AWAIT_TARGET"]:
    txt = (message.text or "").strip().lower() if message.text else ""
    if txt == "/no":
        st["state"] = AUTOKEYS["AWAIT_THUMB"]
        await message.reply_text(
            "🖼️ Forward a photo for your custom thumbnail.\n\n"
            "Options:\n• Forward/Send a photo - Set custom thumbnail\n• /no - Skip adding thumbnail\n• /cancel - Cancel this process"
        )
        return
    if txt == "/done":
        st["state"] = AUTOKEYS["AWAIT_THUMB"]
        await message.reply_text(
            "🖼️ Forward a photo for your custom thumbnail.\n\n"
            "Options:\n• Forward/Send a photo - Set custom thumbnail\n• /no - Skip adding thumbnail\n• /cancel - Cancel this process"
        )
        return
    # Expect a forwarded msg from the target chat (channel/group)
    if message.forward_from_chat:
        st.setdefault("channels", set()).add(message.forward_from_chat.id)
        await message.reply_text("✅ Target added. Send more or /done.")
        return
    return await message.reply_text("Forward from the target chat, or /no, or /done.")

if state == AUTOKEYS["AWAIT_THUMB"]:
    txt = (message.text or "").strip().lower() if message.text else ""
    thumb_path = None
    if txt == "/no":
        thumb_path = None
    elif message.photo or (message.document and str(message.document.mime_type or "").startswith("image/")):
        ph = message.photo or message.document
        temp = await message.reply_text("Saving thumbnail…")
        thumb_path = await ph.download(file_name=os.path.join(WORKDIR, f"rule_thumb_{uid}_{int(time.time())}.jpg"), progress=progress_for_pyrogram, progress_args=("Downloading thumbnail…", temp, time.time()))
        await temp.delete()
    else:
        return await message.reply_text("Send a photo or /no.")

    # Save rule
    rule = {
        "format": st.get("format"),
        "trigger": st.get("trigger"),
        "channels": st.get("channels") if st.get("channels") else None,
        "thumb_path": thumb_path,
    }
    AUTO_RULES.setdefault(uid, []).append(rule)
    SESS.pop(uid, None)

    await message.reply_text(
        "✅ Your format has been saved successfully!\n\n"
        f"📝 Format: {rule['format']}\n"
        f"🔑 Trigger: {rule['trigger']}\n"
        f"📡 Channels: {'Not Set' if not rule['channels'] else ', '.join(str(c) for c in rule['channels'])}\n"
        f"🖼️ Thumbnail: {'Yes' if rule['thumb_path'] else 'No'}\n\n"
        "📌 To view all your saved formats, send /seeformat"
    )
    return

======================

Auto-rename handler — when owner/admin sends a media

If filename contains any trigger word for that user, apply rule.

======================

@app.on_message(filters.private & (filters.video | filters.document | filters.audio)) @access_only_owner_admin async def on_media_private(client: Client, message: Message): uid = message.from_user.id rules = AUTO_RULES.get(uid, []) if not rules: return  # nothing to do

# Determine base filename
media = message.video or message.document or message.audio
base_name = media.file_name or (message.video.file_name if message.video else None) or f"media_{int(time.time())}"

# Find matching rule
chosen = None
for r in rules:
    trig = r.get("trigger", "").lower()
    chans = r.get("channels")  # None or set
    # channel scope: if set, only apply when forwarded from/coming from there
    if chans:
        if not message.forward_from_chat or message.forward_from_chat.id not in chans:
            continue
    if trig and trig in base_name.lower():
        chosen = r
        break

if not chosen:
    return  # No trigger matched; do nothing (per your note you'll be prompted manually via /rename)

# Apply rename format
fmt = chosen["format"]
new_base = apply_format(fmt, base_name)
new_base = safe_filename(new_base)
suffix = os.path.splitext(base_name)[1]
if suffix and not new_base.lower().endswith(suffix.lower()):
    out_name = f"{new_base}{suffix}"
else:
    out_name = new_base

status = await message.reply_text("Starting auto-rename download…")

# Download
start = time.time()
dl_path = await message.download(file_name=os.path.join(WORKDIR, out_name), progress=progress_for_pyrogram, progress_args=("Downloading…", status, start))

# Metadata
await status.edit_text("Applying metadata…")
temp_out = os.path.join(WORKDIR, f"meta_{int(time.time())}_{out_name}")
await add_metadata(dl_path, temp_out, uid)

# Thumbnail priority: rule thumb > user thumb
thumb = chosen.get("thumb_path") or USER_THUMBS.get(uid)

# Upload
caption = f"**{out_name}**"
await status.edit_text("Uploading…")
up_start = time.time()
sent = None
try:
    if message.video:
        sent = await message.reply_video(
            video=temp_out,
            caption=caption,
            thumb=thumb if thumb and os.path.exists(thumb) else None,
            progress=progress_for_pyrogram,
            progress_args=("Uploading…", status, up_start),
        )
    else:
        sent = await message.reply_document(
            document=temp_out,
            caption=caption,
            thumb=thumb if thumb and os.path.exists(thumb) else None,
            progress=progress_for_pyrogram,
            progress_args=("Uploading…", status, up_start),
        )
except FloodWait as e:
    await asyncio.sleep(e.value)
finally:
    await status.delete()

# Log
if LOG_CHANNEL and sent:
    try:
        await sent.copy(LOG_CHANNEL)
    except Exception as e:
        logger.warning("Log copy failed: %s", e)

# Cleanup
for p in (dl_path, temp_out):
    try:
        if p and os.path.exists(p):
            os.remove(p)
    except Exception:
        pass

======================

Start/help

======================

HELP_TEXT = ( "Suto Rename Bot (Private)\n\n" "Commands:\n" "• /addthumbnail – reply with a photo to set\n" "• /delt
