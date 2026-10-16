import os
import json
import time
import asyncio
import logging
import mimetypes
from pathlib import Path

import requests
from telethon import TelegramClient, events

TG_API_ID = 38811018
TG_API_HASH = "295c8b2cd315ae5633c3888f55bc51a5"
BOT_TOKEN = "8385215044:AAFUyqX5wK_x8D-uZfRKCqzju63dBDd-uEw"
AICREDITS_API_KEY = "sk-live-9b76bdf9c305795fb0c7f648749f3f84c33ef2f5925a08ee4e5c0349180849d8"

AI_MODEL = "openai/gpt-4o-mini"
AI_API_URL = "https://api.aicredits.in/v1/chat/completions"

BASE = Path.home() / ".gallery_ai"
BASE.mkdir(parents=True, exist_ok=True)
BASE.chmod(0o700)

SESSION = str(BASE / "telethon")
STATE_FILE = BASE / "state.json"
STORAGE = Path.home() / "storage" / "shared"

MAX_HISTORY = 12
MAX_BATCH = 100
MAX_FILE_BYTES = 49 * 1024 * 1024
UPLOAD_DELAY = 0

BOT_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("assistant")

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".heic",
    ".heif", ".bmp", ".gif", ".tif", ".tiff"
}

VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".webm", ".3gp", ".avi"
}

FOLDERS = {
    "camera": ["DCIM/Camera"],
    "snapchat": [
        "Pictures/Snapchat",
        "DCIM/Snapchat",
        "Android/media/com.snapchat.android",
    ],
    "screenshots": [
        "Pictures/Screenshots",
        "DCIM/Screenshots",
    ],
    "downloads": ["Download"],
    "videos": ["DCIM", "Movies"],
    "all": [
        "DCIM", "Pictures", "Movies", "Download",
        "Android/media/com.snapchat.android",
    ],
}

if (
    TG_API_ID == 12345678
    or "YOUR_" in TG_API_HASH
    or "YOUR_" in BOT_TOKEN
    or "YOUR_" in AICREDITS_API_KEY
):
    raise SystemExit("Configure your credentials at the top of the script.")

if not isinstance(TG_API_ID, int):
    raise SystemExit("TG_API_ID must be an integer.")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


state = load_state()
state.setdefault("ai_enabled", False)
state.setdefault("history", {})
state.setdefault("offsets", {})
state.setdefault("last_reply", {})
state.setdefault("last_transfer", None)


def save_state():
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state))
    temp.chmod(0o600)
    temp.replace(STATE_FILE)


def bot_request(method, data=None, files=None, timeout=60):
    response = requests.post(
        f"{BOT_URL}/{method}",
        data=data,
        files=files,
        timeout=timeout,
    )
    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            result.get("description", "Telegram API error")
        )

    return result.get("result")


async def bot_send(chat_id, message):
    try:
        await asyncio.to_thread(
            bot_request,
            "sendMessage",
            {"chat_id": chat_id, "text": str(message)[:4000]},
        )
    except Exception:
        log.exception("Message delivery failed")


def media_files(category):
    if category not in FOLDERS:
        return []

    found = set()

    valid_extensions = (
        IMAGE_EXTS | VIDEO_EXTS
        if category == "all"
        else VIDEO_EXTS
        if category == "videos"
        else IMAGE_EXTS
    )

    for relative in FOLDERS[category]:
        root = STORAGE / relative

        if not root.exists():
            continue

        for base, dirs, filenames in os.walk(root):
            dirs[:] = [
                directory for directory in dirs
                if not directory.startswith(".")
            ]

            for filename in filenames:
                if filename.startswith("."):
                    continue

                path = Path(base) / filename

                if path.suffix.lower() not in valid_extensions:
                    continue

                try:
                    if path.is_file():
                        found.add(str(path))
                except OSError:
                    continue

    return sorted(found, key=str.lower)


def folder_summary():
    if not STORAGE.exists():
        return "Storage unavailable. Run termux-setup-storage."

    lines = ["Available folders:", ""]

    for category in FOLDERS:
        try:
            lines.append(
                f"{category}: {len(media_files(category))} files"
            )
        except Exception:
            lines.append(f"{category}: unable to scan")

    return "\n".join(lines)


owner_id = None
transfer_task = None
transfer_status = "Idle"
pause_event = asyncio.Event()
pause_event.set()


async def upload_file(chat_id, filepath):
    path = Path(filepath)

    try:
        size = path.stat().st_size

        if size > MAX_FILE_BYTES:
            await bot_send(
                chat_id,
                f"Skipped oversized file: {path.name}"
            )
            return True

        def upload():
            with path.open("rb") as handle:
                return bot_request(
                    "sendDocument",
                    {"chat_id": chat_id},
                    files={
                        "document": (
                            path.name,
                            handle,
                            mimetypes.guess_type(path.name)[0]
                            or "application/octet-stream",
                        )
                    },
                    timeout=120,
                )

        for attempt in range(3):
            try:
                await asyncio.to_thread(upload)
                return True
            except Exception:
                log.exception("Upload attempt %s failed", attempt + 1)

                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))

        return False

    except OSError:
        log.exception("File access failed")
        return False


