#!/usr/bin/env python3
# main.py - Suto Rename Bot (single-file)
# Requirements: pyrogram, tgcrypto. FFmpeg optional (for metadata).
# Usage: set API_ID, API_HASH, BOT_TOKEN, OWNER_ID, optional ADMIN_IDS, LOG_CHANNEL

import os
import re
import time
import shutil
import asyncio
import logging
from typing import Dict, Any, Optional, List, Set
from datetime import datetime

from pyrogram import Client, filters
from pyrogram.types import Message

# ---------------------
# Configuration (ENV)
# ---------------------
API_ID = int(os.getenv("API_ID", "22768311"))
API_HASH = os.getenv("API_HASH", "702d8884f48b42e865425391432b3794")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "6040503076"))
ADMIN_IDS_ENV = os.getenv("ADMIN_IDS", "5469101870")
LOG_CHANNEL = int(os.getenv("LOG_CHANNEL", "-1003058967184"))  # 0 means disabled
WORKDIR = os.getenv("WORKDIR", "/tmp/suto_rename")

os.makedirs(WORKDIR, exist_ok=True)

# Parse ADMIN_IDS (comma separated)
ADMIN_IDS: Set[int] = set()
if ADMIN_IDS_ENV:
    for x in ADMIN_IDS_ENV.split(","):
        x = x.strip()
        if x.isdigit():
            ADMIN_IDS.add(int(x))

# ffmpeg & ffprobe paths (if installed)
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

# ---------------------
# Logging
# ---------------------
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("suto_rename")

# ---------------------
# Bot client
# ---------------------
app = Client(
    "suto_rename_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=WORKDIR
)

# ---------------------
# In-memory stores
# ---------------------
# user_id -> thumbnail file path
USER_THUMBS: Dict[int, str] = {}
# user_id -> list of rules: {format, trigger, channels:set[int] or None, thumb_path or None}
AUTO_RULES: Dict[int, List[Dict[str, Any]]] = {}
# session state for /autorename per user
SESS: Dict[int, Dict[str, Any]] = {}

# ---------------------
# Utilities
# ---------------------
def is_owner_or_admin(user_id: int) -> bool:
    return user_id == OWNER_ID or (user_id in ADMIN_IDS)

def owner_admin_only(func):
    async def wrapper(client: Client, message: Message):
        if not message.from_user:
            return
        uid = message.from_user.id
        if not is_owner_or_admin(uid):
            try:
                await message.reply_text("This bot is private. Access denied.")
            except Exception:
                pass
            return
        return await func(client, message)
    return wrapper

def safe_filename(name: str) -> str:
    # replace forbidden characters in filenames
    name = name.replace("/", "-").replace("\\", "-")
    name = re.sub(r"[\r\n\t]+", " ", name).strip()
    # remove multiple spaces
    name = re.sub(r"\s+", " ", name)
    return name

EP_RE = re.compile(r"(?:E|EP|Ep|ep)(\d{1,3})")
Q_RE = re.compile(r"\b(?:480p|720p|1080p|1440p|2160p|2K|4K)\b", re.I)

def extract_episode_quality(name: str):
    ep = None
    q = None
    m = EP_RE.search(name)
    if m:
        ep = m.group(1)
    mq = Q_RE.search(name)
    if mq:
        q = mq.group(0)
    return ep, q

def apply_format(fmt: str, filename: str) -> str:
    ep, q = extract_episode_quality(filename)
    out = fmt
    out = out.replace("episode", ep or "01")
    out = out.replace("quality", q or "480p")
    return out

def human_readable_bytes(size: float) -> str:
    if size is None:
        return "0 B"
    power = 2**10
    n = 0
    units = ["B", "KB", "MB", "GB", "TB"]
    while size > power and n < len(units)-1:
        size /= power
        n += 1
    return f"{size:.2f} {units[n]}"

def time_formatter(milliseconds: int) -> str:
    seconds = int(milliseconds / 1000)
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days: parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    if sec or not parts: parts.append(f"{sec}s")
    return ", ".join(parts)

# ---------------------
# Progress callback (used for downloads & uploads)
# Pyrogram will call progress(current, total, *progress_args)
# We'll pass (label_text, status_message_obj, start_ts)
# ---------------------
PROG_BAR_SIZE = 20

