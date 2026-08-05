import asyncio
import logging
import sqlite3
import secrets  # Для генерации уникальных токенов диалогов
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

# --- НАСТРОЙКИ ---
TOKEN = "8778414676:AAHWdX12JWXv5FjKvGb8F83WziNuXh3ZFuI"
OWNER_ID = 8759913724
DB_FILE = "bot_database.db"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# --- ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ---
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        # Таблица администраторов
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                role TEXT DEFAULT 'admin'
            )
        """)
        # Таблица диалогов (Добавлена колонка dialog_token)
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
        # Таблица статистики админов (Новая)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_stats (
                admin_id INTEGER PRIMARY KEY,
                closed_count INTEGER DEFAULT 0
            )
        """)
        # Всегда добавляем создателя в БД как супер-админа
        cursor.execute("INSERT OR IGNORE INTO admins (user_id, role) VALUES (?, 'owner')", (OWNER_ID,))
        conn.commit()

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
        cursor.execute("INSERT OR IGNORE INTO admins (user_id, role) VALUES (?, 'admin')", (user_id,))
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




# --- КЛАВИАТУРЫ ---
def get_user_start_kb():
    """Клавиатура для главного меню пользователя при /start"""
    builder = InlineKeyboardBuilder()
    builder.button(text="✍️ Написать в тех. поддержку", callback_data="contact_support")
    builder.button(text="❓ Часто задаваемые вопросы (FAQ)", callback_data="faq_menu")
    builder.adjust(1)
    return builder.as_markup()

def get_admin_panel_kb(user_id: int):
    builder = InlineKeyboardBuilder()
    dialogs = get_active_dialogs()

    for uid, username, name, msg, token in dialogs:
        display_name = username if username else name
        # Зашиваем токен прямо в callback_data: "chat_USERID_TOKEN"
        builder.button(text=f"💬 {display_name}: {msg}", callback_data=f"chat_{uid}_{token}")

    builder.button(text="🔄 Обновить список", callback_data="refresh_panel")
    
    if is_owner(user_id):
        builder.button(text="👑 Управление админами", callback_data="manage_admins")
        builder.button(text="📊 Статистика админов", callback_data="view_stats")
        
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
            reply_markup=get_user_start_kb()
        )

@dp.callback_query(F.data == "contact_support")
async def contact_support_callback(callback: types.CallbackQuery):
    uid = callback.from_user.id
    status = get_dialog_status(uid)
    
    if status in ["open", "chatting"]:
        await callback.message.answer("⏳ У вас уже есть активный вопрос в разработке. Пожалуйста, ожидайте ответа.")
    else:
        await callback.message.answer("📥 Пожалуйста, отправьте ваше сообщение или файл в этот чат, и мы сразу передадим его агентам техподдержки.")
    await callback.answer()

@dp.callback_query(F.data == "faq_menu")
async def faq_menu_callback(callback: types.CallbackQuery):
    faq_text = (
        "❓ **Часто задаваемые вопросы:**\n\n"
        "🔹 **Вопрос 1:** Как долго ждать ответ?\n"
        "🔸 Ответ: Обычно администраторы отвечают в течение 10-15 минут.\n\n"
        "🔹 Вопрос 2: Можно ли отправлять медиафайлы?\n"
        "🔸 Ответ: Да, вы можете отправлять скриншоты, документы и голосовые сообщения."
        "\n\n Вопросы и ответы будут обновляться."
    )
    await callback.message.answer(faq_text, parse_mode="Markdown")
    await callback.answer()


# --- КОМАНДЫ И ОБРАБОТКА ДЛЯ АДМИНОВ ---

@dp.message(Command("panel"))
async def cmd_panel(message: types.Message):
    uid = message.from_user.id
    if is_admin(uid):
        # Красивое оформление шпаргалки для админа в виде блока
        info_text = (
            "📂 **Список активных диалогов**\n"
            "📋 ШПАРАЛКА ПО УПРАВЛЕНИЮ ЧАТОМ:\n"
            "• /leave - Временно выйти из чата\n"
            "• /close - Полностью закрыть тикет\n"
            "• /info  - Посмотреть все команды\n"
        )
        await message.answer(info_text, reply_markup=get_admin_panel_kb(uid), parse_mode="Markdown")