async def run_batch(chat_id, category, count):
    global transfer_status

    try:
        files = await asyncio.to_thread(media_files, category)
        offset = int(state["offsets"].get(category, 0))

        if offset >= len(files):
            await bot_send(chat_id, f"No more {category} files.")
            return

        end = min(offset + count, len(files))
        transfer_status = f"{category}: {offset + 1}-{end}"

        await bot_send(
            chat_id,
            f"Starting {category}: {offset + 1}-{end} "
            f"of {len(files)} files."
        )

        sent = 0

        for index in range(offset, end):
            await pause_event.wait()

            success = await upload_file(chat_id, files[index])

            if not success:
                state["last_transfer"] = {
                    "category": category,
                    "count": count,
                    "chat_id": chat_id,
                }
                save_state()

                await bot_send(
                    chat_id,
                    f"Upload failed at item {index + 1}. "
                    "Progress saved. Use /resume to retry."
                )
                return

            state["offsets"][category] = index + 1
            save_state()
            sent += 1

            await asyncio.sleep(UPLOAD_DELAY)

        state["last_transfer"] = {
            "category": category,
            "count": count,
            "chat_id": chat_id,
        }
        save_state()

        await bot_send(
            chat_id,
            f"Batch complete. {sent} files sent.\n"
            f"Category: {category}\n"
            f"Next item: {state['offsets'].get(category, 0) + 1}"
        )

    except Exception:
        log.exception("Transfer batch failed")
        await bot_send(chat_id, "Batch failed. Check the Termux logs.")

    finally:
        transfer_status = "Idle"


SYSTEM_PROMPT = (
    "You write natural, concise Telegram replies. Match the language "
    "and tone of the conversation. Usually use one or two short "
    "sentences. Do not invent personal facts or claim to be the account "
    "owner in ways that could mislead someone. If you lack context, "
    "ask a brief question."
)


def call_ai(history):
    response = requests.post(
        AI_API_URL,
        headers={
            "Authorization": f"Bearer {AICREDITS_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": AI_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                *history,
            ],
            "temperature": 0.8,
            "max_tokens": 160,
        },
        timeout=45,
    )

    response.raise_for_status()
    result = response.json()

    if not result.get("choices"):
        raise RuntimeError(
            f"AI provider returned no choices: {json.dumps(result)[:1000]}"
        )

    content = result["choices"][0].get("message", {}).get("content")

    if not isinstance(content, str):
        raise RuntimeError(
            f"AI provider returned an unexpected response: "
            f"{json.dumps(result)[:1000]}"
        )

    return content.strip()


client = TelegramClient(SESSION, TG_API_ID, TG_API_HASH)


@client.on(events.NewMessage(outgoing=True))
async def outgoing_ai_commands(event):
    if not event.is_private or event.chat_id != owner_id:
        return

    command = (event.raw_text or "").strip().lower()

    if command == ".ai":
        state["ai_enabled"] = True
        save_state()
        await event.respond("AI auto-replies enabled.")

    elif command == ".ai off":
        state["ai_enabled"] = False
        save_state()
        await event.respond("AI auto-replies disabled.")

    elif command == ".ai reset":
        state["history"] = {}
        state["last_reply"] = {}
        save_state()
        await event.respond("AI conversation histories cleared.")


@client.on(events.NewMessage(incoming=True))
async def incoming_ai_messages(event):
    if not state.get("ai_enabled"):
        return

    if not event.is_private or event.sender_id == owner_id:
        return

    message = (event.raw_text or "").strip()

    if not message:
        return

    try:
        sender = await event.get_sender()

        if getattr(sender, "bot", False):
            return

        key = str(event.sender_id)
        now = time.time()

        last = float(state["last_reply"].get(key, 0))

        if now - last < 2:
            return

        history = state["history"].get(key, [])
        history.append({
            "role": "user",
            "content": message[:3000],
        })
        history = history[-MAX_HISTORY:]

        try:
            reply = await asyncio.to_thread(call_ai, history)

        except requests.HTTPError as exc:
            response = exc.response

            if response is not None:
                log.error(
                    "AI HTTP error %s: %s",
                    response.status_code,
                    response.text[:1500],
                )
            else:
                log.exception("AI HTTP request failed")

            return

        except requests.RequestException:
            log.exception("AI network request failed")
            return

        except Exception:
            log.exception("AI response processing failed")
            return

        if not reply:
            log.error("AI returned an empty reply")
            return

        await event.respond(reply[:4000])

        history.append({
            "role": "assistant",
            "content": reply[:3000],
        })

        state["history"][key] = history[-MAX_HISTORY:]
        state["last_reply"][key] = now
        save_state()

    except Exception:
        log.exception("Incoming message processing failed")


