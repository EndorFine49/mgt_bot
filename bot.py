import os
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from supabase import create_client, Client
from dotenv import load_dotenv

# Загружаем переменные из .env
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") # Здесь должен быть service_role ключ
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]

# Инициализируем бота и Supabase
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- КЛАВИАТУРЫ ---

# Главное меню (Reply-кнопки, всегда под полем ввода)
main_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🪙 Получить мосгортугрик")],
        [KeyboardButton(text="💰 Баланс"), KeyboardButton(text=" Посмотреть анкету")]
    ],
    resize_keyboard=True
)

# Inline-кнопка (внутри сообщения)
claim_coin_inline = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="🪙 Получить мосгортугрик", callback_data="claim_coin")]
    ]
)

# Состояния для регистрации
class Registration(StatesGroup):
    waiting_for_first_name = State()
    waiting_for_last_name = State()

# --- РАБОТА С БАЗОЙ ДАННЫХ ---

def get_user(tg_id: int):
    response = supabase.table("users").select("*").eq("tg_id", tg_id).execute()
    if response.data:
        return response.data[0]
    return None

def add_user(tg_id: int, first_name: str, last_name: str):
    response = supabase.table("users").insert({
        "tg_id": tg_id,
        "first_name": first_name,
        "last_name": last_name,
        "balance": 1
    }).execute()
    return response.data[0] if response.data else None

def update_balance(tg_id: int, amount: int):
    user = get_user(tg_id)
    if user:
        new_balance = user["balance"] + amount
        supabase.table("users").update({"balance": new_balance}).eq("tg_id", tg_id).execute()
        return new_balance
    return None

# --- ОБРАБОТЧИКИ ---

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    user = get_user(tg_id)
    
    if user:
        await message.answer(
            f"С возвращением, {user['first_name']}! \n"
            f"Твой баланс: {user['balance']} мосгортугриков ",
            reply_markup=main_menu_keyboard
        )
    else:
        await message.answer("Привет! Давай познакомимся. Как твое имя?")
        await state.set_state(Registration.waiting_for_first_name)

@dp.message(Registration.waiting_for_first_name)
async def process_first_name(message: Message, state: FSMContext):
    await state.update_data(first_name=message.text)
    await message.answer("Отлично! А теперь фамилия?")
    await state.set_state(Registration.waiting_for_last_name)

@dp.message(Registration.waiting_for_last_name)
async def process_last_name(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    data = await state.get_data()
    first_name = data["first_name"]
    last_name = message.text
    
    user = add_user(tg_id, first_name, last_name)
    
    if user:
        await state.clear()
        await message.answer(
            f"Приятно познакомиться, {first_name}! \n"
            f"Ты зарегистрирован и получил 1 мосгортугрик на старт.\n"
            f"Твой баланс: {user['balance']} мосгортугриков ",
            reply_markup=main_menu_keyboard
        )
    else:
        await message.answer("Произошла ошибка при регистрации. Попробуй ещё раз через /start")

@dp.message(F.text == " Баланс")
async def cmd_balance_button(message: Message):
    tg_id = message.from_user.id
    user = get_user(tg_id)
    if user:
        await message.answer(f"Твой баланс: {user['balance']} мосгортугриков ")
    else:
        await message.answer("Сначала зарегистрируйся через /start")

@dp.message(F.text == "📋 Посмотреть анкету")
async def view_survey(message: Message):
    await message.answer("Раздел 'Анкета' находится в разработке. Скоро здесь появится возможность заполнить данные о себе!")

# Обработка нажатия на Inline-кнопку "Получить мосгортугрик"
@dp.callback_query(F.data == "claim_coin")
async def claim_coin_callback(callback: CallbackQuery):
    tg_id = callback.from_user.id
    user = get_user(tg_id)
    
    if user:
        new_balance = update_balance(tg_id, 1)
        await callback.message.answer(
            f"Ты получил 1 мосгортугрик! \n"
            f"Твой текущий баланс: {new_balance} мосгортугриков "
        )
        # Убираем "часики" на кнопке после нажатия
        await callback.answer() 
    else:
        await callback.answer("Сначала зарегистрируйся через /start", show_alert=True)

# Обработка обычной текстовой кнопки "Получить мосгортугрик"
@dp.message(F.text == " Получить мосгортугрик")
async def claim_coin_text(message: Message):
    tg_id = message.from_user.id
    user = get_user(tg_id)
    
    if user:
        new_balance = update_balance(tg_id, 1)
        await message.answer(
            f"Ты получил 1 мосгортугрик! \n"
            f"Твой текущий баланс: {new_balance} мосгортугриков "
        )
    else:
        await message.answer("Сначала зарегистрируйся через /start")

# --- ЗАПУСК БОТА ---

async def main():
    print("Бот запущен! ")
    # Удаляем старый вебхук, если он вдруг остался, и запускаем polling
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())