@dp.callback_query(F.data == "refresh_panel")
async def refresh_panel(callback: types.CallbackQuery):
    uid = callback.from_user.id
    if not is_admin(uid):
        return
    try:
        await callback.message.edit_markup(reply_markup=get_admin_panel_kb(uid))
        await callback.answer("Список обновлен!")
    except TelegramBadRequest:
        await callback.answer("Новых диалогов нет.", show_alert=False)


@dp.callback_query(F.data.startswith("chat_"))
async def open_chat(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    if not is_admin(admin_id):
        return
    
    # ИСПРАВЛЕНО: Добавлены правильные индексы [1] и [2] для извлечения данных из callback_data
    data_parts = callback.data.split("_")
    user_id = int(data_parts[1])
    button_token = data_parts[2]

    # --- ПРОВЕРКА БЕЗОПАСНОСТИ ПО ТОКЕНУ ---
    current_token = get_dialog_token(user_id)
    current_status = get_dialog_status(user_id)

    if current_token != button_token or current_status != "open":
        await callback.answer("⚠️ Эта кнопка устарела! Диалог уже обрабатывается или был закрыт.", show_alert=True)
        # Автоматически обновляем панель, чтобы убрать неактуальную кнопку
        try:
            await callback.message.edit_markup(reply_markup=get_admin_panel_kb(admin_id))
        except Exception:
            pass
        return

    # Подключаем админа к сессии общения
    set_admin_session(admin_id, user_id)
    update_dialog_status(user_id, "chatting")

    # Оформление системного сообщения для админа при входе в чат
    chat_info = (
        f"🤝 **Вы вошли в чат с пользователем** {user_id}.\n\n"
        f"Теперь сообщения будут дублироваться напрямую.\n\n"
        f"• Выйти из чата: /leave\n"
        f"• Завершить тикет: /close\n"
    )
    await callback.message.answer(chat_info, parse_mode="Markdown")
    
    try:
        await bot.send_message(chat_id=user_id, text="⚡️ Администратор подключился к диалогу. Скоро последует ответ.")
    except Exception:
        pass
    await callback.answer()

# --- СТАТИСТИКА И УПРАВЛЕНИЕ АДМИНАМИ (ДЛЯ ОВНЕРА / ГЛАВНОГО) ---



@dp.callback_query(F.data == "manage_admins")
async def manage_admins_callback(callback: types.CallbackQuery):
    """Обработка кнопки '👑 Управление админами' (только для Главного админа)"""
    uid = callback.from_user.id
    
    # Проверяем роль пользователя в БД
    if not is_owner(uid):
        await callback.answer("⚠️ У вас нет прав Главного администратора.", show_alert=True)
        return
        
    try:
        admins = get_all_admins()
        
        # Переводим оформление на красивый HTML с фоном <pre>
        if not admins:
            text = (
                "👑 <b>Управление администраторами</b>\n\n"
                "<i>Обычных админов пока нет.</i>\n\n"
            )
        else:
            text = "👑 <b>Действующие администраторы:</b>\n<pre>"
            for i, adm_id in enumerate(admins, 1):
                text += f"{i}. ID: {adm_id}\n"
            text += "</pre>\n"
            
        text += (
            "💡 Чтобы <b>добавить</b> админа, напиши:\n<code>/addadmin ID</code>\n\n"
            "💡 Чтобы <b>удалить</b> админа, напиши:\n<code>/deladmin ID</code>"
        )
        
        # Отправляем новое сообщение, чтобы админка открылась корректно
        await callback.message.answer(text, parse_mode="HTML")
        await callback.answer() # Закрываем часы загрузки на кнопке
        
    except Exception as e:
        logging.error(f"Ошибка в manage_admins: {e}")
        await callback.answer("❌ Произошла ошибка при получении списка админов.", show_alert=True)





@dp.callback_query(F.data == "view_stats")
async def view_stats_callback(callback: types.CallbackQuery):
    uid = callback.from_user.id
    if not is_owner(uid):
        await callback.answer("⚠️ У вас нет прав Главного администратора.", show_alert=True)
        return

    stats = get_admin_stats()
    if not stats:
        text = "📊 **Статистика админов:**\n\nНи один админ еще не закрыл ни одного диалога."
    else:
        text = "📊 **Статистика обработанных диалогов:**\n\n"
        for adm_id, count in stats:
            # Красивые текстовые карточки для каждого админа
            text += (
                f"👤 Админ ID: {adm_id}\n"
                f"✅ Закрыто тикетов: {count}"
            )
            
    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()


@dp.message(Command("close"))
async def cmd_close(message: types.Message):
    uid = message.from_user.id
    if not is_admin(uid):
        return
    
    target_user_id = get_admin_session(uid)
    if not target_user_id:
        await message.answer("⚠️ Вы сейчас не находитесь в чате. Сначала выберите чат через <code>/panel</code>.")
        return
    
    set_admin_session(uid, None)
    update_dialog_status(target_user_id, "closed") 
    
    # Добавляем +1 закрытый тикет в статистику админу
    increment_admin_stat(uid)
    
    close_info = (
        "✅ **Обращение успешно закрыто**\n"
        "Вам начислено +1 к обработанным диалогам.\n"
        "Пользователь сможет создать новый тикет.\n"
    )
    await message.answer(close_info, parse_mode="Markdown")
    
    try:
        await bot.send_message(
            chat_id=target_user_id, 
            text="✅ Ваше обращение успешно закрыто администратором. Вы можете отправить новое сообщение, если у вас возникнут вопросы."
        )
    except Exception:
        pass


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


@dp.message(Command("info"))
async def cmd_info(message: types.Message):
    uid = message.from_user.id
    if not is_admin(uid):
        return

    admin_text = (
        "🛠 **Панель управления администратора:**\n"
        "• /panel — Открыть список активных диалогов.\n"
        "• /leave — Выйти из текущего диалога (оставив открытым).\n"
        "• /close — Полностью закрыть обращение.\n"
    )
    if is_owner(uid):
        admin_text += (
            "\n👑 **Команды Главного владельца:**\n"
            "• /addadmin <ID> — Назначить администратора.\n"
            "• /deladmin <ID> — Удалить администратора."
        )
    await message.answer(admin_text, parse_mode="Markdown")


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
            await message.answer("Вы не вошли в чат. Используйте /panel, чтобы выбрать пользователя.")
            return
            
        try:
            await message.copy_to(chat_id=target_user_id)
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
                await message.copy_to(chat_id=active_admin_id)
            except Exception:
                await message.answer("Не удалось доставить сообщение администратору.")
        return

    # 2. Если диалог уже создан, но админ еще не подключился ('open')
    if status == "open":
        await message.answer("❌ Ваше обращение уже находится в очереди. Ожидайте подключения администратора.")
        return

    # 3. Если диалог закрыт ('closed') или новый — открываем обращение
    username = f"@{message.from_user.username}" if message.from_user.username else "Нет юзернейма"
    first_name = message.from_user.first_name
    text_preview = message.text if message.text else f"[{message.content_type.upper()}]"
    
    save_or_open_dialog(uid, username, first_name, text_preview)

    # Красивое админ-уведомление в виде карточки (с серым фоном)
    admin_notification = (
        f"🔔 **Новое обращение в систему!**\n"
        f"```\n"
        f"👤 Пользователь: {first_name}\n"
        f"🔗 Юзернейм: {username}\n"
        f"🆔 Telegram ID: {uid}\n"
        f"📂 Тип данных: {message.content_type.upper()}\n"
        f"```\n"
        f"📝 **Сообщение:**\n"
        f"_{text_preview}_\n\n"
        f"👉 Откройте /panel для ответа."
    )

    try:
        await bot.send_message(chat_id=OWNER_ID, text=admin_notification, parse_mode="Markdown")
    except Exception:
        pass
        
    await message.answer("🚀 Ваше обращение успешно зарегистрировано в системе. Ожидайте подключения администратора!")


# --- ЗАПУСК БОТА ---
async def main():
    print("Бот со встроенной защитой токенов, оформлением и статистикой запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

  

