import asyncio
import logging
import sqlite3
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
        # Таблица диалогов
        # status может быть: 'open' (ждет ответа), 'chatting' (админ общается с ним), 'closed' (решено)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dialogs (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_message TEXT,
                status TEXT DEFAULT 'open'
            )
        """)
        # Таблица текущих сессий админов (в каком чате сейчас находится админ)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_sessions (
                admin_id INTEGER PRIMARY KEY,
                target_user_id INTEGER
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
        cursor.execute("SELECT user_id FROM admins WHERE user_id = ?", (user_id,))
        return cursor.fetchone() is not None

def add_admin_to_db(user_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO admins (user_id, role) VALUES (?, 'admin')", (user_id,))
        conn.commit()

def remove_admin_from_db(user_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM admins WHERE user_id = ? AND role != 'owner'", (user_id,))
        conn.commit()


# --- ФУНКЦИИ УПРАВЛЕНИЯ СЕССИЯМИ И ДИАЛОГАМИ ---
def get_dialog_status(user_id: int) -> str | None:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM dialogs WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        return result[0] if result else None

def save_or_open_dialog(user_id: int, username: str, first_name: str, text: str):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO dialogs (user_id, username, first_name, last_message, status)
            VALUES (?, ?, ?, ?, 'open')
            ON CONFLICT(user_id) DO UPDATE SET last_message = excluded.last_message, status = 'open'
        """, (user_id, username, first_name, text[:50]))
        conn.commit()

def update_dialog_status(user_id: int, new_status: str):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE dialogs SET status = ? WHERE user_id = ?", (new_status, user_id))
        conn.commit()

def get_active_dialogs():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        # Показываем админу диалоги, которые ждут ответа ('open')
        cursor.execute("SELECT user_id, username, first_name, last_message FROM dialogs WHERE status = 'open'")
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


# --- КЛАВИАТУРЫ ---
def get_admin_panel_kb():
    builder = InlineKeyboardBuilder()
    dialogs = get_active_dialogs()

    for uid, username, name, msg in dialogs:
        display_name = username if username else name
        builder.button(text=f"💬 {display_name}: {msg}", callback_data=f"chat_{uid}")

    builder.button(text="🔄 Обновить список", callback_data="refresh_panel")
    builder.adjust(1)
    return builder.as_markup()


# --- КОМАНДЫ ДОСТУПНЫЕ ВСЕМ / АДМИНАМ ---
@dp.message(Command("info"))
async def cmd_info(message: types.Message):
    uid = message.from_user.id
    
    # Текст для обычного пользователя
    user_text = (
        "ℹ️ **Справка для пользователя:**\n"
        "• `/start` — Начать работу с ботом и отправить свой вопрос.\n"
        "• `/info` — Посмотреть список команд.\n\n"
        "После отправки первого сообщения диалог фиксируется, отправка новых медиа/сообщений будет "
        "заблокирована, пока администратор не войдет в диалог и не начнет с вами общение."
    )
    
    # Текст для администраторов
    admin_text = (
        "🛠 **Панель управления администратора:**\n"
        "• `/panel` — Открыть список активных диалогов (инлайн-меню).\n"
        "• `/leave` — Выйти из текущего диалога с пользователем (оставив его открытым).\n"
        "• `/close` — Полностью закрыть обращение (пользователь сможет писать снова).\n"
        "• `/info` — Вызов этого меню.\n\n"
        "👑 **Команды Главного владельца (Owner):**\n"
        "• `/addadmin <ID>` — Назначить нового администратора бота.\n"
        "• `/deladmin <ID>` — Удалить администратора из базы данных."
    )
    
    if is_admin(uid):
        await message.answer(admin_text, parse_mode="Markdown")
    else:
        await message.answer(user_text, parse_mode="Markdown")


