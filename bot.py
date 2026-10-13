import os
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from supabase import create_client, Client
from dotenv import load_dotenv

# Настраиваем логирование
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def vibes_word(count):
    """Склоняет слово 'вайб'."""
    count = abs(count) % 100
    n1 = count % 10
    if count > 10 and count < 20: return "вайбов"
    if n1 > 1 and n1 < 5: return "вайба"
    if n1 == 1: return "вайб"
    return "вайбов"

# --- КЛАВИАТУРЫ ---
main_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✨ Получить вайб")],
        [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="📋 Посмотреть анкету")]
    ],
    resize_keyboard=True
)

# Состояния
class Registration(StatesGroup):
    waiting_for_first_name = State()
    waiting_for_last_name = State()

# --- БАЗА ДАННЫХ ---
def get_user(tg_id: int):
    response = supabase.table("users").select("*").eq("tg_id", tg_id).execute()
    return response.data[0] if response.data else None

def add_user(tg_id: int, first_name: str, last_name: str):
    response = supabase.table("users").insert({
        "tg_id": tg_id, "first_name": first_name, "last_name": last_name, "balance": 1
    }).execute()
    return response.data[0] if response.data else None

def update_balance(tg_id: int, amount: int):
    user = get_user(tg_id)
    if user:
        new_balance = user["balance"] + amount
        supabase.table("users").update({"balance": new_balance}).eq("tg_id", tg_id).execute()
        return new_balance
    return None

# --- ОБРАБОТЧИКИ (ВАЖНО: СПЕЦИФИЧНЫЕ СВЕРХУ, ОБЩИЕ СНИЗУ) ---

# 1. Команда /start
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    logger.info(f"Команда /start от {message.from_user.id}")
    tg_id = message.from_user.id
    user = get_user(tg_id)
    
    if user:
        await message.answer(
            f"С возвращением, {user['first_name']}! 👋\n"
            f"Твой баланс: {user['balance']} {vibes_word(user['balance'])}",
            reply_markup=main_menu_keyboard
        )
    else:
        await state.set_state(Registration.waiting_for_first_name)
        await message.answer("Привет! Давай познакомимся. Как твое имя?")

# 2. Обработчики кнопок меню
@dp.message(F.text == "✨ Получить вайб")
async def handle_claim_vibe(message: Message):
    tg_id = message.from_user.id
    user = get_user(tg_id)
    if user:
        new_balance = update_balance(tg_id, 1)
        await message.answer(f"Тебе +1 вайб! ✨\nТвой баланс: {new_balance} {vibes_word(new_balance)}")
    else:
        await message.answer("Сначала зарегистрируйся через /start")

@dp.message(F.text == "💰 Баланс")
async def handle_balance(message: Message):
    tg_id = message.from_user.id
    user = get_user(tg_id)
    if user:
        await message.answer(f"Твой баланс: {user['balance']} {vibes_word(user['balance'])}")
    else:
        await message.answer("Сначала зарегистрируйся через /start")

@dp.message(F.text == "📋 Посмотреть анкету")
async def handle_survey(message: Message):
    await message.answer("Раздел 'Анкета' в разработке. Скоро здесь появится возможность заполнить данные о себе!", reply_markup=main_menu_keyboard)

# 3. Общий обработчик (Fallback) - в самом конце!
@dp.message()
async def catch_all_messages(message: Message, state: FSMContext):
    logger.info(f"Получено сообщение: '{message.text}' от {message.from_user.id}")
    
    current_state = await state.get_state()
    
    if current_state == Registration.waiting_for_first_name:
        await state.update_data(first_name=message.text)
        await message.answer("Отлично! А теперь фамилия?")
        await state.set_state(Registration.waiting_for_last_name)
        
    elif current_state == Registration.waiting_for_last_name:
        tg_id = message.from_user.id
        data = await state.get_data()
        first_name = data.get("first_name", "Неизвестно")
        last_name = message.text
        
        user = add_user(tg_id, first_name, last_name)
        if user:
            await state.clear()
            await message.answer(
                f"Приятно познакомиться, {first_name}! 🎉\n"
                f"Ты получил 1 вайб на старт.\n"
                f"Твой баланс: {user['balance']} {vibes_word(user['balance'])}",
                reply_markup=main_menu_keyboard
            )
        else:
            await message.answer("Ошибка при регистрации. Попробуй /start")
    else:
        await message.answer("Я тебя не понимаю. Используй кнопки меню 👇", reply_markup=main_menu_keyboard)

# 4. Обработчик колбэков (на будущее)
@dp.callback_query()
async def catch_all_callbacks(callback: CallbackQuery):
    logger.info(f"Получен колбэк: '{callback.data}'")
    await callback.answer()

# --- ЗАПУСК ---
async def main():
    logger.info(" Бот запускается...")
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("✅ Вебхук удалён, запускаем polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())