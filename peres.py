import asyncio
import logging
import sqlite3
import secrets  # Для генерации уникальных токенов диалогов
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
class ReportStates(StatesGroup):
    waiting_for_report_text = State()  # Ожидание текста жалобы от пользователя


import os
os.makedirs("chat_logs", exist_ok=True)


class AdminManagement(StatesGroup):
    waiting_for_admin_id = State() # Состояние ожидания ввода ID


# --- НАСТРОЙКИ ---
TOKEN = "8778414676:AAHWdX12JWXv5FjKvGb8F83WziNuXh3ZFuI"
OWNER_ID = 8759913724
DB_FILE = "bot_database.db"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# --- ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ---
import datetime as dt
import time

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        # Добавлена колонка created_at (дата назначения)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                role TEXT DEFAULT 'admin',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Таблица диалогов
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dialogs (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_message TEXT,
                status TEXT DEFAULT 'open',
                dialog_token TEXT
            )
        """)
        # Таблица текущих сессий админов
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_sessions (
                admin_id INTEGER PRIMARY KEY,
                target_user_id INTEGER
            )
        """)
        # Таблица статистики админов
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_stats (
                admin_id INTEGER PRIMARY KEY,
                closed_count INTEGER DEFAULT 0
            )
        """)
        
        # Защита: Проверяем, есть ли колонка created_at (на случай если таблица уже существовала)
        cursor.execute("PRAGMA table_info(admins)")
        columns = [column[1] for column in cursor.fetchall()]
        if 'created_at' not in columns:
            cursor.execute("ALTER TABLE admins ADD COLUMN created_at TEXT DEFAULT CURRENT_TIMESTAMP")
            
        # Всегда добавляем создателя в БД как супер-админа
        cursor.execute("""
            INSERT INTO admins (user_id, role, created_at) 
            VALUES (?, 'owner', datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET role = 'owner'
        """, (OWNER_ID,))
        conn.commit()


        # Таблица для хранения сообщений текущих диалогов (Новая)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS message_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                sender TEXT, -- 'user' или 'admin' (или имя/ID админа)
                text TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Таблица архива закрытых диалогов для скачивания файлов (Новая)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS closed_dialogs_archive (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                user_id INTEGER,
                username TEXT,
                closed_at TEXT,
                file_path TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY,
                banned_at TEXT,
                reason TEXT
            )
        """)



init_db()



# --- ФУНКЦИИ ПРОВЕРКИ И УПРАВЛЕНИЯ РОЛЯМИ ---
def is_admin(user_id: int) -> bool:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT role FROM admins WHERE user_id = ?", (user_id,))
        return cursor.fetchone() is not None

def is_owner(user_id: int) -> bool:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT role FROM admins WHERE user_id = ? AND role = 'owner'", (user_id,))
        return cursor.fetchone() is not None

def add_admin_to_db(user_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        # Записываем текущую дату назначения
        cursor.execute("""
            INSERT INTO admins (user_id, role, created_at) 
            VALUES (?, 'admin', datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET role = 'admin'
        """, (user_id,))
        conn.commit()


def remove_admin_from_db(user_id: int) -> bool:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM admins WHERE user_id = ? AND role != 'owner'", (user_id,))
        conn.commit()
        return cursor.rowcount > 0

def get_all_admins():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM admins WHERE role = 'admin'")
        return [row[0] for row in cursor.fetchall()]


# --- ФУНКЦИИ СТАТИСТИКИ (НОВЫЕ) ---
def increment_admin_stat(admin_id: int):
    """Увеличивает счетчик закрытых диалогов админа на 1"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO admin_stats (admin_id, closed_count)
            VALUES (?, 1)
            ON CONFLICT(admin_id) DO UPDATE SET closed_count = closed_count + 1
        """, (admin_id,))
        conn.commit()

def get_admin_stats():
    """Возвращает статистику всех админов"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT admin_id, closed_count FROM admin_stats")
        return cursor.fetchall()


# --- ФУНКЦИИ УПРАВЛЕНИЯ СЕССИЯМИ И ДИАЛОГАМИ ---
def get_dialog_status(user_id: int) -> str | None:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM dialogs WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        return result[0] if result else None

def get_dialog_token(user_id: int) -> str | None:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT dialog_token FROM dialogs WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        return result[0] if result else None

def save_or_open_dialog(user_id: int, username: str, first_name: str, text: str):
    """Открывает диалог и генерирует уникальный токен, если статус closed или диалог новый"""
    current_status = get_dialog_status(user_id)
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        
        # Если диалог создается заново, генерируем новый секретный токен безопасности
        if current_status is None or current_status == 'closed':
            new_token = secrets.token_hex(4) # Создаст короткий токен вроде 'a1b2c3d4'
            cursor.execute("""
                INSERT INTO dialogs (user_id, username, first_name, last_message, status, dialog_token)
                VALUES (?, ?, ?, ?, 'open', ?)
                ON CONFLICT(user_id) DO UPDATE SET last_message = excluded.last_message, status = 'open', dialog_token = ?
            """, (user_id, username, first_name, text[:50], new_token, new_token))
        else:
            # Если диалог уже открыт, просто обновляем последнее сообщение, не меняя токен
            cursor.execute("""
                UPDATE dialogs SET last_message = ? WHERE user_id = ?
            """, (text[:50], user_id))
        conn.commit()

def update_dialog_status(user_id: int, new_status: str):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE dialogs SET status = ? WHERE user_id = ?", (new_status, user_id))
        conn.commit()

def get_active_dialogs():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, username, first_name, last_message, dialog_token FROM dialogs WHERE status = 'open'")
        return cursor.fetchall()

def set_admin_session(admin_id: int, target_user_id: int | None):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        if target_user_id is None:
            cursor.execute("DELETE FROM admin_sessions WHERE admin_id = ?", (admin_id,))
        else:
            cursor.execute("""
                INSERT INTO admin_sessions (admin_id, target_user_id)
                VALUES (?, ?)
                ON CONFLICT(admin_id) DO UPDATE SET target_user_id = excluded.target_user_id
            """, (admin_id, target_user_id))
        conn.commit()

def get_admin_session(admin_id: int) -> int | None:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT target_user_id FROM admin_sessions WHERE admin_id = ?", (admin_id,))
        result = cursor.fetchone()
        return result[0] if result else None

def get_admin_by_target_user(user_id: int) -> int | None:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT admin_id FROM admin_sessions WHERE target_user_id = ?", (user_id,))
        result = cursor.fetchone()
        return result[0] if result else None
        

def add_owner_to_db(user_id: int):
    """Назначает пользователя Главным админом (owner)"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        # Если пользователь уже был обычным админом, REPLACE обновит его роль до 'owner'
        cursor.execute("""
            INSERT OR REPLACE INTO admins (user_id, role) 
            VALUES (?, 'owner')
        """, (user_id,))
        conn.commit()

