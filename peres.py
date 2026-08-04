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
        # Таблица активных диалогов со статусом (open / closed)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dialogs (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_message TEXT,
                status TEXT DEFAULT 'open'
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


# --- ФУНКЦИИ УПРАВЛЕНИЯ ДИАЛОГАМИ ---
def get_dialog_status(user_id: int) -> str | None:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM dialogs WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        return result[0] if result else None

def save_or_open_dialog(user_id: int, username: str, first_name: str, text: str):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        # Если диалог уже был closed, переоткрываем его. Если не было — создаем open.
        cursor.execute("""
            INSERT INTO dialogs (user_id, username, first_name, last_message, status)
            VALUES (?, ?, ?, ?, 'open')
            ON CONFLICT(user_id) DO UPDATE SET last_message = excluded.last_message, status = 'open'
        """, (user_id, username, first_name, text[:50]))
        conn.commit()

def close_dialog_in_db(user_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE dialogs SET status = 'closed' WHERE user_id = ?", (user_id,))
        conn.commit()

def get_active_dialogs():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        # Выводим в админку только незакрытые диалоги
        cursor.execute("SELECT user_id, username, first_name, last_message FROM dialogs WHERE status = 'open'")
        return cursor.fetchall()


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

    try:
        await bot.send_message(
            chat_id=new_id,
            text="🎉 Вы были назначены администратором!\nИспользуйте команду /panel для просмотра диалогов."
        )
    except Exception:
        pass


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


# --- ОБРАБОТКА СТАРТА И ПАНЕЛИ ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    uid = message.from_user.id
    
    if is_admin(uid):
        await message.answer("Приветствуем в панели управления! Вот список диалогов пользователей:", reply_markup=get_admin_panel_kb())
    else:
        # Проверяем текущий статус
        status = get_dialog_status(uid)
        if status == "open":
            await message.answer("⏳ У вас уже есть активный вопрос в разработке. Пожалуйста, дождитесь ответа администратора.")
        else:
            await message.answer("👋 Здравствуйте! Отправьте ваше сообщение, и оно будет передано администраторам.")


@dp.message(Command("panel"))
async def cmd_panel(message: types.Message):
    if is_admin(message.from_user.id):
        await message.answer("📂 Список активных диалогов:", reply_markup=get_admin_panel_kb())


# --- ОБРАБОТКА НАЖАТИЙ НА КНОПКИ ПАНЕЛИ ---
@dp.callback_query(F.data == "refresh_panel")
async def refresh_panel(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    # Исправление ошибки Bad Request: ловим исключение, если контент не изменился
    try:
        await callback.message.edit_text("📂 Обновленный список активных диалогов:", reply_markup=get_admin_panel_kb())
        await callback.answer("Список обновлен!")
    except TelegramBadRequest:
        await callback.answer("Новых диалогов нет, список актуален.", show_alert=False)


@dp.callback_query(F.data.startswith("chat_"))
async def open_chat(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    user_id = int(callback.data.split("_")[1])

    await callback.message.answer(
        f"✍️ Вы выбрали диалог с пользователем `{user_id}`.\n\n"
        f"**Чтобы ответить ему, сделайте REPLY (Ответ)** на это сообщение и введите ваш ответ.\n"
        f"После вашего ответа диалог закроется, и пользователь сможет написать снова.",
        parse_mode="Markdown"
    )
    await bot.send_message(
        chat_id=callback.from_user.id, 
        text=f"Ответ для пользователя [ID_USER: `{user_id}`]", 
        parse_mode="Markdown"
    )
    await callback.answer()


# --- ЛОГИКА ПЕРЕСЫЛКИ И ОТВЕТОВ ---
@dp.message(F.chat.type == "private")
async def handle_messages(message: types.Message):
    uid = message.from_user.id

    # Если пишет админ
    if is_admin(uid):
        if message.reply_to_message and "ID_USER:" in message.reply_to_message.text:
            try:
                text_reply = message.reply_to_message.text
                target_user_id = int(text_reply.split("ID_USER: `")[1].split("`")[0])
                
                # Отправляем ответ пользователю
                await message.copy_to(chat_id=target_user_id)
                
                # Меняем статус диалога на 'closed' в БД, чтобы пользователь мог писать снова
                close_dialog_in_db(target_user_id)
                
                await message.reply("✅ Ответ отправлен! Диалог закрыт, пользователь снова может писать.")
            except Exception as e:
                await message.reply(f"❌ Ошибка отправки: {e}")
        else:
            await message.answer("Используйте команду /panel, выберите пользователя и отвечайте реплаем на сообщение с его ID.")
        return

    # Если пишет обычный пользователь
    status = get_dialog_status(uid)
    
    # Новое условие: Если диалог открыт, запрещаем слать новые сообщения
    if status == "open":
        await message.answer("❌ Вы не можете отправить новое сообщение. Пожалуйста, дождитесь ответа администратора на ваш предыдущий вопрос.")
        return

    # Если диалога нет или он closed — регистрируем новое обращение
    username = f"@{message.from_user.username}" if message.from_user.username else "Нет юзернейма"
    first_name = message.from_user.first_name
    text_content = message.text if message.text else "[Медиа/Файл]"

    save_or_open_dialog(uid, username, first_name, text_content)

    # Уведомляем создателя бота о новом открытом диалоге
    try:
        await bot.send_message(
            chat_id=OWNER_ID,
            text=f"🔔 Новое сообщение от {first_name} ({username}) [ID: `{uid}`]:\n_{text_content}_\n\nПосмотрите в /panel",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    await message.answer("Ваш вопрос передан в разработку. Администратор скоро ответит вам.")




# --- ЗАПУСК БОТА ---
async def main():
    print("Бот на SQLite3 успешно запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

  