async def progress_for_pyrogram(current, total, label, status_msg: Message, start_ts):
    try:
        now = time.time()
        diff = now - start_ts
        if diff <= 0:
            diff = 0.001
        # update every ~5 seconds or when complete
        if int(diff) % 5 == 0 or current == total:
            percentage = (current * 100 / total) if total else 0
            speed = current / diff
            elapsed_ms = int(diff * 1000)
            eta_ms = int(((total - current) / speed) * 1000) if speed > 0 else 0
            eta_total = elapsed_ms + eta_ms
            elapsed_fmt = time_formatter(elapsed_ms)
            eta_fmt = time_formatter(eta_total)
            filled = int(PROG_BAR_SIZE * percentage / 100)
            bar = "■" * filled + "□" * (PROG_BAR_SIZE - filled)
            msg = (
                f"{label}\n"
                f"`[{bar}] {percentage:.2f}%`\n"
                f"**Done:** {human_readable_bytes(current)} / {human_readable_bytes(total)}\n"
                f"**Speed:** {human_readable_bytes(speed)}/s\n"
                f"**ETA:** {eta_fmt} | **Elapsed:** {elapsed_fmt}"
            )
            try:
                await status_msg.edit_text(msg)
            except Exception:
                pass
    except Exception:
        # swallow any progress errors to avoid breaking operation
        return

# ---------------------
# Metadata application (uses ffmpeg if available)
# Attempts to copy streams and set metadata (title, encoder)
# If ffmpeg missing or fails, fallback to simple copy
# ---------------------
async def add_metadata(input_path: str, output_path: str, title: Optional[str] = None) -> None:
    """
    Add metadata (title, encoder) using ffmpeg by copying streams.
    If ffmpeg not available or fails, perform a plain copy.
    """
    try:
        if not FFMPEG:
            shutil.copy2(input_path, output_path)
            return

        # guess title from output filename if not provided
        if not title:
            title = os.path.splitext(os.path.basename(output_path))[0]

        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error",
            "-y", "-i", input_path,
            "-map", "0",
            "-c", "copy",
            "-metadata", f"title={title}",
            "-metadata", "encoder=SutoRenameBot",
            output_path
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("ffmpeg metadata addition failed: %s", err.decode(errors="ignore"))
            # fallback copy
            shutil.copy2(input_path, output_path)
    except Exception as e:
        logger.exception("add_metadata failed: %s", e)
        try:
            shutil.copy2(input_path, output_path)
        except Exception:
            pass

# ---------------------
# Thumbnail commands
# ---------------------
@app.on_message(filters.command(["addthumbnail", "add_thumb"]) & filters.private)
@owner_admin_only
async def cmd_add_thumbnail(client: Client, message: Message):
    if not message.reply_to_message:
        return await message.reply_text("Reply to a photo to set as thumbnail.")
    reply = message.reply_to_message
    if not (reply.photo or (reply.document and str(reply.document.mime_type or "").startswith("image/"))):
        return await message.reply_text("Reply to a photo (or image file) to set as thumbnail.")
    status = await message.reply_text("Saving thumbnail...")
    try:
        start = time.time()
        fname = os.path.join(WORKDIR, f"thumb_{message.from_user.id}.jpg")
        path = await reply.download(file_name=fname,
                                    progress=progress_for_pyrogram,
                                    progress_args=("Downloading thumbnail…", status, start))
        USER_THUMBS[message.from_user.id] = path
        await status.edit_text("✅ Thumbnail saved.")
    except Exception as e:
        logger.exception("addthumbnail error: %s", e)
        await status.edit_text("❌ Failed to save thumbnail.")
    finally:
        try:
            await asyncio.sleep(0.5)
            await status.delete()
        except Exception:
            pass

@app.on_message(filters.command(["delthumbnail", "del_thumb"]) & filters.private)
@owner_admin_only
async def cmd_del_thumbnail(client: Client, message: Message):
    uid = message.from_user.id
    p = USER_THUMBS.pop(uid, None)
    if p and os.path.exists(p):
        try:
            os.remove(p)
        except Exception:
            pass
    await message.reply_text("🗑️ Thumbnail deleted (if existed).")

@app.on_message(filters.command(["showthumbnail", "show_thumb"]) & filters.private)
@owner_admin_only
async def cmd_show_thumbnail(client: Client, message: Message):
    p = USER_THUMBS.get(message.from_user.id)
    if not p or not os.path.exists(p):
        return await message.reply_text("No thumbnail set.")
    await message.reply_photo(p, caption="Current thumbnail")