def demote_owner_in_db(user_id: int) -> bool:
    """Понижает Главного админа до обычного админа. Возвращает True, если успешно."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        # Меняем роль обратно на 'admin'
        cursor.execute("UPDATE admins SET role = 'admin' WHERE user_id = ? AND role = 'owner'", (user_id,))
        conn.commit()
        return cursor.rowcount > 0


def get_admin_profile_data(admin_id: int):
    """Возвращает (роль, дата_создания, кол-во_закрытых_тикетов)"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        # Получаем роль и дату назначения
        cursor.execute("SELECT role, created_at FROM admins WHERE user_id = ?", (admin_id,))
        admin_info = cursor.fetchone()
        
        # Получаем статистику закрытых тикетов
        cursor.execute("SELECT closed_count FROM admin_stats WHERE admin_id = ?", (admin_id,))
        stats_info = cursor.fetchone()
        
        role = admin_info[0] if admin_info else "Неизвестно"
        created_at = admin_info[1] if admin_info else None
        closed_count = stats_info[0] if stats_info else 0
        
        # Вычисляем сколько дней админ на должности
        days_on_duty = 0
        if created_at:
            try:
                # В SQLite datetime('now') сохраняет в формате YYYY-MM-DD HH:MM:SS
                parsed_date = dt.datetime.strptime(created_at.split(".")[0], "%Y-%m-%d %H:%M:%S")
                delta = dt.datetime.utcnow() - parsed_date
                days_on_duty = max(0, delta.days) # Исключаем отрицательные значения из-за разницы часовых поясов
            except Exception:
                days_on_duty = 0
                
        return role, days_on_duty, closed_count





def ban_user_in_db(user_id: int, reason: str = "Спам/неадекватное поведение"):
    """Добавляет пользователя в черный список"""
    now_str = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO banned_users (user_id, banned_at, reason) VALUES (?, ?, ?)",
            (user_id, now_str, reason)
        )
        conn.commit()

def is_user_banned(user_id: int) -> bool:
    """Проверяет, находится ли пользователь в бане"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,))
            return cursor.fetchone() is not None
    except Exception:
        return False

def is_user_owner(user_id: int) -> bool:
    """Проверяет, является ли пользователь Главным Администратором (owner)"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            # Проверяем роль в вашей таблице admins
            cursor.execute("SELECT 1 FROM admins WHERE user_id = ? AND role = 'owner'", (user_id,))
            return cursor.fetchone() is not None
    except Exception as e:
        logging.error(f"Ошибка проверки роли owner: {e}")
        return False


