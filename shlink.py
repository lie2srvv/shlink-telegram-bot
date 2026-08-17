import asyncio
import json
import os
import random
import re
import string
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
)

# CONFIG
# Fill these via environment variables (or a .env file + python-dotenv).
# BOT_TOKEN     - Telegram bot token from @BotFather
# SHLINK_URL    - Shlink instance API URL, e.g. http://localhost:8082
# SHLINK_API    - Shlink API key (Shlink admin panel -> API keys)
# SHLINK_DOMAIN - public domain used for short links, e.g. https://your-domain.com
# ADMIN_ID      - your Telegram numeric user id
BOT_TOKEN       = os.environ["BOT_TOKEN"]
SHLINK_URL      = os.environ.get("SHLINK_URL", "http://localhost:8082")
SHLINK_API      = os.environ["SHLINK_API"]
SHLINK_DOMAIN   = os.environ["SHLINK_DOMAIN"]
ADMIN_ID        = int(os.environ["ADMIN_ID"])
DB_FILE         = "users.json"
FILES_DIR       = Path(os.environ.get("FILES_DIR", "/var/www/files"))
FILES_DB        = "files.json"
MAX_FILE_SIZE   = 50 * 1024 * 1024

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

FILES_DIR.mkdir(parents=True, exist_ok=True)

# USERS DB

def load_db() -> set[int]:
    if not os.path.exists(DB_FILE):
        return set()
    with open(DB_FILE) as f:
        return set(json.load(f))

def save_db(users: set[int]) -> None:
    with open(DB_FILE, "w") as f:
        json.dump(list(users), f)

def is_allowed(user_id: int) -> bool:
    return user_id == ADMIN_ID or user_id in load_db()

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

# FILES DB

def load_files_db() -> dict:
    if not os.path.exists(FILES_DB):
        return {}
    with open(FILES_DB) as f:
        return json.load(f)

def save_files_db(data: dict) -> None:
    with open(FILES_DB, "w") as f:
        json.dump(data, f)

def register_file(filename: str, expire_ts: float | None, owner_id: int = 0) -> None:
    data = load_files_db()
    data[filename] = {"expire_ts": expire_ts, "owner_id": owner_id}
    save_files_db(data)

def cleanup_expired_files() -> list[str]:
    """Delete expired files from disk, return list of removed filenames."""
    data = load_files_db()
    now = datetime.now(timezone.utc).timestamp()
    to_delete = []
    for fn, val in data.items():
        ts = val["expire_ts"] if isinstance(val, dict) else val
        if ts is not None and ts < now:
            to_delete.append(fn)
    for fn in to_delete:
        path = FILES_DIR / fn
        if path.exists():
            path.unlink()
        del data[fn]
    if to_delete:
        save_files_db(data)
    return to_delete

# SLUGS DB
SLUGS_DB = "slugs.json"

def load_slugs_db() -> dict:
    if not os.path.exists(SLUGS_DB):
        return {}
    with open(SLUGS_DB) as f:
        return json.load(f)

def save_slugs_db(data: dict) -> None:
    with open(SLUGS_DB, "w") as f:
        json.dump(data, f)

def register_slug(slug: str, owner_id: int, short_url: str = "", long_url: str = "") -> None:
    data = load_slugs_db()
    data[slug] = {"owner_id": owner_id, "short_url": short_url, "long_url": long_url}
    save_slugs_db(data)

def get_slug_owner(slug: str) -> int | None:
    data = load_slugs_db()
    val = data.get(slug)
    if isinstance(val, dict):
        return val.get("owner_id")
    return val

def remove_slug(slug: str) -> None:
    data = load_slugs_db()
    data.pop(slug, None)
    save_slugs_db(data)

# SHLINK API

def random_slug(length: int = 6) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))

def normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url