# ---------------------
# Manual /rename
# Format: reply to media with `/rename New Name`
# Works for video, document, audio, generic file
# ---------------------
@app.on_message(filters.command("rename") & filters.private)
@owner_admin_only
async def cmd_rename(client: Client, message: Message):
    if len(message.command) < 2 or not message.reply_to_message:
        return await message.reply_text("Usage: reply to a media with `/rename New Name`")
    new_raw = message.text.split(" ", 1)[1].strip()
    if not new_raw:
        return await message.reply_text("Provide a new filename after /rename.")
    new_raw = safe_filename(new_raw)

    reply = message.reply_to_message
    media = reply.video or reply.document or reply.audio
    if not media:
        return await message.reply_text("Reply to a video/file/audio to rename.")

    # determine extension (if media has file_name)
    orig_name = getattr(media, "file_name", None) or f"file_{int(time.time())}"
    ext = os.path.splitext(orig_name)[1]
    if ext and not new_raw.lower().endswith(ext.lower()):
        out_name = f"{new_raw}{ext}"
    else:
        out_name = new_raw

    status = await message.reply_text("Starting download...")
    try:
        start = time.time()
        dl_path = await reply.download(file_name=os.path.join(WORKDIR, f"dl_{int(time.time())}_{out_name}"),
                                       progress=progress_for_pyrogram,
                                       progress_args=("Downloading…", status, start))
    except Exception as e:
        logger.exception("download failed: %s", e)
        return await status.edit_text("❌ Download failed.")

    # metadata application
    temp_out = os.path.join(WORKDIR, f"meta_{int(time.time())}_{out_name}")
    await status.edit_text("Applying metadata...")
    await add_metadata(dl_path, temp_out, title=os.path.splitext(out_name)[0])

    thumb = USER_THUMBS.get(message.from_user.id)
    if thumb and not os.path.exists(thumb):
        thumb = None

    caption = f"**{out_name}**"
    await status.edit_text("Uploading...")
    try:
        up_start = time.time()
        sent = None
        if reply.video:
            sent = await message.reply_video(
                video=temp_out,
                caption=caption,
                thumb=thumb if thumb else None,
                progress=progress_for_pyrogram,
                progress_args=("Uploading…", status, up_start)
            )
        else:
            # document / audio / others
            sent = await message.reply_document(
                document=temp_out,
                caption=caption,
                thumb=thumb if thumb else None,
                progress=progress_for_pyrogram,
                progress_args=("Uploading…", status, up_start)
            )
    except Exception as e:
        logger.exception("upload failed: %s", e)
        return await status.edit_text("❌ Upload failed.")
    finally:
        try:
            await status.delete()
        except Exception:
            pass

    # forward/copy to log channel if set
    if LOG_CHANNEL and sent:
        try:
            await sent.copy(LOG_CHANNEL)
        except Exception:
            logger.exception("failed to copy to log channel")

    # cleanup
    for p in (dl_path, temp_out):
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

# ---------------------
# Autorename interactive flow
# Steps:
# 1) /autorename -> asks for format text
# 2) ask for trigger word(s)
# 3) set target channels (forward messages) or /no
# 4) set thumbnail (forward photo) or /no
# Save in AUTO_RULES[user_id] as dict {format, trigger, channels, thumb_path}
# ---------------------
AWAIT_FMT = "await_format"
AWAIT_TRIGGER = "await_trigger"
AWAIT_TARGET = "await_target"
AWAIT_THUMB = "await_thumb"

@app.on_message(filters.command("autorename") & filters.private)
@owner_admin_only
async def cmd_autorename_start(client: Client, message: Message):
    uid = message.from_user.id
    SESS[uid] = {"state": AWAIT_FMT, "channels": set()}
    txt = (
        "📝 Send your custom rename format.\n\n"
        "Example: Naruto Shippuden S02 - EPepisode - quality [Dual Audio] - @CrunchyRollChannel\n\n"
        "📌 Available Variables:\n• episode - Episode number\n• quality - Video quality\n\n/cancel - Cancel this process"
    )
    await message.reply_text(txt)

@app.on_message(filters.command("cancel") & filters.private)
@owner_admin_only
async def cmd_cancel(client: Client, message: Message):
    uid = message.from_user.id
    if uid in SESS:
        SESS.pop(uid, None)
    await message.reply_text("❌ Process cancelled.")