def unban_user_in_db(user_id: int):
    """Удаляет пользователя из черного списка"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
        conn.commit()







# --- КЛАВИАТУРЫ ---
def get_user_start_kb(user_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="✍️ Написать в тех. поддержку", callback_data="contact_support")
    builder.button(text="❓ Часто задаваемые вопросы (FAQ)", callback_data="faq_menu")
    
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            # Берем последнего обслуживавшего админа
            cursor.execute("SELECT admin_id FROM closed_dialogs_archive WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,))
            res = cursor.fetchone()
            
            if res:
                # ИСПРАВЛЕНО: достаем именно число ID из кортежа res[0]
                last_admin_id = res[0]
                
                # Проверяем, использован ли единственный шанс жалобы на этого админа
                cursor.execute("SELECT 1 FROM submitted_reports WHERE user_id = ? AND admin_id = ?", (user_id, last_admin_id))
                already_reported = cursor.fetchone() is not None
                
                # Кнопка доступна, если жалоба еще ни разу не отправлялась
                if not already_reported:
                    builder.button(text="⚠️ Пожаловаться на прошлый ответ", callback_data="report_last_admin")
    except Exception as e:
        logging.error(f"Ошибка генерации клавиатуры старта: {e}")
        
    builder.adjust(1)
    return builder.as_markup()




def get_admin_panel_kb(user_id: int):
    builder = InlineKeyboardBuilder()
    dialogs = get_active_dialogs()

    for uid, username, name, msg, token in dialogs:
        display_name = username if username else name
        builder.button(text=f"💬 {display_name}: {msg}", callback_data=f"chat_{uid}_{token}")

    builder.button(text="🔄 Обновить список", callback_data="refresh_panel")
    # Новая кнопка Личного Кабинета, доступная ВСЕМ админам
    builder.button(text="👤 Личный кабинет", callback_data="admin_profile")
    
    if is_owner(user_id):
        builder.button(text="👑 Управление админами", callback_data="manage_admins")
        builder.button(text="📊 Общая статистика", callback_data="view_stats")
        
    builder.adjust(1)
    return builder.as_markup()



# --- ХЕНДЛЕРЫ ОБРАБОТКИ СТАРТА / МЕНЮ ПОЛЬЗОВАТЕЛЯ ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    uid = message.from_user.id
    if is_admin(uid):
        await message.answer("👋 Приветствуем в панели администратора! Используйте команду /panel для просмотра очереди.", parse_mode="Markdown")
    else:
        await message.answer(
            f"👋 Здравствуйте, {message.from_user.first_name}!\n"
            f"Добро пожаловать в нашего бота поддержки. Выберите интересующий вас раздел:",
            # ИСПРАВЛЕНО: передаем uid для проверки истории
            reply_markup=get_user_start_kb(uid) 
        )



@dp.callback_query(F.data == "contact_support")
async def contact_support_callback(callback: types.CallbackQuery):
    uid = callback.from_user.id
    
    # ⛔️ ЗАПРЕТ ДЛЯ ЧС: забаненный пользователь не может писать новые обращения
    if is_user_banned(uid):
        await callback.answer("🔒 Ваш доступ к созданию новых обращений заблокирован Администратором.", show_alert=True)
        return

    status = get_dialog_status(uid)
    if status in ["open", "chatting"]:
        await callback.message.answer("⏳ У вас уже есть активный вопрос в разработке. Пожалуйста, ожидайте ответа.")
    else:
        await callback.message.answer("📥 Пожалуйста, отправьте ваше сообщение или файл в этот чат, и мы сразу передадим его агентам техподдержки.")
    await callback.answer()





@dp.callback_query(F.data == "faq_menu")
async def faq_menu_callback(callback: types.CallbackQuery):
    faq_text = (
        "❓ <b>Часто задаваемые вопросы (FAQ) Sonata VPN</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        
        "🔹 <b>1. Как долго ждать ответ поддержки?</b>\n"
        "🔸 <i>Ответ:</i> Обычно администраторы отвечают в течение 10-15 минут.\n\n"
        
        "🔹 <b>2. Что делать, если VPN не подключается?</b>\n"
        "🔸 <i>Ответ:</i> Попробуйте сменить сервер в приложении или обновить подписку. "
        "Если не помогает, зайдите в настройки приложения и смените протокол (например, на gRPC).\n\n"
        
        "🔹 <b>3. Можно ли отправлять медиафайлы в этот чат?</b>\n"
        "🔸 <i>Ответ:</i> Да, вы можете присылать скриншоты ошибок, документы и голосовые сообщения.\n\n"
        
        "🔹 <b>4. Как настроить VPN на телевизоре (Smart TV)?</b>\n"
        "🔸 <i>Ответ:</i> Для Android TV скачайте приложение <b>Karing</b> или <b>NekoBox</b> из Google Play и отсканируйте ваш QR-код. Для Apple TV используйте <b>Streisand</b>.\n\n"
        
        "🔹 <b>5. Как обновить сервера, если пропал интернет?</b>\n"
        "🔸 <i>Ответ:</i> В приложении нажмите три точки рядом с названием нашей подписки и выберите <b>«Обновить подписку» (Update)</b>.\n\n"
        
        "<pre>💡 Список вопросов и ответов регулярно обновляется. Если вашей проблемы нет в списке — нажмите кнопку «Написать в тех. поддержку».</pre>"
    )
    
    # Отправляем красивый HTML-текст
    await callback.message.answer(faq_text, parse_mode="HTML")
    await callback.answer()



@dp.callback_query(F.data == "report_last_admin")
async def report_last_admin_callback(callback: types.CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
        
    # 1. Находим последнего админа
    last_admin_id = None
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT admin_id FROM closed_dialogs_archive WHERE user_id = ? ORDER BY id DESC LIMIT 1", (uid,))
        res = cursor.fetchone()
        if res:
            # ИСПРАВЛЕНО: берем значение из кортежа
            last_admin_id = res[0]
            
    if not last_admin_id:
        await callback.answer("❌ История ваших обращений не найдена. Вам еще никто не отвечал.", show_alert=True)
        return
        
    # 2. Проверяем таблицу поданных жалоб
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM submitted_reports WHERE user_id = ? AND admin_id = ?", (uid, last_admin_id))
        already_reported = cursor.fetchone() is not None
        
    if already_reported:
        await callback.answer("❌ Вы уже использовали свой единственный шанс отправить жалобу по этому обращению.", show_alert=True)
        return

    await callback.answer()
    await state.set_state(ReportStates.waiting_for_report_text)
    
    text = (
        "⚠️ <b>Оформление жалобы на работу поддержки</b>\n\n"
        "Пожалуйста, напишите в одном сообщении, с чем именно вы не согласны.\n\n"
        "<i>Обратите внимание: на этот ответ вы можете пожаловаться только ОДИН раз. Вводите текст обдуманно.</i>"
    )
    
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back")]
    ])
    
    await callback.message.answer(text, reply_markup=back_kb, parse_mode="HTML")





# --- КОМАНДЫ И ОБРАБОТКА ДЛЯ АДМИНОВ ---


from aiogram.filters import Command

@dp.message(Command("ban"))
async def cmd_ban(message: types.Message):
    uid = message.from_user.id
    # Проверяем, что команду вводит админ
    if not is_admin(uid):
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer(
            "⚠️ <b>Неверный формат команды!</b>\n\n"
            "Пример использования:\n"
            "<code>/ban 123456789 Спам в репорты</code>",
            parse_mode="HTML"
        )
        return

    # Проверяем корректность ID пользователя
    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("❌ ID пользователя должен состоять только из цифр.")
        return

    # Получаем причину, если она указана
    reason = parts[2] if len(parts) > 2 else "Нарушение правил общения с поддержкой"

    # Защита от бана самого себя или овнера
    if target_id == uid or target_id == 8759913724:
        await message.answer("❌ Вы не можете заблокировать этого пользователя.")
        return

    # Добавляем в ЧС
    ban_user_in_db(target_id, reason)
    
    await message.answer(
        f"⛔️ <b>Пользователь успешно заблокирован!</b>\n\n"
        f"👤 ID: <code>{target_id}</code>\n"
        f"📝 Причина: <i>{reason}</i>",
        parse_mode="HTML"
    )
    
    # Уведомляем нарушителя (если он не заблокировал бота)
    try:
        await bot.send_message(
            chat_id=target_id,
            text=f"🔒 <b>Доступ к поддержке ограничен.</b>\nПричина: {reason}",
            parse_mode="HTML"
        )
    except Exception:
        pass


@dp.message(Command("unban"))
async def cmd_unban(message: types.Message):
    uid = message.from_user.id
    
    if not is_user_owner(uid):
        await message.answer("⚠️ <b>Доступ запрещен.</b> Эту команду может использовать только Главный Администратор.", parse_mode="HTML")
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("⚠️ Неверный формат! Пример: <code>/unban 123456789</code>", parse_mode="HTML")
        return

    try:
        # ИСПРАВЛЕНО: берем parts[1], а не весь список parts
        target_id = int(parts[1])
    except ValueError:
        await message.answer("❌ ID пользователя должен состоять только из цифр.")
        return

    if not is_user_banned(target_id):
        await message.answer("ℹ️ Этот пользователь не находится в черном списке.")
        return

    unban_user_in_db(target_id)
    
    await message.answer(f"✅ <b>Пользователь {target_id} успешно разблокирован!</b>", parse_mode="HTML")
    try:
        await bot.send_message(chat_id=target_id, text="🎉 <b>Доступ к системе тех. поддержки полностью восстановлен.</b>", parse_mode="HTML")
    except Exception: pass



@dp.message(Command("panel"))
async def cmd_panel(message: types.Message):
    uid = message.from_user.id
    if is_admin(uid):
        # Сразу удаляем команду админа, чтобы не засорять чат истории
        try:
            await message.delete()
        except Exception:
            pass
            
        info_text = (
            "📂 <b>Рабочая панель администратора</b>\n\n"
            "<pre>"
            "📋 ШПАРАЛКА ПО УПРАВЛЕНИЮ ЧАТОМ:\n"
            "• /leave - Временно выйти из чата\n"
            "• /close - Полностью закрыть тикет\n"
            "• /ban - Заблокировать пользователя\n"
            "</pre>\n"
            "<i>Выберите активный диалог из списка ниже для начала общения:</i>"
        )
        await message.answer(info_text, reply_markup=get_admin_panel_kb(uid), parse_mode="HTML")



@dp.callback_query(F.data == "refresh_panel")
async def refresh_panel(callback: types.CallbackQuery):
    uid = callback.from_user.id
    if not is_admin(uid):
        return
    try:
        # ИСПРАВЛЕНО: Вместо edit_markup используем правильный метод edit_reply_markup
        await callback.message.edit_reply_markup(reply_markup=get_admin_panel_kb(uid))
        await callback.answer("Список обновлен!")
    except TelegramBadRequest:
        # Эта ошибка вылетает, если список тикетов не изменился (кнопки остались теми же)
        await callback.answer("Новых диалогов нет.", show_alert=False)
    except Exception as e:
        logging.error(f"Ошибка при обновлении панели: {e}")
        await callback.answer("❌ Не удалось обновить список.")



@dp.callback_query(F.data.startswith("chat_"))
async def open_chat(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    if not is_admin(admin_id):
        return
    
    # Разбор данных callback_data: "chat_USERID_TOKEN"
    data_parts = callback.data.split("_")
    user_id = int(data_parts[1])
    button_token = data_parts[2]

    # --- ПРОВЕРКА БЕЗОПАСНОСТИ ПО ТОКЕНУ ---
    current_token = get_dialog_token(user_id)
    current_status = get_dialog_status(user_id)

    if current_token != button_token or current_status != "open":
        await callback.answer("⚠️ Эта кнопка устарела! Диалог уже обрабатывается или был закрыт.", show_alert=True)
        try:
            await callback.message.edit_markup(reply_markup=get_admin_panel_kb(admin_id))
        except Exception:
            pass
        return

    # --- ВЫТАСКИВАЕМ ПОСЛЕДНЕЕ СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ ИЗ БД ---
    user_message_preview = "Сообщение отсутствует"
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT last_message FROM dialogs WHERE user_id = ?", (user_id,))
            result = cursor.fetchone()
            if result and result[0]:
                user_message_preview = result[0]
    except Exception as e:
        logging.error(f"Ошибка получения сообщения из БД: {e}")

    # Подключаем админа к сессии общения
    set_admin_session(admin_id, user_id)
    update_dialog_status(user_id, "chatting")

    # Оформление системного сообщения на HTML с серым фоном для кнопок и цитаты
    chat_info = (
        f"🤝 <b>Вы вошли в чат с пользователем</b> <code>{user_id}</code>.\n\n"
        f"📩 <b>Последнее сообщение от него:</b>\n"
        f"<pre>{user_message_preview}</pre>\n\n"
        f"Теперь сообщения будут дублироваться напрямую.\n"
        f"• Выйти из чата: <code>/leave</code>\n"
        f"• Завершить тикет: <code>/close</code>\n"
        f"• Заблокировать пользователя <code>/ban</code>"
    )
    
    # Отправляем админу карточку входа и цитату
    await callback.message.answer(chat_info, parse_mode="HTML")
    
    try:
        await bot.send_message(chat_id=user_id, text="⚡️ Администратор подключился к диалогу. Скоро последует ответ.")
    except Exception:
        pass
        
    await callback.answer()


# --- СТАТИСТИКА И УПРАВЛЕНИЕ АДМИНАМИ (ДЛЯ ОВНЕРА / ГЛАВНОГО) ---



@dp.callback_query(F.data == "manage_admins")
async def manage_admins_callback(callback: types.CallbackQuery, state: FSMContext):
    """Интерактивная панель управления администраторами (только для Owner)"""
    uid = callback.from_user.id
    
    if not is_owner(uid):
        await callback.answer("⚠️ У вас нет прав Главного администратора.", show_alert=True)
        return
        
    await state.clear() # Сбрасываем старые состояния на всякий случай
    
    try:
        admins = get_all_admins()
        builder = InlineKeyboardBuilder()
        
        text = "👑 <b>Управление администраторами</b>\n\n"
        
        if not admins:
            text += "<i>В системе пока нет обычных администраторов.</i>"
        else:
            text += "Список действующих сотрудников поддержки. Вы можете снять любого из них в один клик:"
            
            # Строим кнопки для каждого админа динамически
            for row in admins:
                adm_id = row[0] if isinstance(row, (tuple, list)) else row
                
                # Кнопка 1 (Левая): Просто показывает ID админа
                builder.button(text=f"👤 ID: {adm_id}", callback_data=f"view_adm_{adm_id}")
                # Кнопка 2 (Правая): Снятие этого админа по клику
                builder.button(text="❌ Снять", callback_data=f"fire_adm_{adm_id}")
        
        # В самом низу добавляем кнопку добавления нового админа
        builder.button(text="➕ Добавить нового админа", callback_data="start_add_admin")

        builder.button(text="⬅️ Назад в меню", callback_data="back_to_panel")
        
        
        # Настраиваем сетку кнопок: по 2 кнопки на админа (ID и Снять), и 1 большая кнопка внизу
        # Если админов 3, то структура: [2, 2, 2, 1]
        sizes = [2] * len(admins) + [1]
        builder.adjust(*sizes)
        
        await callback.message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await callback.answer()
        
    except Exception as e:
        logging.error(f"Ошибка в manage_admins: {e}")
        await callback.answer("❌ Произошла ошибка при получении списка.", show_alert=True)



@dp.message(ReportStates.waiting_for_report_text, F.text)
async def handle_report_text(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or "Не указан"
    report_text = message.text
    
    await state.clear() # Очищаем состояние FSM
    
    # 1. Находим последний закрытый диалог
    last_admin_id = None
    chat_file_path = None
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT admin_id, file_path FROM closed_dialogs_archive WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,)
        )
        res = cursor.fetchone()
        if res:
            last_admin_id, chat_file_path = res[0], res[1]

    if not last_admin_id:
        await message.answer("❌ Ошибка поиска администратора.")
        return

    # 2. Ищем Telegram ID Овнера в вашей таблице 'admins'
    owner_id = None
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM admins WHERE role = 'owner' LIMIT 1")
            owner_res = cursor.fetchone()
            if owner_res:
                owner_id = owner_res[0]
    except Exception as db_err:
        logging.error(f"Ошибка при поиске овнера в БД: {db_err}")

    if not owner_id:
        owner_id = 8759913724 # Запасной ID со скриншота

    # 3. ФИКСИРУЕМ ИСПОЛЬЗОВАНИЕ ШАНСА: записываем в таблицу жалоб
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO submitted_reports (user_id, admin_id) VALUES (?, ?)", 
                (user_id, last_admin_id)
            )
            conn.commit()
    except Exception as e:
        logging.error(f"Ошибка фиксации использованного шанса в БД: {e}")

    # 4. Формируем карточку для Главного Админа
    owner_msg_text = (
        f"🚨 <b>ПОСТУПИЛА НОВАЯ ЖАЛОБА НА АДМИНА!</b>\n\n"
        f"👤 <b>Отправитель:</b> {message.from_user.mention_html()} (ID: <code>{user_id}</code>)\n"
        f"👮‍♂️ <b>На кого жалоба:</b> Администратор ID <code>{last_admin_id}</code>\n\n"
        f"📝 <b>Текст жалобы:</b>\n<i>{report_text}</i>"
    )

    # 5. Отправляем жалобу и файл лога Овнеру
    try:
        import os
        if chat_file_path and os.path.exists(chat_file_path):
            from aiogram.types import FSInputFile
            document = FSInputFile(chat_file_path)
            await bot.send_document(chat_id=owner_id, document=document, caption=owner_msg_text, parse_mode="HTML")
        else:
            await bot.send_message(chat_id=owner_id, text=owner_msg_text, parse_mode="HTML")
            
        # 6. Отвечаем пользователю
        await message.answer(
            "✅ <b>Ваша жалоба успешно отправлена Овнеру!</b>\n"
            "Руководство проекта рассмотрит её в ближайшее время.",
            reply_markup=get_user_start_kb(user_id),
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"❌ Не удалось доставить жалобу овнеру (ID: {owner_id}): {e}", exc_info=True)
        await message.answer("⚠️ Произошла ошибка при доставке вашей жалобы администрации.")






@dp.callback_query(F.data.startswith("fire_adm_"))
async def fire_admin_callback(callback: types.CallbackQuery):
    """Обработка клика по кнопке '❌ Снять'"""
    uid = callback.from_user.id
    if not is_owner(uid):
        await callback.answer("⚠️ Отказано в доступе.", show_alert=True)
        return
        
    target_id = int(callback.data.split("_")[2])
    
    if remove_admin_from_db(target_id):
        await callback.answer("✅ Администратор успешно удален!", show_alert=True)
        
        # Уведомляем самого уволенного пользователя
        try:
            await bot.send_message(
                chat_id=target_id,
                text="❌ Ваши полномочия администратора тех. поддержки были <b>аннулированы</b>.",
                parse_mode="HTML"
            )
        except Exception:
            pass
            
        # Удаляем сообщение и вызываем панель заново, чтобы список обновился на лету
        try:
            await callback.message.delete()
        except Exception:
            pass
            
        # Имитируем повторный вызов меню, чтобы Главный админ увидел свежий список
        await manage_admins_callback(callback, FSMContext)
    else:
        await callback.answer("⚠️ Не удалось удалить. Возможно, ID не найден.", show_alert=True)


@dp.callback_query(F.data == "start_add_admin")
async def start_add_admin_callback(callback: types.CallbackQuery, state: FSMContext):
    """Пользователь нажал '➕ Добавить нового админа'"""
    uid = callback.from_user.id
    if not is_owner(uid):
        await callback.answer("⚠️ Доступ заблокирован.", show_alert=True)
        return
        
    # Включаем стейт ожидания
    await state.set_state(AdminManagement.waiting_for_admin_id)
    
    await callback.message.answer(
        "📥 <b>Ожидание ввода данных</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Пожалуйста, введите <b>Telegram ID</b> пользователя, которого вы хотите назначить администратором:\n\n"
        "<i>*ID должен состоять только из цифр (например: 123456789)</i>",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("view_adm_"))
async def view_adm_stub(callback: types.CallbackQuery):
    # Заглушка, чтобы при нажатии на кнопку самого ID ничего не происходило, кроме закрытия анимации часиков
    await callback.answer()




@dp.message(AdminManagement.waiting_for_admin_id)
async def process_input_admin_id(message: types.Message, state: FSMContext):
    """Ловим введенный текст, когда бот находится в состоянии ожидания ID"""
    uid = message.from_user.id
    
    # Дополнительная проверка безопасности, что пишет именно владелец
    if not is_owner(uid):
        await state.clear()
        return

    target_text = message.text.strip()
    
    # Проверяем, что введены именно цифры ID (поиск по юзернейму через ботов без специального API Telegram сделать нельзя, поэтому используем ID)
    if not target_text.isdigit():
        await message.reply(
            "⚠️ <b>Ошибка ввода!</b>\n"
            "ID должен состоять строго из цифр. Попробуйте нажать кнопку добавления заново.",
            parse_mode="HTML"
        )
        await state.clear() # Сбрасываем ожидание при ошибке
        return

    target_id = int(target_text)
    
    # Сохраняем в SQLite3 базу данных
    add_admin_to_db(target_id)
    await state.clear() # Обязательно выключаем режим ожидания!
    
    success_card = (
        f"✅ <b>Сотрудник успешно добавлен!</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 Назначен: <code>{target_id}</code>\n"
        f"Права автоматически распределены на подключенных ботов."
    )
    await message.answer(success_card, parse_mode="HTML")
    
    # Автоматически пишем самому новому админу приятную новость
    try:
        await bot.send_message(
            chat_id=target_id,
            text="🎉 <b>Поздравляем!</b> Вы были назначены <b>Администратором</b> в тех. поддержке Sonata.\nИспользуйте команду /panel для управления очередью.",
            parse_mode="HTML"
        )
    except Exception:
        pass






@dp.callback_query(F.data == "view_stats")
async def view_stats_callback(callback: types.CallbackQuery):
    """Овнер видит список админов для проверки их логов"""
    uid = callback.from_user.id
    if not is_owner(uid):
        await callback.answer("⚠️ Нет прав.", show_alert=True)
        return

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT admin_id, closed_count FROM admin_stats")
        stats = cursor.fetchall()

    if not stats:
        await callback.message.answer("📊 Статистика пуста. Ни один чат еще не сохранен.")
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()
    text = "📊 <b>Выберите администратора для просмотра его закрытых диалогов:</b>\n"
    
    for adm_id, count in stats:
        # Кнопка ведет на архив чатов конкретного админа
        builder.button(text=f"👤 Админ {adm_id} ({count} чатов)", callback_data=f"arch_adm_{adm_id}")
    
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_panel")
    builder.adjust(1)
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("arch_adm_"))
async def view_admin_archive_callback(callback: types.CallbackQuery):
    """Показывает полную статистику и список закрытых чатов выбранного админа"""
    adm_id = int(callback.data.split("_")[2])
    
    # Подстраиваем формат под вашу запись: "10.08.2026"
    today_date_str = dt.datetime.now().strftime('%d.%m.%Y')
    
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        
        # 1. Считаем тикеты за ВСЕ ВРЕМЯ (без лимитов)
        cursor.execute(
            "SELECT COUNT(*) FROM closed_dialogs_archive WHERE admin_id = ?", 
            (adm_id,)
        )
        total_tickets = cursor.fetchone()[0]
        
        # 2. Считаем тикеты ЗА СЕГОДНЯ (Ищем вашу строку формата 10.08.2026 в %H:%M:%S)
        cursor.execute(
            "SELECT COUNT(*) FROM closed_dialogs_archive WHERE admin_id = ? AND closed_at LIKE ?", 
            (adm_id, f"{today_date_str}%")
        )
        today_tickets = cursor.fetchone()[0]
        
        # 3. Достаем абсолютно ВСЕ закрытые чаты этого админа
        cursor.execute(
            "SELECT id, user_id, username, closed_at FROM closed_dialogs_archive WHERE admin_id = ? ORDER BY id DESC",
            (adm_id,)
        )
        all_archive = cursor.fetchall()

    builder = InlineKeyboardBuilder()
    
    # Формируем карточку статистики
    text = (
        f"👤 <b>Профиль администратора:</b> <code>{adm_id}</code>\n\n"
        f"📊 <b>Статистика тикетов:</b>\n"
        f"├ За сегодня: <b>{today_tickets} шт.</b>\n"
        f"└ За все время: <b>{total_tickets} шт.</b>\n\n"
        f"📂 <b>Список всех закрытых диалогов ({len(all_archive)} шт.):</b>\n"
    )
    
    if not all_archive:
        text += "❌ <i>У этого админа пока нет записанных логов в архиве.</i>\n"
    else:
        # Чтобы не раздувать кнопки, выводим ВСЕ диалоги списком в тексте
        for idx, (arch_id, u_id, name, date) in enumerate(all_archive, 1):
            # В текст пишем абсолютно все диалоги
            text += f" {idx}. Юзер <code>{u_id}</code> ({name}) — <i>{date}</i>\n"
            
            # А кнопки создания файлов делаем ТОЛЬКО для последних 5, чтобы Telegram не выдал ошибку
            if idx <= 5:
                builder.button(text=f"📄 Скачать лог: {name}", callback_data=f"getfile_{arch_id}")
    
    text += "\n<i>*Кнопки доступны только для 5 последних диалогов во избежание зависания Telegram.</i>"
            
    builder.button(text="⬅️ Назад к списку админов", callback_data="view_stats")
    builder.adjust(1)
    
    # Если текст получился слишком длинным (больше 4096 символов), Telegram его обрежет. 
    # Защита от слишком длинного списка:
    if len(text) > 4000:
        text = text[:3900] + "\n\n⚠️ <i>Список слишком длинный и был обрезан...</i>"

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()



@dp.callback_query(F.data.startswith("getfile_"))
async def get_archive_file_callback(callback: types.CallbackQuery):
    """Отправляет файл .txt с логом переписки"""
    arch_id = int(callback.data.split("_")[1])
    
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT file_path, user_id FROM closed_dialogs_archive WHERE id = ?", (arch_id,))
        res = cursor.fetchone()

    if not res:
        await callback.answer("⚠️ Файл лога не найден в базе данных.", show_alert=True)
        return

    file_path, user_id = res
    
    if os.path.exists(file_path):
        await callback.answer("Отправляю файл лога...")
        # Принудительно отправляем документ в чат овнеру
        await callback.message.answer_document(
            document=types.FSInputFile(file_path),
            caption=f"📋 Полная история переписки с пользователем {user_id}"
        )
    else:
        await callback.answer("⚠️ Физический файл лога был удален с сервера.", show_alert=True)



@dp.message(Command("close"))
async def cmd_close(message: types.Message):
    uid = message.from_user.id
    if not is_admin(uid):
        return
    
    target_user_id = get_admin_session(uid)
    if not target_user_id:
        await message.answer("⚠️ Вы сейчас не находитесь в чате.")
        return

    # Достаем данные пользователя для архива
    username = "unknown"
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT username FROM dialogs WHERE user_id = ?", (target_user_id,))
        res = cursor.fetchone()
        if res: username = res[0]

    # --- ФОРМИРОВАНИЕ ТЕКСТОВОГО ФАЙЛА ИСТОРИИ ---
    close_time = dt.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    file_name = f"chat_logs/chat_{target_user_id}_{close_time}.txt"
    
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            # Берем всю историю сообщений этого чата
            cursor.execute("SELECT sender, text, timestamp FROM message_logs WHERE user_id = ? ORDER BY id ASC", (target_user_id,))
            history = cursor.fetchall()
            
            # Пишем в текстовый файл
            with open(file_name, "w", encoding="utf-8") as f:
                f.write(f"=== ИСТОРИЯ ДИАЛОГА ===\n")
                f.write(f"Пользователь ID: {target_user_id} ({username})\n")
                f.write(f"Администратор ID: {uid}\n")
                f.write(f"Закрыто: {dt.datetime.now().strftime('%d.%m.%Y в %H:%M:%S')}\n")
                f.write(f"========================\n\n")
                
                if not history:
                    f.write("[Сообщения не записывались или велись медиа-файлами без текста]\n")
                else:
                    for sender, text, timestamp in history:
                        f.write(f"[{timestamp}] {sender}: {text}\n")
            
            # Сохраняем информацию о файле в архив закрытых тикетов
            cursor.execute(
                "INSERT INTO closed_dialogs_archive (admin_id, user_id, username, closed_at, file_path) VALUES (?, ?, ?, ?, ?)",
                (uid, target_user_id, username, dt.datetime.now().strftime('%d.%m.%Y %H:%M'), file_name)
            )
            # Очищаем временные логи этого юзера, чтобы не переполнять БД
            cursor.execute("DELETE FROM message_logs WHERE user_id = ?", (target_user_id,))
            conn.commit()
            
    except Exception as e:
        logging.error(f"Ошибка сохранения файла лога: {e}")

    # Закрываем сессию (оригинальный код)
    set_admin_session(uid, None)
    update_dialog_status(target_user_id, "closed") 
    increment_admin_stat(uid)
    
    await message.answer("✅ Обращение успешно закрыто.", parse_mode="HTML")
    try:
        await bot.send_message(chat_id=target_user_id, text="✅ Ваше обращение успешно закрыто администратором.")
    except Exception: pass



@dp.message(Command("leave"))
async def cmd_leave(message: types.Message):
    uid = message.from_user.id
    if not is_admin(uid):
        return
    
    target_user_id = get_admin_session(uid)
    if not target_user_id:
        await message.answer("⚠️ Вы сейчас не находитесь ни в одном активном чате.")
        return
    
    set_admin_session(uid, None)
    update_dialog_status(target_user_id, "open") # Возвращаем в общую очередь (токен безопасности не меняется)
    
    leave_info = (
        "⏱ **Вы вышли из чата**\n"
        "Диалог снова доступен в /panel для всех админов."
    )
    await message.answer(leave_info, parse_mode="Markdown")
    
    try:
        await bot.send_message(chat_id=target_user_id, text="⏱ Администратор временно покинул чат. Пожалуйста, ожидайте.")
    except Exception:
        pass


@dp.callback_query(F.data == "admin_profile")
async def admin_profile_callback(callback: types.CallbackQuery):
    """Вывод личного кабинета администратора / создателя"""
    uid = callback.from_user.id
    if not is_admin(uid):
        return
        
    # Получаем данные из нашей новой функции
    role, days, closed_tickets = get_admin_profile_data(uid)
    
    # Красиво переводим системное название роли на понятный язык
    role_titles = {
        'owner': '👑 Главный Администратор',
        'admin': '🛠 Администратор'
    }
    display_role = role_titles.get(role, '🔒 Сотрудник')
    
    # Склонение слова "день/дня/дней"
    if days % 10 == 1 and days % 100 != 11:
        days_text = f"{days} день"
    elif 2 <= days % 10 <= 4 and (days % 100 < 10 or days % 100 >= 20):
        days_text = f"{days} дня"
    else:
        days_text = f"{days} дней"

    profile_text = (
        f"👤 <b>Личный кабинет сотрудника</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>Ваш Telegram ID:</b> <code>{uid}</code>\n"
        f"💼 <b>Ваш ранг:</b> {display_role}\n"
        f"⏱ <b>На должности:</b> <code>{days_text}</code>\n"
        f"📊 <b>Обработано тикетов:</b> <b>{closed_tickets}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<i>Спасибо за ваш вклад в работу поддержки Sonata VPN!</i>"
    )
    
    # Клавиатура ЛК с кнопкой «Назад в панель»
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_to_panel")]
    ])
    
    try:
        # Редактируем текущее сообщение панели, чтобы не плодить новые
        await callback.message.edit_text(text=profile_text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        # Если вдруг отредактировать нельзя, удаляем старое и шлем новое
        try: await callback.message.delete()
        except Exception: pass
        await callback.message.answer(text=profile_text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "back_to_panel")
async def back_to_panel_callback(callback: types.CallbackQuery):
    """Кнопка НАЗАД: возвращает админа из ЛК или управления в главное меню очереди"""
    uid = callback.from_user.id
    if not is_admin(uid):
        return
        
    info_text = (
        "📂 <b>Рабочая панель администратора</b>\n\n"
        "<pre>"
        "📋 ШПАРАЛКА ПО УПРАВЛЕНИЮ ЧАТОМ:\n"
        "• /leave - Временно выйти из чата\n"
        "• /close - Полностью закрыть тикет\n"
        "</pre>\n"
        "<i>Выберите активный диалог из списка ниже для начала общения:</i>"
    )
    
    try:
        # Возвращаем меню /panel прямо в этом же сообщении
        await callback.message.edit_text(text=info_text, reply_markup=get_admin_panel_kb(uid), parse_mode="HTML")
    except Exception:
        try: await callback.message.delete()
        except Exception: pass
        await callback.message.answer(text=info_text, reply_markup=get_admin_panel_kb(uid), parse_mode="HTML")
    await callback.answer()



# --- КОМАНДЫ ВЛАДЕЛЬЦА (Управление админами и овнерами) ---

@dp.message(Command("addadmin"))
async def cmd_add_admin(message: types.Message):
    uid = message.from_user.id
    if not is_owner(uid): 
        return
        
    args = message.text.split()
    # Заменили проверку на HTML-безопасную
    if len(args) < 2 or not args[1].isdigit(): 
        await message.answer("⚠️ Использование: <code>/addadmin 123456789</code>", parse_mode="HTML")
        return
        
    target_id = int(args[1])
    add_admin_to_db(target_id)
    
    await message.answer(f"✅ Пользователь <code>{target_id}</code> сохранен как Администратор.", parse_mode="HTML")
    
    # УВЕДОМЛЕНИЕ ПОЛЬЗОВАТЕЛЮ
    try:
        await bot.send_message(
            chat_id=target_id,
            text="🎉 <b>Поздравляем!</b> Вы назначены <b>Администратором</b> в тех. поддержке.\nВведите /panel, чтобы начать работу.",
            parse_mode="HTML"
        )
    except Exception:
        pass


@dp.message(Command("deladmin"))
async def cmd_del_admin(message: types.Message):
    if not is_owner(message.from_user.id): 
        return
        
    args = message.text.split()
    # Исправили ошибку парсинга (убрали < > и перевели на HTML)
    if len(args) < 2 or not args[1].isdigit(): 
        await message.answer("⚠️ Использование: <code>/deladmin 123456789</code>", parse_mode="HTML")
        return
        
    target_id = int(args[1])
    if target_id == OWNER_ID:
        await message.answer("⚠️ Нельзя удалить главного владельца.")
        return
        
    if remove_admin_from_db(target_id):
        await message.answer(f"❌ Пользователь <code>{target_id}</code> удален из админов.", parse_mode="HTML")
        
        # УВЕДОМЛЕНИЕ ПОЛЬЗОВАТЕЛЮ
        try:
            await bot.send_message(
                chat_id=target_id,
                text="❌ Вы были <b>удалены</b> из списка администраторов тех. поддержки.",
                parse_mode="HTML"
            )
        except Exception:
            pass
    else:
        await message.answer("⚠️ Данный ID не найден в списке администраторов.")


@dp.message(Command("addowner"))
async def cmd_add_owner(message: types.Message):
    uid = message.from_user.id
    if uid != OWNER_ID:
        return
        
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("⚠️ Использование: <code>/addowner 123456789</code>", parse_mode="HTML")
        return
        
    new_owner_id = int(args[1])
    add_owner_to_db(new_owner_id)
    
    success_text = (
        f"👑 <b>Новый Главный админ назначен!</b>\n"
        f"<pre>"
        f"ID: {new_owner_id}\n"
        f"Статус: Активирован\n"
        f"</pre>\n"
        f"Пользователю доступны функции просмотра статистики и управления админами."
    )
    await message.answer(success_text, parse_mode="HTML")
    
    # УВЕДОМЛЕНИЕ ПОЛЬЗОВАТЕЛЮ
    try:
        await bot.send_message(
            chat_id=new_owner_id,
            text="👑 Вы назначены <b>Главным администратором</b> (Owner).\nВам доступны функции управления персоналом и просмотра статистики.",
            parse_mode="HTML"
        )
    except Exception:
        pass


@dp.message(Command("delowner"))
async def cmd_del_owner(message: types.Message):
    uid = message.from_user.id
    if uid != OWNER_ID:
        return
        
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("⚠️ Использование: <code>/delowner 123456789</code>", parse_mode="HTML")
        return
        
    target_id = int(args[1])
    if target_id == OWNER_ID:
        await message.answer("⚠️ Вы не можете снять роль Главного админа с самого себя.")
        return
        
    if demote_owner_in_db(target_id):
        demote_text = (
            f"❌ <b>Полномочия отозваны!</b>\n"
            f"<pre>"
            f"ID: {target_id}\n"
            f"Статус: Понижен\n"
            f"</pre>\n"
            f"Пользователь переведен в ранг обычного администратора."
        )
        await message.answer(demote_text, parse_mode="HTML")
        
        # УВЕДОМЛЕНИЕ ПОЛЬЗОВАТЕЛЮ
        try:
            await bot.send_message(
                chat_id=target_id,
                text="⚠️ Ваши полномочия Главного администратора отозваны. Вы переведены в ранг <b>обычного администратора</b>.",
                parse_mode="HTML"
            )
        except Exception:
            pass
    else:
        await message.answer("⚠️ Пользователь с таким ID не найден в списке Главных администраторов.")










# --- ЕДИНЫЙ ОБРАБОТЧИК ДЛЯ ВСЕХ ТИПОВ СООБЩЕНИЙ И МЕДИА ---

@dp.message()
async def handle_all_messages(message: types.Message):
    uid = message.from_user.id

    # ЛОГИКА ДЛЯ АДМИНИСТРАТОРА
    if is_admin(uid):
        target_user_id = get_admin_session(uid)
        
        # Не отправляем системные команды пользователю в чат
        if message.text and message.text.startswith("/"):
            return
            
        if not target_user_id:
            await message.answer("⚠️ Вы не вошли в чат. Используйте команду /panel, чтобы выбрать пользователя.")
            return
            
        try:
            # Пересылаем сообщение пользователю
            await message.copy_to(chat_id=target_user_id)
            
            # --- СОХРАНЯЕМ ОТВЕТ АДМИНА В ЛОГ ДЛЯ ФАЙЛА ТХТ ---
            text_to_log = message.text if message.text else f"[{message.content_type.upper()}]"
            with sqlite3.connect(DB_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO message_logs (user_id, sender, text) VALUES (?, ?, ?)",
                    (target_user_id, f"Админ ({uid})", text_to_log)
                )
                conn.commit()
                
        except Exception as e:
            await message.reply(f"❌ Ошибка доставки пользователю: {e}")
        return

    # ЛОГИКА ДЛЯ ОБЫЧНОГО ПОЛЬЗОВАТЕЛЯ
    status = get_dialog_status(uid)

    # 1. Если админ уже зашел в чат и общается ('chatting') — разрешаем слать ВСЁ
    if status == "chatting":
        active_admin_id = get_admin_by_target_user(uid)
        if active_admin_id:
            try:
                # 1. Сначала пересылаем сообщение активному админу
                await message.copy_to(chat_id=active_admin_id)
                
                # 2. Сразу после этого сохраняем текст в базу данных для будущего файла .txt
                text_to_log = message.text if message.text else f"[{message.content_type.upper()}]"
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "INSERT INTO message_logs (user_id, sender, text) VALUES (?, ?, ?)",
                        (uid, "Пользователь", text_to_log)
                    )
                    conn.commit()
                    
            except Exception:
                # Если админ заблокировал бота или удалил чат, юзер увидит ошибку
                await message.answer("Не удалось доставить сообщение администратору.")
        return


    # 2. Если диалог уже создан, но админ еще не подключился ('open')
    if status == "open":
        await message.answer("❌ Ваше обращение уже находится в очереди. Пожалуйста, дождитесь подключения администратора.")
        return

    # 3. Если диалог закрыт ('closed') или новый — открываем обращение
    username = f"@{message.from_user.username}" if message.from_user.username else "Нет юзернейма"
    first_name = message.from_user.first_name
    text_preview = message.text if message.text else f"[{message.content_type.upper()}]"
    
    save_or_open_dialog(uid, username, first_name, text_preview)

    # --- ТАКЖЕ ЛОГИРУЕМ САМОЕ ПЕРВОЕ СООБЩЕНИЕ ТИКЕТА ---
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO message_logs (user_id, sender, text) VALUES (?, ?, ?)",
                (uid, "Пользователь (Первый вопрос)", text_preview)
            )
            conn.commit()
    except Exception:
        pass

    # Красивое админ-уведомление в формате HTML с серым фоном <pre>
    admin_notification = (
        f"🔔 <b>Новое обращение в систему!</b>\n"
        f"<pre>"
        f"👤 Пользователь: {first_name}\n"
        f"🔗 Юзернейм: {username}\n"
        f"🆔 Telegram ID: {uid}\n"
        f"📂 Тип данных: {message.content_type.upper()}\n"
        f"</pre>\n"
        f"📝 <b>Сообщение:</b>\n"
        f"<i>{text_preview}</i>\n\n"
        f"👉 Откройте /panel для ответа."
    )

    # Извлекаем всех администраторов (и owner, и admin) из таблицы admins
    admin_ids = []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            # ТОЧНЫЙ ЗАПРОС К ВАШЕЙ ТАБЛИЦЕ admins
            cursor.execute("SELECT user_id FROM admins WHERE role IN ('admin', 'owner')")
            rows = cursor.fetchall()
            # Извлекаем числа из кортежей БД
            admin_ids = [row[0] for row in rows]
    except Exception as e:
        logging.error(f"Ошибка получения списка админов из таблицы admins: {e}")

    # Фолбек: если база данных недоступна, отправляем хотя бы создателю
    if not admin_ids:
        admin_ids = [OWNER_ID]

    # Рассылаем уведомление ВСЕМ найденным админам по очереди
    for admin_id in admin_ids:
        try:
            await bot.send_message(chat_id=admin_id, text=admin_notification, parse_mode="HTML")
        except Exception as e:
            # Если один админ заблокировал бота, цикл не прервется и отправит остальным
            logging.warning(f"Не удалось отправить уведомление админу {admin_id}: {e}")
        
    await message.answer("🚀 Ваше обращение успешно зарегистрировано в системе. Ожидайте подключения администратора!")

# --- ЗАПУСК БОТА ---
async def main():
    print("Бот со встроенной защитой токенов, оформлением и статистикой запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

  

