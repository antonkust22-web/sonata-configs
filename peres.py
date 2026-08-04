import asyncio
import logging
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import Message

# Вставьте ваш токен и ваш Telegram ID (главный админ)
TOKEN = "8778414676:AAHWdX12JWXv5FjKvGb8F83WziNuXh3ZFuI"
OWNER_ID = 8759913724

bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()

# База данных в памяти (для примера)
admins = {OWNER_ID}
forward_pairs = {}  # {from_user_id: to_user_id}


@router.message(Command("start"))
async def cmd_start(message: Message):
  await message.answer("Бот запущен. Я пересылаю сообщения.")


@router.message(Command("addadmin"))
async def cmd_add_admin(message: Message):
  if message.from_user.id != OWNER_ID:
    await message.answer("Только главный администратор может добавлять админов.")
    return

  args = message.text.split()
  if len(args) < 2 or not args[1].isdigit():
    await message.answer("Использование: /addadmin ID_пользователя")
    return

  new_admin_id = int(args[1])
  admins.add(new_admin_id)
  await message.answer(f"Пользователь {new теперь администратор.")


@router.message(Command("bind"))
async def cmd_bind(message: Message):
  if message.from_user.id not in admins:
    await message.answer("У вас нет прав администратора.")
    return

  args = message.text.split()
  if len(args) < 3 or not args[1].isdigit() or not args[2].isdigit():
    await message.answer("Использование: /bind ID_отправителя ID_получателя")
    return

  from_id = int(args[1])
  to_id = int(args[2])
  forward_pairs[from_id] = to_id
  await message.answer(f"Связь создана: сообщения от {from_id} идут к {to_id}.")


@router.message(F.from_user.id.in_(forward_pairs))
async def forward_user_message(message: Message):
  target_id = forward_pairs[message.from_user.id]
  await message.copy_to(target_id)


@router.message(F.from_user.id.not_in_(forward_pairs), ~F.from_user.id.in_(admins))
async def handle_other_messages(message: Message):
  # Сообщения от людей, для которых не настроена пересылка
  pass


async def main():
  dp.include_router(router)
  await bot.delete_webhook(drop_pending_updates=True)
  await dp.start_polling(bot)


if __name__ == "__main__":
  logging.basicConfig(level=logging.INFO)
  asyncio.run(main())