async def handle_command(message):
    global transfer_task

    sender = message.get("from", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()

    if sender.get("id") != owner_id:
        return

    if chat.get("type") != "private":
        return

    parts = text.split()

    if not parts:
        return

    command = parts[0].split("@")[0].lower()

    if command == "/start":
        await bot_send(
            chat_id,
            "Controller\n\n"
            "/folders\n"
            "/next camera 100\n"
            "/next snapchat 50\n"
            "/next screenshots 100\n"
            "/next videos 20\n"
            "/next all 100\n"
            "/status\n"
            "/pause\n"
            "/resume\n"
            "/resetoffset camera\n"
            "/ai on\n"
            "/ai off\n\n"
            "Personal account: send .ai or .ai off in Saved Messages."
        )

    elif command == "/folders":
        summary = await asyncio.to_thread(folder_summary)
        await bot_send(chat_id, summary)

    elif command == "/next":
        if len(parts) != 3:
            await bot_send(chat_id, "Usage: /next camera 100")
            return

        category = parts[1].lower()

        if category not in FOLDERS:
            await bot_send(chat_id, "Unknown category. Use /folders.")
            return

        try:
            count = int(parts[2])
        except ValueError:
            await bot_send(chat_id, "Count must be numeric.")
            return

        if not 1 <= count <= MAX_BATCH:
            await bot_send(chat_id, f"Count must be 1-{MAX_BATCH}.")
            return

        if transfer_task and not transfer_task.done():
            await bot_send(chat_id, "A transfer is already running.")
            return

        state["last_transfer"] = {
            "category": category,
            "count": count,
            "chat_id": chat_id,
        }
        save_state()

        transfer_task = asyncio.create_task(
            run_batch(chat_id, category, count)
        )

    elif command == "/status":
        await bot_send(
            chat_id,
            f"Transfer: {transfer_status}\n"
            f"AI auto-replies: "
            f"{'ON' if state['ai_enabled'] else 'OFF'}\n"
            f"Storage accessible: {STORAGE.exists()}\n"
            f"Paused: {not pause_event.is_set()}"
        )

    elif command == "/pause":
        pause_event.clear()
        await bot_send(chat_id, "Transfer paused.")

    elif command == "/resume":
        pause_event.set()

        if transfer_task and not transfer_task.done():
            await bot_send(chat_id, "Transfer resumed.")
        else:
            last = state.get("last_transfer")

            if not last:
                await bot_send(chat_id, "No saved batch. Start a new batch.")
                return

            if last["chat_id"] != chat_id:
                await bot_send(chat_id, "Saved batch belongs to another chat.")
                return

            transfer_task = asyncio.create_task(
                run_batch(
                    chat_id,
                    last["category"],
                    last["count"],
                )
            )

            await bot_send(chat_id, "Retrying from the saved position.")

    elif command == "/resetoffset":
        if len(parts) != 2 or parts[1].lower() not in FOLDERS:
            await bot_send(chat_id, "Usage: /resetoffset camera")
            return

        if transfer_task and not transfer_task.done():
            await bot_send(chat_id, "Finish the active batch first.")
            return

        category = parts[1].lower()
        state["offsets"][category] = 0
        save_state()

        await bot_send(chat_id, f"{category} position reset.")

    elif command == "/ai":
        if len(parts) != 2 or parts[1].lower() not in {"on", "off"}:
            await bot_send(chat_id, "Usage: /ai on or /ai off")
            return

        state["ai_enabled"] = parts[1].lower() == "on"
        save_state()

        await bot_send(
            chat_id,
            "AI auto-replies "
            + ("enabled." if state["ai_enabled"] else "disabled.")
        )


async def bot_polling():
    offset = None

    while True:
        try:
            params = {"timeout": 25}

            if offset is not None:
                params["offset"] = offset

            response = await asyncio.to_thread(
                requests.get,
                f"{BOT_URL}/getUpdates",
                params=params,
                timeout=35,
            )

            response.raise_for_status()
            result = response.json()

            if not result.get("ok"):
                log.error("Bot API error: %s", result)
                await asyncio.sleep(3)
                continue

            for update in result.get("result", []):
                offset = update["update_id"] + 1

                if "message" in update:
                    await handle_command(update["message"])

        except asyncio.CancelledError:
            raise

        except Exception:
            log.exception("Bot polling failed")
            await asyncio.sleep(4)


async def main():
    global owner_id

    if not STORAGE.exists():
        print("Storage permission missing. Run termux-setup-storage.")
        return

    await client.start()
    me = await client.get_me()
    owner_id = me.id

    session_path = Path(SESSION + ".session")

    if session_path.exists():
        session_path.chmod(0o600)

    print("Running.")
    print("AI replies:", "ON" if state["ai_enabled"] else "OFF")

    polling_task = asyncio.create_task(bot_polling())

    try:
        await client.run_until_disconnected()
    finally:
        polling_task.cancel()
        await asyncio.gather(polling_task, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped.")