# --- КОМАНДЫ ВЛАДЕЛЬЦА (Управление админами) ---
@dp.message(Command("addadmin"))
async def cmd_add_admin(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование: `/addadmin <Telegram_ID>`", parse_mode="Markdown")
        return
    new_id = int(args[1])
    add_admin_to_db(new_id)
    await message.answer(f"Пользователь `{new_id}` успешно сохранен в SQLite как **Администратор**.", parse_mode="Markdown")


@dp.message(Command("deladmin"))
async def cmd_del_admin(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование: `/deladmin <Telegram_ID>`", parse_mode="Markdown")
        return
    target_id = int(args[1])
    if target_id == OWNER_ID:
        await message.answer("Нельзя удалить главного владельца.")
        return
    remove_admin_from_db(target_id)
    await message.answer(f"Пользователь `{target_id}` удален из базы данных админов.", parse_mode="Markdown")


# --- УПРАВЛЕНИЕ ЧАТОМ ДЛЯ АДМИНА ---
@dp.message(Command("leave"))
async def cmd_leave(message: types.Message):
    uid = message.from_user.id
    if not is_admin(uid):
        return
    
    target_user_id = get_admin_session(uid)
    if not target_user_id:
        await message.answer("Вы сейчас не находитесь ни в одном активном чате.")
        return
    
    set_admin_session(uid, None)
    update_dialog_status(target_user_id, "open") # Возвращаем статус в очередь
    await message.answer("Вы вышли из чата. Диалог остался открытым в `/panel` для вас или других админов.")
    try:
        await bot.send_message(chat_id=target_user_id, text="⏱ Администратор временно покинул чат. Пожалуйста, ожидайте.")
    except Exception:
        pass


@dp.message(Command("close"))
async def cmd_close(message: types.Message):
    uid = message.from_user.id
    if not is_admin(uid):
        return
    
    target_user_id = get_admin_session(uid)
    if not target_user_id:
        await message.answer("Вы сейчас не находитесь в чате. Сначала выберите чат через `/panel`.")
        return
    
    set_admin_session(uid, None)
    update_dialog_status(target_user_id, "closed") # Полное закрытие
    await message.answer("✅ Обращение успешно закрыто. Теперь пользователь может создать новый тикет.")
    try:
        await bot.send_message(chat_id=target_user_id, text="✅ Ваше обращение закрыто администратором. Вы можете отправить новое сообщение, если у вас возникнут вопросы.")
    except Exception:
        pass


# --- ОБРАБОТКА СТАРТА И ПАНЕЛИ ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    uid = message.from_user.id
    if is_admin(uid):
        await message.answer("Приветствуем в панели! Используйте команду `/panel` или проверьте команды через `/info`.", parse_mode="Markdown")
    else:
        # (Этот кусок относится к концу функции cmd_start)
        status = get_dialog_status(uid)
        if status in ["open", "chatting"]:
            await message.answer("⏳ У вас уже есть активный вопрос в разработке. Пожалуйста, ожидайте ответа.")
        else:
            await message.answer("👋 Здравствуйте! Отправьте ваше сообщение или файл, и мы вам поможем.")


@dp.message(Command("panel"))
async def cmd_panel(message: types.Message):
    if is_admin(message.from_user.id):
        await message.answer("📂 Список активных диалогов:", reply_markup=get_admin_panel_kb())


# --- ОБРАБОТКА НАЖАТИЙ НА КНОПКИ ПАНЕЛИ ---

@dp.callback_query(F.data == "refresh_panel")
async def refresh_panel(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    try:
        await callback.message.edit_text("📂 Обновленный список активных диалогов:", reply_markup=get_admin_panel_kb())
        await callback.answer("Список обновлен!")
    except TelegramBadRequest:
        await callback.answer("Новых диалогов нет, список актуален.", show_alert=False)


@dp.callback_query(F.data.startswith("chat_"))
async def open_chat(callback: types.CallbackQuery):
    admin_id = callback.from_user.id
    if not is_admin(admin_id):
        return
    
    # Исправлено: добавлен индекс, чтобы правильно забрать ID из строки "chat_123456"
    user_id = int(callback.data.split("_")[1])

    # Подключаем админа к сессии общения
    set_admin_session(admin_id, user_id)
    update_dialog_status(user_id, "chatting")

    await callback.message.answer(
        f"🤝 Вы вошли в чат с пользователем `{user_id}`.\n\n"
        f"Теперь ЛЮБЫЕ отправленные вами сообщения и медиафайлы будут пересылаться напрямую ему.\n\n"
        f"• Выйти из чата (оставить открытым): `/leave`\n"
        f"• Закончить диалог (решено): `/close`",
        parse_mode="Markdown"
    )
    try:
        await bot.send_message(chat_id=user_id, text="⚡️ Администратор подключился к диалогу. Вы можете общаться и отправлять медиафайлы.")
    except Exception:
        pass
    await callback.answer()


# --- ЕДИНЫЙ ОБРАБОТЧИК ДЛЯ ВСЕХ ТИПОВ СООБЩЕНИЙ И МЕДИА ---

@dp.message()
async def handle_all_messages(message: types.Message):
    uid = message.from_user.id

    # ЛОГИКА ДЛЯ АДМИНИСТРАТОРА
    if is_admin(uid):
        # Проверяем, находится ли админ в сессии чата
        target_user_id = get_admin_session(uid)

        # Перехватываем системные команды, чтобы не слать их пользователю
        if message.text and message.text.startswith("/"):
            return

        if not target_user_id:
            await message.answer("Вы не вошли в чат. Используйте `/panel`, чтобы выбрать пользователя.")
            return

        # Пересылаем (копируем) ЛЮБОЙ медиафайл или текст пользователю
        try:
            await message.copy_to(chat_id=target_user_id)
        except Exception as e:
            await message.reply(f"❌ Ошибка доставки пользователю: {e}")
        return

    # ЛОГИКА ДЛЯ ОБЫЧНОГО ПОЛЬЗОВАТЕЛЯ
    status = get_dialog_status(uid)

    # Если админ уже зашел в чат и общается ('chatting') — разрешаем слать ВСЁ
    if status == "chatting":
        active_admin_id = get_admin_by_target_user(uid)
        if active_admin_id:
            try:
                await message.copy_to(chat_id=active_admin_id)
            except Exception:
                await message.answer("Не удалось доставить сообщение администратору.")
        return

    # Если диалог уже создан, но админ еще не подключился ('open')
    if status == "open":
        await message.answer("❌ Ваш вопрос уже находится в очереди. Пожалуйста, дождитесь, пока администратор подключится к чату.")
        return

    # Если диалог 'closed' или новый — открываем обращение
    username = f"@{message.from_user.username}" if message.from_user.username else "Нет юзернейма"
    first_name = message.from_user.first_name
    
    # Определяем текст для превью в админке
    text_preview = message.text if message.text else f"[{message.content_type.upper()}]"
    
    save_or_open_dialog(uid, username, first_name, text_preview)

    # Уведомляем создателя
    try:
        await bot.send_message(
            chat_id=OWNER_ID,
            text=f"🔔 Новое обращение от {first_name} ({username}) [ID: `{uid}`]:\nТип: {message.content_type}\n\nПосмотрите в `/panel`",
            parse_mode="Markdown"
        )
    except Exception:
        pass
        
    await message.answer("🚀 Ваше обращение успешно зарегистрировано в системе. Ожидайте подключения администратора!")


 



# --- ЗАПУСК БОТА ---
async def main():
    print("Бот на SQLite3 успешно запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

  