async def create_short_link(long_url: str, days: int | None, max_attempts: int = 5) -> dict:
    headers = {
        "X-Api-Key": SHLINK_API,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    base_payload: dict = {
        "longUrl": long_url,
        "findIfExists": False,
    }
    if days is not None:
        expire_at = (datetime.now(timezone.utc) + timedelta(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        base_payload["validUntil"] = expire_at

    last_error: str = "unknown error"
    # First attempts use a 6-char slug, fall back to 7 chars if it keeps colliding.
    for attempt in range(max_attempts):
        length = 6 if attempt < max_attempts - 2 else 7
        payload = dict(base_payload)
        payload["customSlug"] = random_slug(length)

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{SHLINK_URL}/rest/v3/short-urls",
                headers=headers,
                json=payload,
            ) as resp:
                text = await resp.text()
                if resp.content_type != "application/json":
                    raise RuntimeError(f"Shlink returned non-JSON response (status {resp.status}).")
                data = json.loads(text)
                if resp.status in (200, 201):
                    return data

                error_type = data.get("type", "")
                detail = data.get("detail", f"API error: {resp.status}")
                if resp.status == 400 and "invalid-slug" in error_type.lower():
                    last_error = detail
                    continue
                raise RuntimeError(detail)

    raise RuntimeError(
        f"Could not find a free slug in {max_attempts} attempts: {last_error}"
    )

async def delete_short_link(slug: str) -> None:
    headers = {
        "X-Api-Key": SHLINK_API,
        "Accept": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.delete(
            f"{SHLINK_URL}/rest/v3/short-urls/{slug}",
            headers=headers,
        ) as resp:
            if resp.status not in (200, 204):
                raise RuntimeError(f"Failed to delete link: {resp.status}")

def slug_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1]

async def get_short_link_info(slug: str) -> dict | None:
    """Return link data from Shlink, or None if it doesn't exist (404)."""
    headers = {"X-Api-Key": SHLINK_API, "Accept": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{SHLINK_URL}/rest/v3/short-urls/{slug}",
            headers=headers,
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            return None

def _is_expired(info: dict) -> bool:
    valid_until = info.get("meta", {}).get("validUntil")
    if not valid_until:
        return False
    try:
        dt = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
    except ValueError:
        return False
    return dt < datetime.now(timezone.utc)

async def cleanup_expired_slugs(deleted_filenames: list[str] | None = None) -> list[str]:
    """
    Walk slugs.json and actually remove from Shlink (and the local DB) links that:
      - no longer exist in Shlink (deleted manually / out of sync),
      - expired via validUntil,
      - point to a file that was just deleted because it expired.
    Returns the list of removed slugs.
    """
    data = load_slugs_db()
    if not data:
        return []

    deleted_filenames = set(deleted_filenames or [])
    removed: list[str] = []

    for slug in list(data.keys()):
        try:
            info = await get_short_link_info(slug)
        except Exception:
            continue  # network/API unavailable, skip this round

        long_url = ""
        val = data.get(slug)
        if isinstance(val, dict):
            long_url = val.get("long_url", "")
        points_to_deleted_file = any(
            long_url.endswith(f"/files/{fn}") for fn in deleted_filenames
        )

        should_remove = False
        if info is None:
            should_remove = True
        elif _is_expired(info):
            should_remove = True
        elif points_to_deleted_file:
            should_remove = True

        if not should_remove:
            continue

        if info is not None:
            try:
                await delete_short_link(slug)
            except Exception:
                pass  # may have been deleted concurrently, ignore

        remove_slug(slug)
        removed.append(slug)

    return removed

# STORAGE FOR PENDING INPUTS
pending_url:    dict[int, str]  = {}
pending_file:   dict[int, dict] = {}
pending_inline: dict[str, str]  = {}

# KEYBOARDS

def expiry_keyboard(is_admin_user: bool, prefix: str = "exp") -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="1 день",  callback_data=f"{prefix}_1"),
            InlineKeyboardButton(text="3 дня",   callback_data=f"{prefix}_3"),
            InlineKeyboardButton(text="7 дней",  callback_data=f"{prefix}_7"),
        ],
    ]
    if is_admin_user:
        buttons.append([
            InlineKeyboardButton(text="♾ Бессрочно", callback_data=f"{prefix}_0"),
        ])
    buttons.append([
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"{prefix}_cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# HANDLERS: commands

@dp.message(Command("start"))
async def cmd_start(msg: Message):
    if not is_allowed(msg.from_user.id):
        return
    text = "👋 Привет! Отправь ссылку или файл — я создам короткую ссылку.\n\nКоманды:\n/start — это сообщение\n/list — список ваших ссылок\n/delete <ссылка> — удалить ссылку (и файл если есть)\n"
    if is_admin(msg.from_user.id):
        text += "/add <id> — добавить пользователя\n/remove <id> — удалить пользователя\n/db — список пользователей\n"
    await msg.answer(text)

@dp.message(Command("list"))
async def cmd_list(msg: Message):
    if not is_allowed(msg.from_user.id):
        return

    wait_msg = await msg.answer("🔎 Проверяю ссылки...")

    # Clean up expired / dangling links first so the list doesn't show dead entries.
    try:
        deleted_files = cleanup_expired_files()
        await cleanup_expired_slugs(deleted_files)
    except Exception as e:
        print(f"[list] cleanup error: {e}")

    data = load_slugs_db()
    user_slugs = []

    for slug, val in data.items():
        if isinstance(val, dict):
            if val.get("owner_id") == msg.from_user.id:
                user_slugs.append((slug, val.get("long_url", "")))
        else:
            if val == msg.from_user.id:
                user_slugs.append((slug, ""))

    if not user_slugs:
        await wait_msg.edit_text("📭 У вас нет активных ссылок.")
        return

    lines = []
    domain_clean = SHLINK_DOMAIN.replace("https://", "").replace("http://", "")
    db_updated = False
    dead_slugs = []

    for slug, long_url in user_slugs:
        try:
            info = await get_short_link_info(slug)
        except Exception:
            info = "unknown"  # network unavailable, show as-is without deleting

        if info is None:
            dead_slugs.append(slug)
            continue

        if isinstance(info, dict):
            if not long_url:
                long_url = info.get("longUrl", "")
                data[slug] = {
                    "owner_id": msg.from_user.id,
                    "short_url": f"{SHLINK_DOMAIN}/{slug}",
                    "long_url": long_url,
                }
                db_updated = True

        display_short_clean = f"{domain_clean}/{slug}"
        if long_url:
            lines.append(f"• {display_short_clean} -> {long_url}")
        else:
            lines.append(f"• {display_short_clean}")

    for slug in dead_slugs:
        remove_slug(slug)

    if db_updated:
        save_slugs_db(data)

    if not lines:
        await wait_msg.edit_text("📭 У вас нет активных ссылок.")
        return

    await wait_msg.edit_text("🔗 <b>Ваши ссылки:</b>\n" + "\n".join(lines), parse_mode="HTML")

@dp.message(Command("add"))
async def cmd_add(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await msg.answer("Использование: /add <telegram_id>")
        return
    uid = int(parts[1])
    if uid == ADMIN_ID:
        await msg.answer("Это же сам админ 😄")
        return
    users = load_db()
    if uid in users:
        await msg.answer(f"Пользователь {uid} уже есть в базе.")
        return
    users.add(uid)
    save_db(users)
    await msg.answer(f"✅ Пользователь {uid} добавлен.")

@dp.message(Command("remove"))
async def cmd_remove(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await msg.answer("Использование: /remove <telegram_id>")
        return
    uid = int(parts[1])
    users = load_db()
    if uid not in users:
        await msg.answer(f"Пользователь {uid} не найден в базе.")
        return
    users.discard(uid)
    save_db(users)
    await msg.answer(f"🗑 Пользователь {uid} удалён.")

@dp.message(Command("db"))
async def cmd_db(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    users = load_db()
    if not users:
        await msg.answer("📭 База пользователей пуста.")
        return
    lines = "\n".join(f"• {uid}" for uid in sorted(users))
    await msg.answer(f"👥 Пользователи ({len(users)}):\n{lines}")

@dp.message(Command("delete"))
async def cmd_delete(msg: Message):
    if not is_allowed(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) != 2:
        await msg.answer("Использование: /delete <короткая_ссылка>\nПример: /delete your-domain.com/abc123")
        return

    short_url = parts[1].strip()
    slug = slug_from_url(short_url)

    owner = get_slug_owner(slug)
    if not is_admin(msg.from_user.id):
        if owner is None or owner != msg.from_user.id:
            return

    wait_msg = await msg.answer(f"🗑 Удаляю <code>{slug}</code>...", parse_mode="HTML")

    files_data = load_files_db()
    deleted_file = False
    file_lookup_failed = False

    try:
        info = await get_short_link_info(slug)
        long_url = info.get("longUrl", "") if info else ""
        if "/files/" in long_url:
            filename = long_url.split("/files/")[-1]
            if filename in files_data:
                path = FILES_DIR / filename
                if path.exists():
                    path.unlink()
                del files_data[filename]
                save_files_db(files_data)
                deleted_file = True
    except Exception:
        # couldn't tell whether a file is attached, warn but don't block deletion
        file_lookup_failed = True

    try:
        await delete_short_link(slug)
    except Exception as e:
        # if it's already gone from Shlink, just clean up the local DB
        if "404" not in str(e):
            await wait_msg.edit_text(f"❌ Ошибка удаления ссылки: {e}")
            return

    remove_slug(slug)

    file_note = ""
    if deleted_file:
        file_note = "\n🗂 Файл тоже удалён."
    elif file_lookup_failed:
        file_note = "\n⚠️ Не удалось проверить, привязан ли файл — проверьте вручную."

    await wait_msg.edit_text(f"✅ Ссылка <code>{slug}</code> удалена.{file_note}", parse_mode="HTML")

# HANDLERS: links

@dp.message(F.text.regexp(r"(https?://)?([\w\-.]+\.)+[\w\-]{2,}(/\S*)?"))
async def handle_url(msg: Message):
    if not is_allowed(msg.from_user.id):
        return
    url = normalize_url(msg.text.strip())
    pending_url[msg.from_user.id] = url
    await msg.answer(
        f"🔗 <code>{url}</code>\n\nВыбери срок действия ссылки:",
        parse_mode="HTML",
        reply_markup=expiry_keyboard(is_admin(msg.from_user.id), prefix="exp"),
    )

@dp.callback_query(F.data.startswith("exp_"))
async def cb_expiry(call: CallbackQuery):
    if not is_allowed(call.from_user.id):
        await call.answer()
        return

    choice = call.data.split("_")[1]

    if choice == "cancel":
        pending_url.pop(call.from_user.id, None)
        await call.message.edit_text("❌ Отменено.")
        return

    url = pending_url.pop(call.from_user.id, None)
    if not url:
        return

    if choice == "0" and not is_admin(call.from_user.id):
        return

    days = None if choice == "0" else int(choice)
    await call.message.edit_text("⏳ Создаю ссылку...")

    try:
        data      = await create_short_link(url, days)
        short_url = data["shortUrl"]
        expires_at = data.get("meta", {}).get("validUntil")

        if expires_at:
            dt_utc = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            dt_msk = dt_utc.astimezone(timezone(timedelta(hours=3)))
            expire_str = dt_msk.strftime("📅 Истекает: %d.%m.%Y %H:%M MSK")
        else:
            expire_str = "♾ Бессрочная"

        slug = slug_from_url(short_url)
        register_slug(slug, call.from_user.id, short_url, url)
        await call.message.edit_text(
            f"✅ Готово!\n\n🔗 {short_url}\n{expire_str}",
            parse_mode="HTML",
        )
    except Exception as e:
        await call.message.edit_text(f"❌ Ошибка: {e}")

# HANDLERS: files

@dp.message(F.document | F.photo | F.video | F.audio | F.voice | F.animation)
async def handle_file(msg: Message):
    if not is_allowed(msg.from_user.id):
        return

    if msg.document:
        file_obj  = msg.document
        orig_name = file_obj.file_name or "file"
        file_size = file_obj.file_size
    elif msg.photo:
        file_obj  = msg.photo[-1]
        orig_name = "photo.jpg"
        file_size = file_obj.file_size
    elif msg.video:
        file_obj  = msg.video
        orig_name = file_obj.file_name or "video.mp4"
        file_size = file_obj.file_size
    elif msg.audio:
        file_obj  = msg.audio
        orig_name = file_obj.file_name or "audio.mp3"
        file_size = file_obj.file_size
    elif msg.voice:
        file_obj  = msg.voice
        orig_name = "voice.ogg"
        file_size = file_obj.file_size
    elif msg.animation:
        file_obj  = msg.animation
        orig_name = file_obj.file_name or "animation.gif"
        file_size = file_obj.file_size
    else:
        return

    if not is_admin(msg.from_user.id) and file_size and file_size > MAX_FILE_SIZE:
        return

    stem     = Path(orig_name).stem
    ext      = Path(orig_name).suffix or ""
    filename = f"{stem}{ext}"
    counter  = 1
    while (FILES_DIR / filename).exists():
        filename = f"{stem} ({counter}){ext}"
        counter += 1

    wait_msg = await msg.answer("⬇️ Скачиваю файл...")
    try:
        tg_file = await bot.get_file(file_obj.file_id)
        dest    = FILES_DIR / filename
        await bot.download_file(tg_file.file_path, destination=str(dest))
    except Exception as e:
        await wait_msg.edit_text(f"❌ Не удалось скачать файл: {e}")
        return

    file_url = f"{SHLINK_DOMAIN}/files/{filename}"
    pending_file[msg.from_user.id] = {"filename": filename, "file_url": file_url}

    await wait_msg.edit_text(
        f"📁 <b>{orig_name}</b>\n📦 {(file_size or 0) / 1024 / 1024:.2f} МБ\n\nВыбери срок хранения файла:",
        parse_mode="HTML",
        reply_markup=expiry_keyboard(is_admin(msg.from_user.id), prefix="fexp"),
    )

@dp.callback_query(F.data.startswith("fexp_"))
async def cb_file_expiry(call: CallbackQuery):
    if not is_allowed(call.from_user.id):
        await call.answer()
        return

    choice = call.data.split("_")[1]

    if choice == "cancel":
        info = pending_file.pop(call.from_user.id, None)
        if info:
            p = FILES_DIR / info["filename"]
            if p.exists():
                p.unlink()
        await call.message.edit_text("❌ Отменено, файл удалён.")
        return

    info = pending_file.pop(call.from_user.id, None)
    if not info:
        return

    if choice == "0" and not is_admin(call.from_user.id):
        return

    days = None if choice == "0" else int(choice)

    if days is not None:
        expire_ts = (datetime.now(timezone.utc) + timedelta(days=days)).timestamp()
        dt_msk = datetime.now(timezone(timedelta(hours=3))) + timedelta(days=days)
        expire_str = dt_msk.strftime("📅 Истекает: %d.%m.%Y %H:%M MSK")
    else:
        expire_ts = None
        expire_str = "♾ Бессрочно"

    register_file(info["filename"], expire_ts, owner_id=call.from_user.id)
    await call.message.edit_text("⏳ Создаю ссылку...")

    try:
        data      = await create_short_link(info["file_url"], days)
        short_url = data["shortUrl"]

        slug = slug_from_url(short_url)
        register_slug(slug, call.from_user.id, short_url, info["file_url"])
        await call.message.edit_text(
            f"✅ Готово!\n\n🔗 {short_url}\n{expire_str}",
            parse_mode="HTML",
        )
    except Exception as e:
        await call.message.edit_text(f"❌ Ошибка: {e}")

# HANDLERS: inline mode

@dp.inline_query()
async def inline_handler(query: InlineQuery):
    if not is_allowed(query.from_user.id):
        return

    text = query.query.strip()
    if not text:
        return

    url_pattern = re.compile(r"(https?://)?([\w\-.]+\.)+[\w\-]{2,}(/\S*)?")
    if not url_pattern.match(text):
        return

    url = normalize_url(text)
    unique_id = uuid.uuid4().hex[:10]
    pending_inline[unique_id] = url

    buttons = [
        [
            InlineKeyboardButton(text="1 день",  callback_data=f"inexp_1_{unique_id}"),
            InlineKeyboardButton(text="3 дня",   callback_data=f"inexp_3_{unique_id}"),
            InlineKeyboardButton(text="7 дней",  callback_data=f"inexp_7_{unique_id}"),
        ],
    ]
    if is_admin(query.from_user.id):
        buttons.append([
            InlineKeyboardButton(text="♾ Бессрочно", callback_data=f"inexp_0_{unique_id}"),
        ])
    buttons.append([
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"inexp_cancel_{unique_id}"),
    ])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    result = InlineQueryResultArticle(
        id=uuid.uuid4().hex,
        title="Создать короткую ссылку со сроком",
        description=f"Ссылка: {url}",
        input_message_content=InputTextMessageContent(
            message_text=f"⏳ Выберите срок действия для ссылки: {url}"
        ),
        reply_markup=markup
    )
    await query.answer([result], cache_time=1, is_personal=True)

@dp.callback_query(F.data.startswith("inexp_"))
async def cb_inline_expiry(call: CallbackQuery):
    parts = call.data.split("_")
    if len(parts) < 3:
        return
    choice = parts[1]
    unique_id = parts[2]

    if not is_allowed(call.from_user.id):
        await call.answer()
        return

    url = pending_inline.get(unique_id)
    if not url:
        await call.answer("Сессия истекла.", show_alert=True)
        return

    if choice == "cancel":
        pending_inline.pop(unique_id, None)
        await bot.edit_message_text(
            text="❌ Отменено.",
            inline_message_id=call.inline_message_id
        )
        return

    if choice == "0" and not is_admin(call.from_user.id):
        await call.answer()
        return

    days = None if choice == "0" else int(choice)

    try:
        data = await create_short_link(url, days)
        short_url = data["shortUrl"]
        expires_at = data.get("meta", {}).get("validUntil")

        if expires_at:
            dt_utc = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            dt_msk = dt_utc.astimezone(timezone(timedelta(hours=3)))
            expire_str = dt_msk.strftime("📅 Истекает: %d.%m.%Y %H:%M MSK")
        else:
            expire_str = "♾ Бессрочная"

        slug = slug_from_url(short_url)
        register_slug(slug, call.from_user.id, short_url, url)

        await bot.edit_message_text(
            text=short_url,
            inline_message_id=call.inline_message_id
        )

        pm_text = f"✅ Готово!\n\n🔗 {short_url}\n{expire_str}"
        try:
            await bot.send_message(chat_id=call.from_user.id, text=pm_text, parse_mode="HTML")
        except Exception:
            pass

        pending_inline.pop(unique_id, None)
    except Exception as e:
        await call.answer(f"Ошибка: {e}", show_alert=True)

# BACKGROUND TASK: expired file cleanup

async def file_cleanup_loop():
    while True:
        try:
            deleted_files = cleanup_expired_files()
            await cleanup_expired_slugs(deleted_files)
        except Exception as e:
            print(f"[cleanup] background cleanup error: {e}")
        await asyncio.sleep(3600)

# MAIN

async def main():
    print("Bot started...")
    asyncio.create_task(file_cleanup_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