@app.on_message(filters.command("seeformat") & filters.private)
@owner_admin_only
async def cmd_seeformat(client: Client, message: Message):
    uid = message.from_user.id
    rules = AUTO_RULES.get(uid, [])
    if not rules:
        return await message.reply_text("No saved formats yet.")
    lines = []
    for i, r in enumerate(rules, 1):
        ch = r.get("channels")
        ch_txt = "Not Set" if not ch else ", ".join(str(x) for x in ch)
        lines.append(
            f"{i}.\n📝 Format: {r['format']}\n🔑 Trigger: {r['trigger']}\n📡 Channels: {ch_txt}\n🖼️ Thumbnail: {'Yes' if r.get('thumb_path') else 'No'}"
        )
    await message.reply_text("\n\n".join(lines))

# State driver: handle text forward/photo during autorename flow
@app.on_message(filters.private & ~filters.command(["autorename", "cancel", "seeformat", "addthumbnail", "delthumbnail", "showthumbnail", "rename"]))
@owner_admin_only
async def autorename_state_driver(client: Client, message: Message):
    uid = message.from_user.id
    st = SESS.get(uid)
    if not st:
        return

    state = st.get("state")
    # 1: format
    if state == AWAIT_FMT:
        fmt = (message.text or "").strip()
        if not fmt:
            return await message.reply_text("Send the format text (plain text).")
        st["format"] = fmt
        st["state"] = AWAIT_TRIGGER
        return await message.reply_text(
            "🔑 Send the trigger word that should activate this format.\n\n"
            "Example: naruto, anime, movies\n\n"
            "💡 Note: If no trigger matches, you'll be prompted to rename manually.\n\n"
            "/cancel - Cancel this process"
        )

    # 2: trigger
    if state == AWAIT_TRIGGER:
        trig = (message.text or "").strip()
        if not trig:
            return await message.reply_text("Send a valid trigger word.")
        st["trigger"] = trig
        st["state"] = AWAIT_TARGET
        return await message.reply_text(
            "📥 ❪ SET TARGET CHAT ❫\n\n"
            "➡️ Forward a message from your target chat where this format will be applied.\n\n"
            "Available Options:\n"
            "• Forward a message - Add specific channel\n"
            "• /no - Skip adding any channel (apply to all)\n"
            "• /done - Finish adding channels\n"
            "• /cancel - Cancel the process"
        )

    # 3: target chats
    if state == AWAIT_TARGET:
        txt = (message.text or "").strip().lower()
        if txt == "/no":
            st["state"] = AWAIT_THUMB
            return await message.reply_text(
                "🖼️ Forward a photo for your custom thumbnail.\n\n"
                "Options:\n• Forward/Send a photo - Set custom thumbnail\n• /no - Skip adding thumbnail\n• /cancel - Cancel this process"
            )
        if txt == "/done":
            st["state"] = AWAIT_THUMB
            return await message.reply_text(
                "🖼️ Forward a photo for your custom thumbnail.\n\n"
                "Options:\n• Forward/Send a photo - Set custom thumbnail\n• /no - Skip adding thumbnail\n• /cancel - Cancel this process"
            )
        # forwarded message adds channel
        if message.forward_from_chat:
            st.setdefault("channels", set()).add(message.forward_from_chat.id)
            return await message.reply_text("✅ Target added. Forward more or send /done when finished.")
        return await message.reply_text("Forward from the target chat, or send /no, or send /done.")

    # 4: thumbnail
    if state == AWAIT_THUMB:
        txt = (message.text or "").strip().lower()
        thumb_path = None
        if txt == "/no":
            thumb_path = None
        elif message.photo or (message.document and str(message.document.mime_type or "").startswith("image/")):
            ph = message.photo or message.document
            status = await message.reply_text("Saving thumbnail...")
            try:
                start = time.time()
                fname = os.path.join(WORKDIR, f"rule_thumb_{uid}_{int(time.time())}.jpg")
                thumb_path = await ph.download(file_name=fname,
                                               progress=progress_for_pyrogram,
                                               progress_args=("Downloading thumbnail…", status, start))
                await status.delete()
            except Exception as e:
                logger.exception("rule thumb save failed: %s", e)
                return await status.edit_text("❌ Failed to save thumbnail.")
        else:
            return await message.reply_text("Send a photo or /no to skip thumbnail.")

        # save rule
        rule = {
            "format": st.get("format"),
            "trigger": st.get("trigger"),
            "channels": st.get("channels") if st.get("channels") else None,
            "thumb_path": thumb_path
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

# ---------------------
# Auto-rename handler for private media: when owner/admin sends media,
# check rules and if a trigger matches filename, auto-apply the rule.
# ---------------------
@app.on_message(filters.private & (filters.video | filters.document | filters.audio))
@owner_admin_only
async def on_media_private(client: Client, message: Message):
    uid = message.from_user.id
    rules = AUTO_RULES.get(uid, [])
    if not rules:
        return

    media = message.video or message.document or message.audio
    base_name = getattr(media, "file_name", None) or f"media_{int(time.time())}"
    chosen = None
    for r in rules:
        trig = (r.get("trigger") or "").lower()
        chans = r.get("channels")
        # channel scope: if set, only apply when forwarded from/coming from there
        if chans:
            if not message.forward_from_chat or message.forward_from_chat.id not in chans:
                continue
        if trig and trig in base_name.lower():
            chosen = r
            break

    if not chosen:
        return  # no match, do nothing (use /rename for manual)

    fmt = chosen["format"]
    new_base = apply_format(fmt, base_name)
    new_base = safe_filename(new_base)
    suffix = os.path.splitext(base_name)[1]
    if suffix and not new_base.lower().endswith(suffix.lower()):
        out_name = f"{new_base}{suffix}"
    else:
        out_name = new_base

    status = await message.reply_text("Starting auto-rename download...")
    try:
        start = time.time()
        dl_path = await message.download(file_name=os.path.join(WORKDIR, f"dl_{int(time.time())}_{out_name}"),
                                         progress=progress_for_pyrogram,
                                         progress_args=("Downloading…", status, start))
    except Exception as e:
        logger.exception("auto download failed: %s", e)
        return await status.edit_text("❌ Download failed.")

    temp_out = os.path.join(WORKDIR, f"meta_{int(time.time())}_{out_name}")
    await status.edit_text("Applying metadata...")
    await add_metadata(dl_path, temp_out, title=os.path.splitext(out_name)[0])

    # choose thumb: rule-specific thumb > user thumb
    thumb = chosen.get("thumb_path") or USER_THUMBS.get(uid)
    if thumb and not os.path.exists(thumb):
        thumb = None

    caption = f"**{out_name}**"
    await status.edit_text("Uploading...")
    try:
        up_start = time.time()
        sent = None
        if message.video:
            sent = await message.reply_video(
                video=temp_out,
                caption=caption,
                thumb=thumb if thumb else None,
                progress=progress_for_pyrogram,
                progress_args=("Uploading…", status, up_start)
            )
        else:
            sent = await message.reply_document(
                document=temp_out,
                caption=caption,
                thumb=thumb if thumb else None,
                progress=progress_for_pyrogram,
                progress_args=("Uploading…", status, up_start)
            )
    except Exception as e:
        logger.exception("auto upload failed: %s", e)
        await status.edit_text("❌ Upload failed.")
        sent = None
    finally:
        try:
            await status.delete()
        except Exception:
            pass

    # log copy
    if LOG_CHANNEL and sent:
        try:
            await sent.copy(LOG_CHANNEL)
        except Exception:
            logger.exception("failed to copy to log channel")

    # cleanup
    for p in (dl_path, temp_out):
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

# ---------------------
# Start/help
# ---------------------
HELP_TEXT = (
    "Suto Rename Bot (Private)\n\n"
    "Commands:\n"
    "• /addthumbnail – reply with a photo to set\n"
    "• /delthumbnail – delete saved thumbnail\n"
    "• /showthumbnail – preview your thumbnail\n"
    "• /rename <New Name> – reply to a video/file/audio (applies metadata + thumbnail)\n"
    "• /autorename – guided setup for triggers & formats\n"
    "• /seeformat – list your saved formats\n"
    "• /cancel – cancel current setup\n\n"
    "Notes:\n"
    "• Works for videos, documents and audio.\n"
    "• All outputs are copied to LOG_CHANNEL exactly as delivered (if set).\n"
    "• In-memory only; rules reset on restart."
)

@app.on_message(filters.command(["start", "help"]) & filters.private)
@owner_admin_only
async def cmd_start_help(client: Client, message: Message):
    await message.reply_text(HELP_TEXT)

# ---------------------
# Run
# ---------------------
if __name__ == "__main__":
    if not API_ID or not API_HASH or not BOT_TOKEN or not OWNER_ID:
        logger.error("Missing required environment variables. Exiting.")
        print("Please set API_ID, API_HASH, BOT_TOKEN, OWNER_ID (and optionally LOG_CHANNEL).")
        raise SystemExit(1)
    logger.info("Starting Suto Rename Bot...")
    app.run()
