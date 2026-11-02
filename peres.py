# -*- coding: utf-8 -*-
"""
Sonata Support Bot v2 (aiogram 3 + MySQL).

Что изменено по сравнению с прошлой версией:
  * секреты берутся из .env / переменных окружения, а не из кода;
  * MySQL: пул соединений + выполнение в потоках, бот больше не "замирает" на каждом запросе;
  * открытие тикета делается одной транзакцией, ответ пользователю уходит сразу;
  * штрафы считает только сайт-логика (ticket_waits): бот пишет ту же схему и не дублирует штрафы;
  * сообщения и медиа пишутся в message_logs в формате, который понимает сайт (Админ #ID, media_type, file_id);
  * кнопка "Сотрудники": список, карточка, логин/пароль для сайта (bcrypt), отключение устройств;
  * FAQ превращён в меню быстрых ответов.
"""
import asyncio
import datetime as dt
import html
import logging
import os
import re
import secrets
import subprocess
import sys
import time
from contextlib import contextmanager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _pip(pkg: str):
    logging.warning("Устанавливаю %s ...", pkg)
    subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])


try:
    import mysql.connector
except ModuleNotFoundError:
    _pip("mysql-connector-python")
    import mysql.connector
try:
    import bcrypt
except ModuleNotFoundError:
    _pip("bcrypt")
    import bcrypt

from mysql.connector import errors as mysql_errors
from mysql.connector import pooling

from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ==========================================================
# Настройки (.env рядом с файлом или переменные окружения)
# ==========================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_env(name: str = ".env"):
    path = os.path.join(BASE_DIR, name)
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


def _need(name: str) -> str:
    v = os.getenv(name)
    if not v:
        sys.exit(f"Не задана переменная {name}. Заполните файл .env (см. .env.example)")
    return v


TOKEN = _need("BOT_TOKEN")
OWNER_ID = int(_need("OWNER_ID"))  # создатель системы: его роль и доступ никто не может менять
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_USER = os.getenv("MYSQL_USER", "sonata_user")
MYSQL_PASSWORD = _need("MYSQL_PASSWORD")
MYSQL_DB = os.getenv("MYSQL_DB", "sonata_support")
PANEL_URL = os.getenv("PANEL_URL", "https://sn-go.ru/report34598724").strip()  # например https://sn-go.ru/
PENALTY_MINUTES = int(os.getenv("PENALTY_MINUTES", "10"))  # должно совпадать с $penalty_minutes в PHP

H = {"parse_mode": "HTML"}
os.makedirs(os.path.join(BASE_DIR, "chat_logs"), exist_ok=True)

# ==========================================================
# Быстрые ответы (FAQ) - правьте тексты под свой сервис
# ==========================================================
FAQ = {
    "wait": (
        "Сколько ждать ответа",
        "Обычно администраторы отвечают в течение 10-15 минут. Пока вы ждёте, можно "
        "дописать детали обращения: все сообщения попадут в тот же тикет.",
    ),
    "noconn": (
        "VPN не подключается",
        "1. Обновите подписку в приложении.\n"
        "2. Смените сервер в списке.\n"
        "3. Проверьте, что на устройстве верно выставлены дата и время.\n"
        "Если не помогло, напишите в поддержку и укажите устройство, приложение и время ошибки.",
    ),
    "slow": (
        "Низкая скорость",
        "Попробуйте другой сервер из списка и другой протокол. Проверьте скорость без VPN: "
        "если она низкая и без него, дело в вашем провайдере. Если проблема только с VPN, "
        "пришлите в поддержку название сервера и время замера.",
    ),
    "update": (
        "Обновление подписки",
        "Если пропал интернет или серверы не загружаются, в приложении нажмите три точки "
        "рядом с названием подписки и выберите «Обновить подписку» (Update).",
    ),
    "sites": (
        "Не открывается сайт или приложение",
        "Переподключитесь к VPN и смените сервер. Перезапустите проблемное приложение. "
        "Если сервис всё равно не работает, напишите в поддержку: укажите название сервиса и текст ошибки.",
    ),
    "setup": (
        "Установка и настройка",
        "Установите приложение для вашей платформы, добавьте подписку по ссылке или QR-коду, "
        "выберите сервер и включите подключение. Если на каком-то шаге возникла ошибка, "
        "пришлите скриншот в поддержку.",
    ),
    "tv": (
        "Телевизор (Smart TV)",
        "Для Android TV скачайте приложение Karing или NekoBox из Google Play и отсканируйте ваш QR-код. "
        "Для Apple TV используйте Streisand.",
    ),
    "devices": (
        "Лимит устройств",
        "Количество одновременных подключений зависит от вашего тарифа. Если на новом устройстве "
        "подключение не проходит, а на старых всё работает, возможно, лимит исчерпан. Напишите в поддержку, мы проверим.",
    ),
    "pay": (
        "Оплата и продление",
        "Если оплата прошла, а доступ не появился, напишите в поддержку и приложите чек или время платежа. "
        "Мы проверим и активируем доступ.",
    ),
    "media": (
        "Что можно присылать в чат",
        "Скриншоты ошибок, фото, видео, документы, голосовые сообщения и кружки. "
        "Скриншот часто ускоряет решение вопроса.",
    ),
}

ROLE_RU = {"owner": "Главный администратор", "admin": "Администратор"}
LOGIN_RE = re.compile(r"^[a-z0-9_.\-]{3,32}$")
PW_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# ==========================================================
# MySQL: пул + хелперы
# ==========================================================
_pool = pooling.MySQLConnectionPool(
    pool_name="sonata",
    pool_size=10,
    pool_reset_session=False,
    host=MYSQL_HOST,
    user=MYSQL_USER,
    password=MYSQL_PASSWORD,
    database=MYSQL_DB,
    charset="utf8mb4",
    autocommit=True,
    connection_timeout=8,
)


def _conn():
    c = _pool.get_connection()
    c.ping(reconnect=True, attempts=2, delay=0)
    return c


