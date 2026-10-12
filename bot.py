import os
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

class Registration(StatesGroup):
    waiting_for_first_name = State()
    waiting_for_last_name = State()


# --- РАБОТА С БАЗОЙ ДАННЫХ ---

def get_user(tg_id: int):
    """Получает пользователя из базы по tg_id."""
    response = supabase.table("users").select("*").eq("tg_id", tg_id).execute()
    if response.data:
        return response.data[0]
    return None


def add_user(tg_id: int, first_name: str, last_name: str):
    """Добавляет нового пользователя в базу."""
    response = supabase.table("users").insert({
        "tg_id": tg_id,
        "first_name": first_name,
        "last_name": last_name,
        "balance": 1  
    }).execute()
    return response.data[0] if response.data else None


def update_balance(tg_id: int, amount: int):
    """Изменяет баланс пользователя (amount может быть отрицательным)."""
    user = get_user(tg_id)
    if user:
        new_balance = user["balance"] + amount
        supabase.table("users").update({"balance": new_balance}).eq("tg_id", tg_id).execute()
        return new_balance
    return None


# --- ОБРАБОТЧИКИ КОМАНД ---

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """Обработка команды /start."""
    tg_id = message.from_user.id
    
    # Проверяем, есть ли пользователь в базе
    user = get_user(tg_id)
    
    if user:
        # Пользователь уже зарегистрирован
        await message.answer(
            f"С возвращением, {user['first_name']}! \n"
            f"Твой баланс: {user['balance']} мосгортугриков "
        )
    else:
        # Новый пользователь — начинаем регистрацию
        await message.answer("Привет! Давай познакомимся. Как твое имя?")
        await state.set_state(Registration.waiting_for_first_name)


@dp.message(Registration.waiting_for_first_name)
async def process_first_name(message: Message, state: FSMContext):
    """Сохраняем имя и спрашиваем фамилию."""
    await state.update_data(first_name=message.text)
    await message.answer("Отлично! А теперь фамилия?")
    await state.set_state(Registration.waiting_for_last_name)


@dp.message(Registration.waiting_for_last_name)
async def process_last_name(message: Message, state: FSMContext):
    """Сохраняем фамилию, создаём пользователя и начисляем баллы."""
    tg_id = message.from_user.id
    data = await state.get_data()
    first_name = data["first_name"]
    last_name = message.text
    
    # Создаём пользователя в базе (сразу с балансом 1)
    user = add_user(tg_id, first_name, last_name)
    
    if user:
        await state.clear()
        await message.answer(
            f"Приятно познакомиться, {first_name}! \n"
            f"Ты зарегистрирован и получил 1 мосгортугрик на старт.\n"
            f"Твой баланс: {user['balance']} мосгортугриков "
        )
    else:
        await message.answer("Произошла ошибка при регистрации. Попробуй ещё раз через /start")


@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    """Проверка баланса."""
    tg_id = message.from_user.id
    user = get_user(tg_id)
    
    if user:
        await message.answer(f"Твой баланс: {user['balance']} мосгортугриков ")
    else:
        await message.answer("Сначала зарегистрируйся через /start")


# --- ЗАПУСК БОТА ---

async def main():
    print("Бот запущен! ")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())