def q(sql: str, params=(), fetch=None):
    """Один запрос. fetch: None (rowcount) | 'one' | 'all'."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        if fetch == "one":
            res = cur.fetchone()
        elif fetch == "all":
            res = cur.fetchall()
        else:
            res = cur.rowcount
        cur.close()
        return res
    finally:
        conn.close()


@contextmanager
def tx():
    """Транзакция: несколько запросов за одно соединение."""
    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute("START TRANSACTION")
        yield cur
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cur.close()
        conn.close()


async def aq(sql: str, params=(), fetch=None):
    return await asyncio.to_thread(q, sql, params, fetch)


async def run(fn, *args):
    return await asyncio.to_thread(fn, *args)


DDL = [
    """CREATE TABLE IF NOT EXISTS admins (
        user_id BIGINT PRIMARY KEY,
        role VARCHAR(50) DEFAULT 'admin',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS dialogs (
        user_id BIGINT PRIMARY KEY,
        username VARCHAR(100),
        first_name VARCHAR(100),
        last_message TEXT,
        status VARCHAR(50) DEFAULT 'open',
        dialog_token VARCHAR(50)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS admin_sessions (
        admin_id BIGINT PRIMARY KEY,
        target_user_id BIGINT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS admin_stats (
        admin_id BIGINT PRIMARY KEY,
        closed_count INT DEFAULT 0
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS message_logs (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id BIGINT,
        sender VARCHAR(100),
        text TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS closed_dialogs_archive (
        id INT AUTO_INCREMENT PRIMARY KEY,
        admin_id BIGINT,
        user_id BIGINT,
        username VARCHAR(100),
        closed_at VARCHAR(100),
        file_path TEXT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS banned_users (
        user_id BIGINT PRIMARY KEY,
        banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        reason TEXT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS submitted_reports (
        user_id BIGINT,
        admin_id BIGINT,
        PRIMARY KEY (user_id, admin_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS admin_rest (
        admin_id BIGINT PRIMARY KEY,
        is_resting INT DEFAULT 0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    # Таблицы сайта (схема совпадает с bootstrap_schema в support_panel.php)
    """CREATE TABLE IF NOT EXISTS admin_penalties (
        id INT AUTO_INCREMENT PRIMARY KEY,
        admin_id BIGINT NOT NULL,
        reason TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS ticket_waits (
        user_id BIGINT NOT NULL PRIMARY KEY,
        waiting_since TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        taken_at TIMESTAMP NULL DEFAULT NULL,
        penalized TINYINT NOT NULL DEFAULT 0
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS admin_auth (
        admin_id BIGINT NOT NULL PRIMARY KEY,
        login VARCHAR(64) NOT NULL,
        pass_hash VARCHAR(255) NOT NULL,
        created_by BIGINT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        last_login TIMESTAMP NULL DEFAULT NULL,
        UNIQUE KEY uq_login (login)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS admin_devices (
        id INT AUTO_INCREMENT PRIMARY KEY,
        admin_id BIGINT NOT NULL,
        token_hash CHAR(64) NOT NULL,
        label VARCHAR(120) NOT NULL DEFAULT '',
        ip VARCHAR(64) NOT NULL DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_tok (token_hash),
        KEY idx_admin (admin_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]


def _ensure_col(table: str, col: str, definition: str):
    ok = q(
        "SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
        (table, col), "one",
    )[0]
    if not ok:
        q(f"ALTER TABLE `{table}` ADD COLUMN `{col}` {definition}")


def _ensure_index(table: str, name: str, cols: str):
    ok = q(
        "SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME=%s",
        (table, name), "one",
    )[0]
    if not ok:
        q(f"ALTER TABLE `{table}` ADD INDEX `{name}` ({cols})")


def init_db():
    for ddl in DDL:
        q(ddl)
    _ensure_col("message_logs", "media_type", "VARCHAR(16) NULL DEFAULT NULL")
    _ensure_col("message_logs", "file_id", "VARCHAR(255) NULL DEFAULT NULL")
    # Индексы: без них каждый запрос по user_id читает всю таблицу
    _ensure_index("message_logs", "idx_ml_user", "user_id, id")
    _ensure_index("admin_sessions", "idx_as_target", "target_user_id")
    _ensure_index("dialogs", "idx_dialogs_status", "status")
    _ensure_index("closed_dialogs_archive", "idx_arch_user", "user_id, id")
    q(
        "INSERT INTO admins (user_id, role) VALUES (%s, 'owner') ON DUPLICATE KEY UPDATE role = 'owner'",
        (OWNER_ID,),
    )


# ==========================================================
# Мелочи: экранирование, роли, права
# ==========================================================
def esc(s) -> str:
    return html.escape("" if s is None else str(s), quote=False)


def pre(s) -> str:
    return f"<pre>{esc(s)}</pre>"


def quote(s) -> str:
    return f"<blockquote>{esc(s)}</blockquote>"


_role_cache: dict = {}
ROLE_TTL = 15


async def get_role(uid: int):
    if uid == OWNER_ID:
        return "owner"
    hit = _role_cache.get(uid)
    now = time.monotonic()
    if hit and now - hit[1] < ROLE_TTL:
        return hit[0]
    row = await aq("SELECT role FROM admins WHERE user_id=%s", (uid,), "one")
    role = row[0] if row else None
    _role_cache[uid] = (role, now)
    return role


class RoleFilter(BaseFilter):
    def __init__(self, owner: bool = False):
        self.owner = owner

    async def __call__(self, event) -> bool:
        if not event.from_user:
            return False
        role = await get_role(event.from_user.id)
        return role == "owner" if self.owner else role in ("admin", "owner")


def can_manage(actor_id: int, target_id: int, target_role) -> bool:
    """Как на сайте: создатель неприкосновенен, владельцев меняет только создатель."""
    if target_id == OWNER_ID or target_role == "owner":
        return actor_id == OWNER_ID
    return True


def fmt_dt(v) -> str:
    if not v:
        return "нет"
    if isinstance(v, dt.datetime):
        return v.strftime("%d.%m.%Y %H:%M")
    return str(v)


def days_text(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} дня"
    return f"{n} дней"


async def safe_edit(cb: types.CallbackQuery, text: str, kb=None) -> bool:
    try:
        await cb.message.edit_text(text, reply_markup=kb, **H)
        return True
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return False
        try:
            await cb.message.delete()
        except Exception:
            pass
        await cb.message.answer(text, reply_markup=kb, **H)
        return True


def panel_button():
    return InlineKeyboardButton(text="Открыть веб-панель", url=PANEL_URL) if PANEL_URL else None


# ==========================================================
# Описание входящего сообщения (текст / медиа) для логов и сайта
# ==========================================================
def describe(m: types.Message):
    """-> (preview для списков, text для message_logs, media_type, file_id)"""
    if m.text:
        return m.text, m.text, None, None
    cap = m.caption or ""
    if m.photo:
        mt, fid, label = "photo", m.photo[-1].file_id, "Фото"
    elif m.video:
        mt, fid, label = "video", m.video.file_id, "Видео"
    elif m.animation:
        mt, fid, label = "video", m.animation.file_id, "GIF"
    elif m.voice:
        mt, fid, label = "voice", m.voice.file_id, "Голосовое"
    elif m.video_note:
        mt, fid, label = "video_note", m.video_note.file_id, "Кружок"
    elif m.document:
        mt, fid, label = "document", m.document.file_id, "Файл"
    elif m.audio:
        mt, fid, label = "document", m.audio.file_id, "Аудио"
    elif m.sticker:
        return "[Стикер]", "[Стикер]", None, None
    else:
        t = f"[{m.content_type}]"
        return t, t, None, None
    return f"[{label}] {cap}".strip(), cap, mt, fid


MEDIA_LABEL = {"photo": "Фото", "video": "Видео", "voice": "Голосовое", "video_note": "Кружок", "document": "Файл"}


# ==========================================================
# Синхронные операции с БД (вызываются через asyncio.to_thread)
# ==========================================================
def process_user_message(uid, username, first_name, preview, log_text, mt, fid):
    """Одна транзакция на сообщение клиента: бан, логирование, создание тикета, список получателей."""
    with tx() as cur:
        cur.execute("SELECT 1 FROM banned_users WHERE user_id=%s", (uid,))
        if cur.fetchone():
            return {"kind": "banned"}
        cur.execute("SELECT status FROM dialogs WHERE user_id=%s FOR UPDATE", (uid,))
        row = cur.fetchone()
        status = row[0] if row else None
        ins = "INSERT INTO message_logs (user_id, sender, text, media_type, file_id) VALUES (%s, 'Пользователь', %s, %s, %s)"
        if status in ("open", "chatting"):
            cur.execute(ins, (uid, log_text, mt, fid))
            cur.execute("UPDATE dialogs SET last_message=%s WHERE user_id=%s", (preview[:50], uid))
            if status == "chatting":
                cur.execute("SELECT MIN(admin_id) FROM admin_sessions WHERE target_user_id=%s", (uid,))
                return {"kind": "chatting", "holder": cur.fetchone()[0]}
            return {"kind": "queued"}
        token = secrets.token_hex(4)
        cur.execute(
            """INSERT INTO dialogs (user_id, username, first_name, last_message, status, dialog_token)
               VALUES (%s, %s, %s, %s, 'open', %s)
               ON DUPLICATE KEY UPDATE username=VALUES(username), first_name=VALUES(first_name),
                   last_message=VALUES(last_message), status='open', dialog_token=VALUES(dialog_token)""",
            (uid, username, first_name, preview[:50], token),
        )
        cur.execute(ins, (uid, log_text, mt, fid))
        # Запускаем 10-минутный таймер сайта с момента создания тикета
        cur.execute(
            """INSERT INTO ticket_waits (user_id, waiting_since, taken_at, penalized) VALUES (%s, NOW(), NULL, 0)
               ON DUPLICATE KEY UPDATE waiting_since=NOW(), taken_at=NULL, penalized=0""",
            (uid,),
        )
        cur.execute(
            """SELECT a.user_id FROM admins a LEFT JOIN admin_rest r ON r.admin_id = a.user_id
               WHERE a.role IN ('admin', 'owner') AND (r.is_resting IS NULL OR r.is_resting = 0)"""
        )
        targets = [r[0] for r in cur.fetchall()] or [OWNER_ID]
        return {"kind": "new", "token": token, "targets": targets}


def claim_sync(admin_id: int, uid: int, token: str):
    """Взять тикет в работу (та же логика, что claim_ticket на сайте, включая штраф за опоздание)."""
    limit_sec = PENALTY_MINUTES * 60
    with tx() as cur:
        cur.execute("SELECT is_resting FROM admin_rest WHERE admin_id=%s", (admin_id,))
        r = cur.fetchone()
        if r and int(r[0]) == 1:
            return {"ok": False, "reason": "rest"}
        cur.execute("SELECT status, dialog_token FROM dialogs WHERE user_id=%s FOR UPDATE", (uid,))
        d = cur.fetchone()
        if not d or d[0] != "open" or d[1] != token:
            return {"ok": False, "reason": "stale"}
        cur.execute("SELECT 1 FROM admin_sessions WHERE target_user_id=%s", (uid,))
        if cur.fetchone():
            return {"ok": False, "reason": "stale"}
        cur.execute("SELECT target_user_id FROM admin_sessions WHERE admin_id=%s", (admin_id,))
        busy = cur.fetchone()
        if busy and busy[0] != uid:
            return {"ok": False, "reason": "busy", "other": busy[0]}
        cur.execute(
            "INSERT INTO admin_sessions (admin_id, target_user_id) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE target_user_id=VALUES(target_user_id)",
            (admin_id, uid),
        )
        cur.execute("UPDATE dialogs SET status='chatting' WHERE user_id=%s", (uid,))
        late_min = None
        cur.execute(
            "SELECT UNIX_TIMESTAMP() - UNIX_TIMESTAMP(waiting_since), penalized FROM ticket_waits WHERE user_id=%s AND taken_at IS NULL",
            (uid,),
        )
        w = cur.fetchone()
        if w and not int(w[1]) and int(w[0]) > limit_sec:
            late_min = int(round(int(w[0]) / 60))
            cur.execute(
                "INSERT INTO admin_penalties (admin_id, reason) VALUES (%s, %s)",
                (admin_id, f"Тикет пользователя {uid} принят через {late_min} мин (лимит {PENALTY_MINUTES} мин)"),
            )
        cur.execute(
            """INSERT INTO ticket_waits (user_id, waiting_since, taken_at, penalized) VALUES (%s, NOW(), NOW(), 0)
               ON DUPLICATE KEY UPDATE taken_at=NOW()""",
            (uid,),
        )
        cur.execute("SELECT sender, text, media_type FROM message_logs WHERE user_id=%s ORDER BY id DESC LIMIT 6", (uid,))
        hist = list(reversed(cur.fetchall()))
        return {"ok": True, "late_min": late_min, "history": hist}


def leave_sync(admin_id: int):
    with tx() as cur:
        cur.execute("SELECT target_user_id FROM admin_sessions WHERE admin_id=%s", (admin_id,))
        row = cur.fetchone()
        if not row:
            return None
        uid = row[0]
        cur.execute("DELETE FROM admin_sessions WHERE admin_id=%s", (admin_id,))
        cur.execute("UPDATE dialogs SET status='open' WHERE user_id=%s AND status='chatting'", (uid,))
        cur.execute(
            """INSERT INTO ticket_waits (user_id, waiting_since, taken_at, penalized) VALUES (%s, NOW(), NULL, 0)
               ON DUPLICATE KEY UPDATE waiting_since=NOW(), taken_at=NULL, penalized=0""",
            (uid,),
        )
        return uid


def close_sync(admin_id: int):
    row = q("SELECT target_user_id FROM admin_sessions WHERE admin_id=%s", (admin_id,), "one")
    if not row:
        return None
    uid = row[0]
    d = q("SELECT username FROM dialogs WHERE user_id=%s", (uid,), "one")
    username = (d[0] if d else None) or "unknown"
    history = q(
        "SELECT sender, text, timestamp, media_type FROM message_logs WHERE user_id=%s ORDER BY id ASC", (uid,), "all"
    )
    now = dt.datetime.now()
    file_path = os.path.join(BASE_DIR, "chat_logs", f"chat_{uid}_{now.strftime('%Y-%m-%d_%H-%M-%S')}.txt")
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("=== ИСТОРИЯ ДИАЛОГА ===\n")
            f.write(f"Пользователь ID: {uid} ({username})\n")
            f.write(f"Администратор ID: {admin_id}\n")
            f.write(f"Закрыто: {now.strftime('%d.%m.%Y в %H:%M:%S')}\n")
            f.write("========================\n\n")
            if not history:
                f.write("[Сообщений нет]\n")
            for sender, text, ts, mt in history:
                label = f"[{MEDIA_LABEL.get(mt, mt)}] " if mt else ""
                f.write(f"[{ts}] {sender}: {label}{text or ''}\n")
    except Exception as e:
        logging.error("Не удалось записать лог чата: %s", e)
        file_path = None
    with tx() as cur:
        cur.execute(
            "INSERT INTO closed_dialogs_archive (admin_id, user_id, username, closed_at, file_path) VALUES (%s, %s, %s, %s, %s)",
            (admin_id, uid, username, now.strftime("%Y-%m-%d %H:%M:%S"), file_path),
        )
        cur.execute("DELETE FROM message_logs WHERE user_id=%s", (uid,))
        cur.execute("UPDATE dialogs SET status='closed' WHERE user_id=%s", (uid,))
        cur.execute("DELETE FROM admin_sessions WHERE target_user_id=%s", (uid,))
        cur.execute("DELETE FROM ticket_waits WHERE user_id=%s", (uid,))
        cur.execute(
            "INSERT INTO admin_stats (admin_id, closed_count) VALUES (%s, 1) ON DUPLICATE KEY UPDATE closed_count = closed_count + 1",
            (admin_id,),
        )
    return uid


def log_admin_message(admin_id: int, uid: int, preview: str, log_text: str, mt, fid):
    with tx() as cur:
        cur.execute(
            "INSERT INTO message_logs (user_id, sender, text, media_type, file_id) VALUES (%s, %s, %s, %s, %s)",
            (uid, f"Админ #{admin_id}", log_text, mt, fid),
        )
        cur.execute("UPDATE dialogs SET last_message=%s WHERE user_id=%s", ("Вы: " + preview[:25], uid))


def ban_sync(target_id: int, reason: str):
    with tx() as cur:
        cur.execute(
            """INSERT INTO banned_users (user_id, banned_at, reason) VALUES (%s, NOW(), %s)
               ON DUPLICATE KEY UPDATE banned_at=NOW(), reason=VALUES(reason)""",
            (target_id, reason),
        )
        cur.execute("UPDATE dialogs SET status='closed' WHERE user_id=%s", (target_id,))
        cur.execute("DELETE FROM admin_sessions WHERE target_user_id=%s", (target_id,))
        cur.execute("DELETE FROM ticket_waits WHERE user_id=%s", (target_id,))


def user_state_sync(uid: int):
    banned = q("SELECT 1 FROM banned_users WHERE user_id=%s", (uid,), "one") is not None
    row = q("SELECT status FROM dialogs WHERE user_id=%s", (uid,), "one")
    return banned, (row[0] if row else None)


def upsert_admin_sync(uid: int, role: str, keep_existing: bool = False):
    if keep_existing:
        q("INSERT INTO admins (user_id, role) VALUES (%s, %s) ON DUPLICATE KEY UPDATE role = role", (uid, role))
    else:
        q("INSERT INTO admins (user_id, role) VALUES (%s, %s) ON DUPLICATE KEY UPDATE role = VALUES(role)", (uid, role))
    _role_cache.clear()


def remove_admin_sync(uid: int) -> bool:
    """Снять с должности: убираем доступ на сайт и возвращаем его тикет в очередь."""
    with tx() as cur:
        cur.execute("SELECT target_user_id FROM admin_sessions WHERE admin_id=%s", (uid,))
        s = cur.fetchone()
        cur.execute("DELETE FROM admins WHERE user_id=%s AND role != 'owner'", (uid,))
        removed = cur.rowcount > 0
        if removed:
            cur.execute("DELETE FROM admin_auth WHERE admin_id=%s", (uid,))
            cur.execute("DELETE FROM admin_devices WHERE admin_id=%s", (uid,))
            cur.execute("DELETE FROM admin_sessions WHERE admin_id=%s", (uid,))
            if s:
                cur.execute("UPDATE dialogs SET status='open' WHERE user_id=%s AND status='chatting'", (s[0],))
                cur.execute(
                    """INSERT INTO ticket_waits (user_id, waiting_since, taken_at, penalized) VALUES (%s, NOW(), NULL, 0)
                       ON DUPLICATE KEY UPDATE waiting_since=NOW(), taken_at=NULL, penalized=0""",
                    (s[0],),
                )
    _role_cache.clear()
    return removed


def demote_owner_sync(uid: int) -> bool:
    n = q("UPDATE admins SET role='admin' WHERE user_id=%s AND role='owner'", (uid,))
    _role_cache.clear()
    return n > 0


def login_taken_sync(login: str, target_id: int) -> bool:
    return q("SELECT 1 FROM admin_auth WHERE login=%s AND admin_id<>%s", (login, target_id), "one") is not None


def set_access_sync(target_id: int, login: str, pw: str, actor_id: int):
    """Логин и пароль для сайта. Хэш bcrypt, формат $2y$ - как у password_hash() в PHP."""
    h = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(10)).decode("ascii").replace("$2b$", "$2y$", 1)
    with tx() as cur:
        cur.execute("SELECT 1 FROM admin_auth WHERE login=%s AND admin_id<>%s", (login, target_id))
        if cur.fetchone():
            raise ValueError("login_taken")
        cur.execute("INSERT INTO admins (user_id, role) VALUES (%s, 'admin') ON DUPLICATE KEY UPDATE role = role", (target_id,))
        cur.execute(
            """INSERT INTO admin_auth (admin_id, login, pass_hash, created_by) VALUES (%s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE login=VALUES(login), pass_hash=VALUES(pass_hash), created_by=VALUES(created_by)""",
            (target_id, login, h, actor_id),
        )
        cur.execute("DELETE FROM admin_devices WHERE admin_id=%s", (target_id,))
    _role_cache.clear()


STAFF_SQL = """
SELECT a.user_id, a.role, a.created_at, u.login, u.last_login,
       COALESCE(r.is_resting, 0), COALESCE(s.closed_count, 0),
       (SELECT COUNT(*) FROM admin_penalties p WHERE p.admin_id = a.user_id),
       (SELECT COUNT(*) FROM admin_devices d WHERE d.admin_id = a.user_id)
FROM admins a
LEFT JOIN admin_auth u ON u.admin_id = a.user_id
LEFT JOIN admin_rest r ON r.admin_id = a.user_id
LEFT JOIN admin_stats s ON s.admin_id = a.user_id
{where}
ORDER BY (a.role = 'owner') DESC, a.created_at
"""


def staff_all_sync():
    return q(STAFF_SQL.format(where=""), (), "all")


def staff_one_sync(uid: int):
    rows = q(STAFF_SQL.format(where="WHERE a.user_id = %s"), (uid,), "all")
    return rows[0] if rows else None


def archive_sync(adm_id: int):
    now = dt.datetime.now()
    total = q("SELECT COUNT(*) FROM closed_dialogs_archive WHERE admin_id=%s", (adm_id,), "one")[0]
    today = q(
        "SELECT COUNT(*) FROM closed_dialogs_archive WHERE admin_id=%s AND (closed_at LIKE %s OR closed_at LIKE %s)",
        (adm_id, now.strftime("%Y-%m-%d") + "%", now.strftime("%d.%m.%Y") + "%"), "one",
    )[0]
    rows = q(
        "SELECT id, user_id, username, closed_at, file_path FROM closed_dialogs_archive WHERE admin_id=%s ORDER BY id DESC LIMIT 10",
        (adm_id,), "all",
    )
    return total, today, rows


# ==========================================================
# Имена сотрудников (из Telegram) с кэшем
# ==========================================================
_name_cache: dict = {}


async def tg_name(uid: int) -> str:
    if uid in _name_cache:
        return _name_cache[uid]
    name = ""
    try:
        c = await bot.get_chat(uid)
        name = c.full_name or ""
        if c.username:
            name = f"{name} (@{c.username})".strip()
    except Exception:
        pass
    _name_cache[uid] = name
    return name


# ==========================================================
# Бот
# ==========================================================
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
dp.message.filter(F.chat.type == "private")


class ReportStates(StatesGroup):
    waiting_for_report_text = State()


class Staff(StatesGroup):
    wait_id = State()
    wait_login = State()
    wait_password = State()


_notice_ts: dict = {}


# ---------------- Клавиатуры и экраны ----------------
async def user_start_kb(uid: int):
    b = InlineKeyboardBuilder()
    b.button(text="Написать в поддержку", callback_data="contact_support")
    b.button(text="Быстрые ответы (FAQ)", callback_data="faq_menu")
    row = await aq(
        """SELECT c.admin_id,
                  (SELECT 1 FROM submitted_reports r WHERE r.user_id = c.user_id AND r.admin_id = c.admin_id LIMIT 1)
           FROM closed_dialogs_archive c WHERE c.user_id=%s ORDER BY c.id DESC LIMIT 1""",
        (uid,), "one",
    )
    if row and row[0] and not row[1]:
        b.button(text="Пожаловаться на последний ответ", callback_data="report_last_admin")
    b.adjust(1)
    return b.as_markup()


async def render_panel(uid: int, role: str):
    rows = await aq(
        "SELECT user_id, username, first_name, last_message, dialog_token FROM dialogs WHERE status='open' ORDER BY user_id DESC LIMIT 30",
        (), "all",
    )
    b = InlineKeyboardBuilder()
    for u, un, fn, msg, tok in rows:
        name = un or fn or str(u)
        b.button(text=f"{name}: {msg or ''}"[:60], callback_data=f"chat_{u}_{tok or '0'}")
    b.button(text="Обновить список", callback_data="refresh_panel")
    b.button(text="Личный кабинет", callback_data="admin_profile")
    if role == "owner":
        b.button(text="Сотрудники", callback_data="staff_list")
        b.button(text="Общая статистика", callback_data="view_stats")
    pb = panel_button()
    if pb:
        b.add(pb)
    b.adjust(1)
    text = (
        "<b>Рабочая панель администратора</b>\n"
        + pre("Команды в чате:\n/leave - выйти, тикет вернётся в очередь\n/close - закрыть тикет\n/ban - заблокировать клиента\n/rest, /work - выходной / работа")
        + f"\nВ очереди: <b>{len(rows)}</b>"
        + ("\nВыберите диалог:" if rows else "\nОчередь пуста.")
    )
    return text, b.as_markup()


def faq_menu_kb():
    b = InlineKeyboardBuilder()
    for key, (title, _) in FAQ.items():
        b.button(text=title, callback_data=f"faq_{key}")
    b.button(text="Назад", callback_data="back_start")
    b.adjust(2)
    return b.as_markup()


# ---------------- Старт и меню клиента ----------------
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    if await get_role(uid):
        await message.answer("Панель администратора. Очередь тикетов: /panel", **H)
        return
    await message.answer(
        f"Здравствуйте, {esc(message.from_user.first_name)}.\n"
        "Это служба поддержки. Выберите раздел:",
        reply_markup=await user_start_kb(uid), **H,
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.")


@dp.callback_query(F.data == "back_start")
async def back_start(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await safe_edit(cb, "Выберите раздел:", await user_start_kb(cb.from_user.id))


@dp.callback_query(F.data == "contact_support")
async def contact_support(cb: types.CallbackQuery):
    await cb.answer()
    banned, status = await run(user_state_sync, cb.from_user.id)
    if banned:
        await cb.message.answer("Доступ к созданию обращений ограничен администратором.")
    elif status in ("open", "chatting"):
        await cb.message.answer("У вас уже есть активное обращение. Дождитесь ответа или допишите детали сюда.")
    else:
        await cb.message.answer("Отправьте сообщение или файл в этот чат, мы сразу передадим его специалистам.")


@dp.callback_query(F.data == "faq_menu")
async def faq_menu(cb: types.CallbackQuery):
    await cb.answer()
    await safe_edit(cb, "<b>Быстрые ответы</b>\nВыберите тему:", faq_menu_kb())


@dp.callback_query(F.data.startswith("faq_"))
async def faq_item(cb: types.CallbackQuery):
    key = cb.data[4:]
    if key not in FAQ:
        await cb.answer()
        return
    title, text = FAQ[key]
    b = InlineKeyboardBuilder()
    b.button(text="К списку вопросов", callback_data="faq_menu")
    b.button(text="Написать в поддержку", callback_data="contact_support")
    b.adjust(1)
    await cb.answer()
    await safe_edit(cb, f"<b>{esc(title)}</b>\n{quote(text)}", b.as_markup())


# ---------------- Жалобы ----------------
@dp.callback_query(F.data == "report_last_admin")
async def report_start(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    row = await aq(
        """SELECT c.admin_id, (SELECT 1 FROM submitted_reports r WHERE r.user_id=c.user_id AND r.admin_id=c.admin_id LIMIT 1)
           FROM closed_dialogs_archive c WHERE c.user_id=%s ORDER BY c.id DESC LIMIT 1""",
        (uid,), "one",
    )
    if not row or not row[0]:
        await cb.answer("История обращений не найдена.", show_alert=True)
        return
    if row[1]:
        await cb.answer("Вы уже отправили жалобу по этому обращению.", show_alert=True)
        return
    await cb.answer()
    await state.set_state(ReportStates.waiting_for_report_text)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="back_start")]])
    await cb.message.answer(
        "<b>Жалоба на работу поддержки</b>\nНапишите одним сообщением, с чем вы не согласны.", reply_markup=kb, **H
    )


@dp.message(ReportStates.waiting_for_report_text, F.text)
async def report_text(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await state.clear()
    row = await aq(
        "SELECT admin_id, file_path FROM closed_dialogs_archive WHERE user_id=%s ORDER BY id DESC LIMIT 1", (uid,), "one"
    )
    if not row or not row[0]:
        await message.answer("Не удалось найти последнее обращение.")
        return
    admin_id, path = row
    await aq("INSERT IGNORE INTO submitted_reports (user_id, admin_id) VALUES (%s, %s)", (uid, admin_id))
    owners = [r[0] for r in await aq("SELECT user_id FROM admins WHERE role='owner'", (), "all")] or [OWNER_ID]
    card = (
        "<b>Новая жалоба на администратора</b>\n"
        + pre(f"От: {message.from_user.full_name} (ID {uid})\nНа кого: администратор {admin_id}")
        + f"\n{quote(message.text)}"
    )
    sent = False
    for oid in owners:
        try:
            if path and os.path.isfile(path):
                await bot.send_document(oid, types.FSInputFile(path), caption=card[:1024], **H)
            else:
                await bot.send_message(oid, card, **H)
            sent = True
        except Exception as e:
            logging.warning("Жалоба не доставлена %s: %s", oid, e)
    await message.answer(
        "Жалоба отправлена руководству." if sent else "Не удалось доставить жалобу, попробуйте позже.",
        reply_markup=await user_start_kb(uid),
    )


# ---------------- Команды администратора ----------------
@dp.message(Command("panel"), RoleFilter())
async def cmd_panel(message: types.Message, state: FSMContext):
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    text, kb = await render_panel(message.from_user.id, await get_role(message.from_user.id))
    await message.answer(text, reply_markup=kb, **H)


@dp.callback_query(F.data.in_({"refresh_panel", "back_to_panel"}), RoleFilter())
async def refresh_panel(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await render_panel(cb.from_user.id, await get_role(cb.from_user.id))
    changed = await safe_edit(cb, text, kb)
    await cb.answer("Список обновлён" if changed else "Без изменений")


@dp.message(Command("rest"), RoleFilter())
async def cmd_rest(message: types.Message):
    row = await aq("SELECT is_resting FROM admin_rest WHERE admin_id=%s", (message.from_user.id,), "one")
    if row and int(row[0]) == 1:
        await message.answer("Вы уже в режиме выходного.")
        return
    await aq(
        "INSERT INTO admin_rest (admin_id, is_resting) VALUES (%s, 1) ON DUPLICATE KEY UPDATE is_resting = 1",
        (message.from_user.id,),
    )
    await message.answer(
        "<b>Режим выходного включён.</b>\nУведомления о новых обращениях приходить не будут. Вернуться: /work", **H
    )


@dp.message(Command("work"), RoleFilter())
async def cmd_work(message: types.Message):
    row = await aq("SELECT is_resting FROM admin_rest WHERE admin_id=%s", (message.from_user.id,), "one")
    if not row or int(row[0]) == 0:
        await message.answer("Вы уже в рабочем режиме.")
        return
    await aq("UPDATE admin_rest SET is_resting = 0 WHERE admin_id=%s", (message.from_user.id,))
    await message.answer("<b>Вы вернулись к работе.</b>\nУведомления о тикетах снова включены.", **H)


@dp.message(Command("ban"), RoleFilter())
async def cmd_ban(message: types.Message):
    uid = message.from_user.id
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) >= 2:
        if not parts[1].isdigit():
            await message.answer("ID должен состоять из цифр.")
            return
        target = int(parts[1])
        reason = parts[2] if len(parts) > 2 else "Нарушение правил общения с поддержкой"
    else:
        row = await aq("SELECT target_user_id FROM admin_sessions WHERE admin_id=%s", (uid,), "one")
        if not row:
            await message.answer("Формат: <code>/ban 123456789 причина</code>\nИли /ban внутри открытого чата.", **H)
            return
        target, reason = row[0], "Нарушение правил общения с поддержкой"
    if target == uid or await get_role(target):
        await message.answer("Этого пользователя заблокировать нельзя.")
        return
    await run(ban_sync, target, reason)
    await message.answer(f"<b>Пользователь заблокирован</b>\n{pre(f'ID: {target}')}Причина: {esc(reason)}", **H)
    try:
        await bot.send_message(target, f"<b>Доступ к поддержке ограничен.</b>\nПричина: {esc(reason)}", **H)
    except Exception:
        pass


@dp.message(Command("unban"), RoleFilter(owner=True))
async def cmd_unban(message: types.Message):
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Формат: <code>/unban 123456789</code>", **H)
        return
    target = int(parts[1])
    n = await aq("DELETE FROM banned_users WHERE user_id=%s", (target,))
    if not n:
        await message.answer("Этот пользователь не в чёрном списке.")
        return
    await message.answer(f"Пользователь <code>{target}</code> разблокирован.", **H)
    try:
        await bot.send_message(target, "Доступ к поддержке восстановлен.")
    except Exception:
        pass


# ---------------- Работа с тикетом ----------------
@dp.callback_query(F.data.startswith("chat_"), RoleFilter())
async def open_chat(cb: types.CallbackQuery):
    admin_id = cb.from_user.id
    try:
        _, uid_s, token = cb.data.split("_", 2)
        uid = int(uid_s)
    except ValueError:
        await cb.answer()
        return
    res = await run(claim_sync, admin_id, uid, token)
    if not res["ok"]:
        msg = {
            "rest": "Вы в режиме выходного. Введите /work, чтобы принимать чаты.",
            "stale": "Кнопка устарела: диалог уже обрабатывается или закрыт.",
            "busy": "Сначала выйдите из текущего чата (/leave) или закройте его (/close).",
        }[res["reason"]]
        await cb.answer(msg, show_alert=True)
        if res["reason"] == "stale":
            text, kb = await render_panel(admin_id, await get_role(admin_id))
            try:
                await cb.message.edit_text(text, reply_markup=kb, **H)
            except Exception:
                pass
        return
    lines = []
    for sender, text, mt in res["history"]:
        who = "Админ" if "Админ" in (sender or "") else "Клиент"
        body = (text or "").strip() or (f"[{MEDIA_LABEL.get(mt, mt)}]" if mt else "")
        lines.append(f"{who}: {body[:200]}")
    info = (
        f"<b>Вы в чате с пользователем</b> <code>{uid}</code>\n"
        + (pre("\n".join(lines)) if lines else "")
        + "\nСообщения пересылаются напрямую.\n/leave - выйти, /close - закрыть, /ban - заблокировать"
    )
    if res["late_min"]:
        info += f"\n\nТикет принят через {res['late_min']} мин (лимит {PENALTY_MINUTES}). Начислен штраф."
    await cb.message.answer(info, **H)
    await cb.answer()
    try:
        await bot.send_message(uid, "Администратор подключился к диалогу. Скоро последует ответ.")
    except Exception:
        pass


@dp.message(Command("leave"), RoleFilter())
async def cmd_leave(message: types.Message):
    uid = await run(leave_sync, message.from_user.id)
    if not uid:
        await message.answer("Вы сейчас не в чате.")
        return
    await message.answer("<b>Вы вышли из чата.</b>\nДиалог снова в очереди (/panel).", **H)
    try:
        await bot.send_message(uid, "Администратор временно покинул чат. Пожалуйста, ожидайте.")
    except Exception:
        pass


@dp.message(Command("close"), RoleFilter())
async def cmd_close(message: types.Message):
    uid = await run(close_sync, message.from_user.id)
    if not uid:
        await message.answer("Вы сейчас не в чате.")
        return
    await message.answer("Обращение закрыто.")
    try:
        await bot.send_message(
            uid, "Ваше обращение закрыто. Если вопрос остался, просто напишите нам снова.",
            reply_markup=await user_start_kb(uid),
        )
    except Exception:
        pass


# ---------------- Личный кабинет и статистика ----------------
@dp.callback_query(F.data == "admin_profile", RoleFilter())
async def admin_profile(cb: types.CallbackQuery):
    uid = cb.from_user.id
    row = await run(staff_one_sync, uid)
    await cb.answer()
    if not row:
        return
    _, role, created, login, last_login, resting, closed, pens, _ = row
    days = max(0, (dt.datetime.now() - created).days) if isinstance(created, dt.datetime) else 0
    text = (
        "<b>Личный кабинет</b>\n"
        + pre(
            f"ID:               {uid}\n"
            f"Роль:             {ROLE_RU.get(role, role)}\n"
            f"На должности:     {days_text(days)}\n"
            f"Закрыто тикетов:  {closed}\n"
            f"Штрафов:          {pens}\n"
            f"Логин на сайте:   {login or 'не задан'}"
        )
        + ("\nЛогин и пароль выдаёт главный администратор." if not login else "")
    )
    b = InlineKeyboardBuilder()
    pb = panel_button()
    if pb:
        b.add(pb)
    b.button(text="Назад", callback_data="back_to_panel")
    b.adjust(1)
    await safe_edit(cb, text, b.as_markup())


@dp.callback_query(F.data == "view_stats", RoleFilter(owner=True))
async def view_stats(cb: types.CallbackQuery):
    rows = await aq("SELECT admin_id, closed_count FROM admin_stats ORDER BY closed_count DESC", (), "all")
    await cb.answer()
    b = InlineKeyboardBuilder()
    for adm, cnt in rows:
        b.button(text=f"Админ {adm}: {cnt} закрыто", callback_data=f"arch_adm_{adm}")
    b.button(text="Назад", callback_data="back_to_panel")
    b.adjust(1)
    await safe_edit(cb, "<b>Общая статистика</b>\n" + ("Выберите администратора:" if rows else "Данных пока нет."), b.as_markup())


@dp.callback_query(F.data.regexp(r"^arch_adm_\d+$"), RoleFilter(owner=True))
async def arch_admin(cb: types.CallbackQuery):
    adm = int(cb.data.split("_")[2])
    total, today, rows = await run(archive_sync, adm)
    await cb.answer()
    b = InlineKeyboardBuilder()
    lines = []
    for i, (aid, u, name, closed_at, path) in enumerate(rows, 1):
        lines.append(f"{i}. {u} ({name or '-'}) {closed_at or ''}")
        if path and i <= 5:
            b.button(text=f"Лог: {name or u}"[:60], callback_data=f"getfile_{aid}")
    b.button(text="Назад", callback_data="view_stats")
    b.adjust(1)
    text = (
        f"<b>Администратор</b> <code>{adm}</code>\n"
        + pre(f"Сегодня:    {today}\nВсего:      {total}")
        + ("\n<b>Последние закрытые:</b>\n" + pre("\n".join(lines)) if lines else "\nЗакрытых диалогов нет.")
    )
    await safe_edit(cb, text[:4000], b.as_markup())


@dp.callback_query(F.data.regexp(r"^getfile_\d+$"), RoleFilter(owner=True))
async def get_file(cb: types.CallbackQuery):
    row = await aq("SELECT file_path, user_id FROM closed_dialogs_archive WHERE id=%s", (int(cb.data.split("_")[1]),), "one")
    if not row or not row[0] or not os.path.isfile(row[0]):
        await cb.answer("Файл лога недоступен.", show_alert=True)
        return
    await cb.answer("Отправляю лог")
    await cb.message.answer_document(types.FSInputFile(row[0]), caption=f"История чата {row[1]}")


# ==========================================================
# Сотрудники (только для главных админов)
# ==========================================================
async def render_staff_list():
    rows = await run(staff_all_sync)
    names = await asyncio.gather(*(tg_name(r[0]) for r in rows))
    b = InlineKeyboardBuilder()
    for r, name in zip(rows, names):
        uid, role, _, login, _, resting, *_ = r
        tags = ["владелец" if role == "owner" else "админ", "отдых" if resting else "работает"]
        if not login:
            tags.append("без входа")
        b.button(text=f"{name or uid} | {', '.join(tags)}"[:60], callback_data=f"staff_card_{uid}")
    b.button(text="Добавить сотрудника", callback_data="staff_new")
    b.button(text="Назад", callback_data="back_to_panel")
    b.adjust(1)
    return f"<b>Сотрудники</b> ({len(rows)})\nВыберите сотрудника или добавьте нового.", b.as_markup()


@dp.callback_query(F.data.in_({"staff_list", "manage_admins"}), RoleFilter(owner=True))
async def staff_list(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    text, kb = await render_staff_list()
    await safe_edit(cb, text, kb)


async def render_staff_card(actor_id: int, uid: int):
    row = await run(staff_one_sync, uid)
    if not row:
        return None, None
    _, role, created, login, last_login, resting, closed, pens, devices = row
    name = await tg_name(uid)
    days = max(0, (dt.datetime.now() - created).days) if isinstance(created, dt.datetime) else 0
    text = "<b>Карточка сотрудника</b>\n" + pre(
        f"ID:               {uid}\n"
        f"Имя:              {name or 'неизвестно'}\n"
        f"Роль:             {ROLE_RU.get(role, role)}\n"
        f"Статус:           {'выходной' if resting else 'работает'}\n"
        f"Логин на сайте:   {login or 'не задан'}\n"
        f"Последний вход:   {fmt_dt(last_login)}\n"
        f"Устройств на сайте: {devices}\n"
        f"Закрыто тикетов:  {closed}\n"
        f"Штрафов:          {pens}\n"
        f"На должности:     {days_text(days)}"
    ) + "\nПароль хранится только в виде хэша, увидеть его нельзя. Можно задать новый."
    b = InlineKeyboardBuilder()
    if can_manage(actor_id, uid, role):
        b.button(text="Сменить логин и пароль" if login else "Задать логин и пароль", callback_data=f"staff_access_{uid}")
        if devices:
            b.button(text="Отключить все устройства", callback_data=f"staff_kick_{uid}")
        if uid != OWNER_ID:
            b.button(text="Снять с должности", callback_data=f"staff_fire_{uid}")
    b.button(text="К списку", callback_data="staff_list")
    b.adjust(1)
    return text, b.as_markup()


@dp.callback_query(F.data.regexp(r"^staff_card_\d+$"), RoleFilter(owner=True))
async def staff_card(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    uid = int(cb.data.split("_")[2])
    text, kb = await render_staff_card(cb.from_user.id, uid)
    await cb.answer()
    if not text:
        await safe_edit(cb, "Сотрудник не найден.", InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="К списку", callback_data="staff_list")]]))
        return
    await safe_edit(cb, text, kb)


async def _guard_manage(cb: types.CallbackQuery, uid: int) -> bool:
    role = await get_role(uid)
    if role is None and uid != OWNER_ID:
        await cb.answer("Сотрудник не найден.", show_alert=True)
        return False
    if not can_manage(cb.from_user.id, uid, role):
        await cb.answer("Недостаточно прав для этого сотрудника.", show_alert=True)
        return False
    return True


@dp.callback_query(F.data.regexp(r"^staff_kick_\d+$"), RoleFilter(owner=True))
async def staff_kick(cb: types.CallbackQuery):
    uid = int(cb.data.split("_")[2])
    if not await _guard_manage(cb, uid):
        return
    n = await aq("DELETE FROM admin_devices WHERE admin_id=%s", (uid,))
    await cb.answer(f"Отключено устройств: {n}", show_alert=True)
    text, kb = await render_staff_card(cb.from_user.id, uid)
    if text:
        await safe_edit(cb, text, kb)


@dp.callback_query(F.data.regexp(r"^staff_fire_\d+$"), RoleFilter(owner=True))
async def staff_fire(cb: types.CallbackQuery):
    uid = int(cb.data.split("_")[2])
    if not await _guard_manage(cb, uid):
        return
    b = InlineKeyboardBuilder()
    b.button(text="Да, снять", callback_data=f"staff_fireyes_{uid}")
    b.button(text="Отмена", callback_data=f"staff_card_{uid}")
    b.adjust(2)
    await cb.answer()
    await safe_edit(cb, f"Снять сотрудника <code>{uid}</code> с должности?\nДоступ на сайт будет закрыт, его тикет вернётся в очередь.", b.as_markup())


@dp.callback_query(F.data.regexp(r"^staff_fireyes_\d+$"), RoleFilter(owner=True))
async def staff_fire_yes(cb: types.CallbackQuery):
    uid = int(cb.data.split("_")[2])
    if not await _guard_manage(cb, uid) or uid == OWNER_ID:
        return
    ok = await run(remove_admin_sync, uid)
    await cb.answer("Сотрудник снят" if ok else "Не удалось снять", show_alert=True)
    if ok:
        try:
            await bot.send_message(uid, "Ваши полномочия администратора аннулированы.")
        except Exception:
            pass
    text, kb = await render_staff_list()
    await safe_edit(cb, text, kb)


# --- выдача логина и пароля для сайта ---
@dp.callback_query(F.data == "staff_new", RoleFilter(owner=True))
async def staff_new(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(Staff.wait_id)
    await cb.answer()
    await cb.message.answer(
        "<b>Новый сотрудник</b>\nОтправьте его Telegram ID (только цифры).\nОтмена: /cancel", **H
    )


@dp.message(Staff.wait_id, F.text, RoleFilter(owner=True))
async def staff_got_id(message: types.Message, state: FSMContext):
    t = message.text.strip()
    if not t.isdigit():
        await message.answer("ID должен состоять из цифр. Отправьте ещё раз или /cancel.")
        return
    target = int(t)
    role = await get_role(target)
    if not can_manage(message.from_user.id, target, role):
        await message.answer("Этого сотрудника вы изменять не можете.")
        await state.clear()
        return
    is_new = role is None
    await run(upsert_admin_sync, target, "admin", True)
    await state.update_data(target=target, is_new=is_new)
    await state.set_state(Staff.wait_login)
    await message.answer(
        f"Сотрудник <code>{target}</code> {'добавлен' if is_new else 'уже в системе'}.\n"
        "Введите логин для сайта: 3-32 символа, латиница, цифры, точка, _ и -.\nОтмена: /cancel", **H
    )


@dp.callback_query(F.data.regexp(r"^staff_access_\d+$"), RoleFilter(owner=True))
async def staff_access(cb: types.CallbackQuery, state: FSMContext):
    uid = int(cb.data.split("_")[2])
    if not await _guard_manage(cb, uid):
        return
    await state.set_state(Staff.wait_login)
    await state.update_data(target=uid, is_new=False)
    await cb.answer()
    await cb.message.answer(
        f"<b>Доступ на сайт для</b> <code>{uid}</code>\n"
        "Введите логин: 3-32 символа, латиница, цифры, точка, _ и -.\nОтмена: /cancel", **H
    )


@dp.message(Staff.wait_login, F.text, RoleFilter(owner=True))
async def staff_got_login(message: types.Message, state: FSMContext):
    login = message.text.strip().lower()
    if not LOGIN_RE.match(login):
        await message.answer("Логин: 3-32 символа, латиница, цифры, точка, _ и -. Попробуйте ещё раз.")
        return
    data = await state.get_data()
    if await run(login_taken_sync, login, data["target"]):
        await message.answer("Такой логин уже занят. Введите другой.")
        return
    await state.update_data(login=login)
    await state.set_state(Staff.wait_password)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Сгенерировать пароль", callback_data="staff_gen")]])
    await message.answer(
        f"Логин: <code>{esc(login)}</code>\nОтправьте пароль (8-72 символа) или сгенерируйте.\n"
        "Сообщение с вашим паролем будет сразу удалено.", reply_markup=kb, **H,
    )


async def _apply_access(chat_id: int, actor_id: int, state: FSMContext, pw: str):
    data = await state.get_data()
    target, login = data.get("target"), data.get("login")
    if not target or not login:
        await state.clear()
        await bot.send_message(chat_id, "Сессия настройки устарела. Начните заново.")
        return
    try:
        await run(set_access_sync, target, login, pw, actor_id)
    except ValueError:
        await bot.send_message(chat_id, "Такой логин уже занят. Начните заново и выберите другой.")
        await state.clear()
        return
    except mysql_errors.IntegrityError:
        await bot.send_message(chat_id, "Такой логин уже занят. Начните заново и выберите другой.")
        await state.clear()
        return
    is_new = data.get("is_new")
    await state.clear()
    text = (
        "<b>Доступ на сайт задан</b>\n"
        f"Сотрудник: <code>{target}</code>\nЛогин: <code>{esc(login)}</code>\nПароль: <code>{esc(pw)}</code>\n"
        "Передайте пароль сотруднику лично. Старые устройства отключены."
        + (f"\nСсылка: {esc(PANEL_URL)}" if PANEL_URL else "")
    )
    await bot.send_message(chat_id, text, **H)
    # Сотрудника уведомляем без пароля
    b = InlineKeyboardBuilder()
    pb = panel_button()
    if pb:
        b.add(pb)
    try:
        await bot.send_message(
            target,
            ("<b>Вы назначены администратором поддержки.</b>\n" if is_new else "<b>Ваши данные для входа в веб-панель обновлены.</b>\n")
            + f"Логин: <code>{esc(login)}</code>\nПароль вам передаст главный администратор.\nОчередь тикетов в боте: /panel",
            reply_markup=b.as_markup() if pb else None, **H,
        )
    except Exception:
        pass


@dp.message(Staff.wait_password, F.text, RoleFilter(owner=True))
async def staff_got_password(message: types.Message, state: FSMContext):
    pw = message.text
    try:
        await message.delete()
    except Exception:
        pass
    n = len(pw.encode("utf-8"))
    if n < 8 or n > 72:
        await message.answer("Пароль: от 8 до 72 символов. Отправьте другой или нажмите «Сгенерировать пароль».")
        return
    await _apply_access(message.chat.id, message.from_user.id, state, pw)


@dp.callback_query(F.data == "staff_gen", Staff.wait_password, RoleFilter(owner=True))
async def staff_generate(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    pw = "".join(secrets.choice(PW_ALPHABET) for _ in range(12))
    await _apply_access(cb.message.chat.id, cb.from_user.id, state, pw)


# ---------------- Текстовые команды владельца ----------------
async def _id_arg(message: types.Message, usage: str):
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer(f"Формат: <code>{usage}</code>", **H)
        return None
    return int(parts[1])


@dp.message(Command("addadmin"), RoleFilter(owner=True))
async def cmd_addadmin(message: types.Message):
    target = await _id_arg(message, "/addadmin 123456789")
    if target is None:
        return
    if not can_manage(message.from_user.id, target, await get_role(target)):
        await message.answer("Этого сотрудника вы изменять не можете.")
        return
    await run(upsert_admin_sync, target, "admin", True)
    await message.answer(
        f"Пользователь <code>{target}</code> добавлен как администратор.\nЛогин и пароль для сайта: кнопка «Сотрудники» в /panel.", **H
    )
    try:
        await bot.send_message(target, "Вы назначены администратором поддержки. Очередь тикетов: /panel")
    except Exception:
        pass


@dp.message(Command("deladmin"), RoleFilter(owner=True))
async def cmd_deladmin(message: types.Message):
    target = await _id_arg(message, "/deladmin 123456789")
    if target is None:
        return
    if not can_manage(message.from_user.id, target, await get_role(target)) or target == OWNER_ID:
        await message.answer("Этого сотрудника снять нельзя.")
        return
    if await run(remove_admin_sync, target):
        await message.answer(f"Пользователь <code>{target}</code> снят с должности.", **H)
        try:
            await bot.send_message(target, "Ваши полномочия администратора аннулированы.")
        except Exception:
            pass
    else:
        await message.answer("Такого администратора нет.")


@dp.message(Command("addowner"), RoleFilter(owner=True))
async def cmd_addowner(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return
    target = await _id_arg(message, "/addowner 123456789")
    if target is None:
        return
    await run(upsert_admin_sync, target, "owner")
    await message.answer(f"Пользователь <code>{target}</code> назначен главным администратором.", **H)
    try:
        await bot.send_message(target, "Вы назначены главным администратором поддержки.")
    except Exception:
        pass


@dp.message(Command("delowner"), RoleFilter(owner=True))
async def cmd_delowner(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return
    target = await _id_arg(message, "/delowner 123456789")
    if target is None:
        return
    if target == OWNER_ID:
        await message.answer("Создателя понизить нельзя.")
        return
    if await run(demote_owner_sync, target):
        await message.answer(f"Пользователь <code>{target}</code> переведён в обычные администраторы.", **H)
        try:
            await bot.send_message(target, "Ваша роль изменена: вы обычный администратор.")
        except Exception:
            pass
    else:
        await message.answer("Среди главных администраторов такого ID нет.")


# ==========================================================
# Сообщения: админ в чате / клиент
# ==========================================================
@dp.message(RoleFilter())
async def admin_message(message: types.Message):
    uid = message.from_user.id
    if message.text and message.text.startswith("/"):
        return
    row = await aq("SELECT target_user_id FROM admin_sessions WHERE admin_id=%s", (uid,), "one")
    if not row:
        await message.answer("Вы не в чате. Откройте /panel и выберите диалог.")
        return
    target = row[0]
    try:
        await message.copy_to(chat_id=target)
    except TelegramForbiddenError:
        await message.reply("Клиент заблокировал бота, доставить сообщение нельзя.")
        return
    except Exception as e:
        await message.reply(f"Ошибка доставки: {esc(e)}", **H)
        return
    preview, log_text, mt, fid = describe(message)
    try:
        await run(log_admin_message, uid, target, preview, log_text, mt, fid)
    except Exception as e:
        logging.error("Не удалось записать сообщение админа: %s", e)


async def notify_admins(targets, uid, token, card):
    b = InlineKeyboardBuilder()
    b.button(text="Взять в работу", callback_data=f"chat_{uid}_{token}")
    pb = panel_button()
    if pb:
        b.add(pb)
    b.adjust(1)
    markup = b.as_markup()

    async def one(a):
        try:
            await bot.send_message(int(a), card, reply_markup=markup, **H)
        except Exception as e:
            logging.warning("Уведомление %s не доставлено: %s", a, e)

    await asyncio.gather(*(one(a) for a in targets))


@dp.message()
async def user_message(message: types.Message):
    uid = message.from_user.id
    if message.text and message.text.startswith("/"):
        await message.answer("Выберите раздел:", reply_markup=await user_start_kb(uid))
        return
    preview, log_text, mt, fid = describe(message)
    username = f"@{message.from_user.username}" if message.from_user.username else "нет"
    first_name = message.from_user.first_name or ""

    res = await run(process_user_message, uid, username, first_name, preview, log_text, mt, fid)
    kind = res["kind"]

    if kind == "banned":
        await message.answer("Доступ к поддержке ограничен администратором.")
    elif kind == "chatting":
        if res.get("holder"):
            try:
                await message.copy_to(chat_id=res["holder"])
            except Exception:
                pass
    elif kind == "queued":
        now = time.monotonic()
        if now - _notice_ts.get(uid, 0) > 60:
            _notice_ts[uid] = now
            await message.answer("Дополнение добавлено к вашему обращению. Ожидайте ответа администратора.")
    else:  # new
        # Сначала быстрый ответ клиенту, потом уведомления админам в фоне
        await message.answer("Обращение зарегистрировано. Ожидайте ответа администратора.")
        card = (
            "<b>Новое обращение</b>\n"
            + pre(f"Пользователь: {first_name}\nЮзернейм:    {username}\nTelegram ID: {uid}\nТип:         {message.content_type}")
            + f"\n<b>Сообщение:</b>\n{quote(preview or '[без текста]')}"
        )
        asyncio.create_task(notify_admins(res["targets"], uid, res["token"], card))


# ==========================================================
# Запуск
# ==========================================================
async def main():
    await asyncio.to_thread(init_db)
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())








  

