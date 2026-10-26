import os
import logging
import re
import asyncio
import random
import io
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile, InputSticker, InputMediaPhoto
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from supabase import create_client, Client
from dotenv import load_dotenv
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

# --- НАСТРОЙКИ И ЛОГИРОВАНИЕ ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]
BOT_USERNAME = os.getenv("BOT_USERNAME")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def vibes_word(count):
    count = abs(count) % 100
    n1 = count % 10
    if count > 10 and count < 20: return "вайбов"
    if n1 > 1 and n1 < 5: return "вайба"
    if n1 == 1: return "вайб"
    return "вайбов"

# --- КЛАВИАТУРЫ ---
def get_main_keyboard(tg_id: int) -> ReplyKeyboardMarkup:
    """Возвращает главное меню с учетом прав пользователя."""
    rows = [
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="🛒 Магазин")],
        [KeyboardButton(text="📋 Задания")]
    ]
    if tg_id in ADMIN_IDS:
        rows.append([KeyboardButton(text="🛠 Админ-панель")])
    
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

TEAM_OPTIONS = ["Джем", "Драйв", "Сияй", "Начало", "Big Family", "Полис", "Крым Team", "Не состою в команде"]

def get_team_keyboard():
    keyboard = []
    for i in range(0, len(TEAM_OPTIONS), 2):
        row = [InlineKeyboardButton(text=TEAM_OPTIONS[i], callback_data=f"team_{TEAM_OPTIONS[i]}")]
        if i + 1 < len(TEAM_OPTIONS):
            row.append(InlineKeyboardButton(text=TEAM_OPTIONS[i+1], callback_data=f"team_{TEAM_OPTIONS[i+1]}"))
        keyboard.append(row)
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# --- СОСТОЯНИЯ (FSM) ---
class Registration(StatesGroup):
    waiting_for_first_name = State()
    waiting_for_last_name = State()

class Survey(StatesGroup):
    waiting_for_team = State()
    waiting_for_shifts = State()
    waiting_for_birthday = State()
    waiting_for_study_group = State()
    waiting_for_hobbies = State()
    editing_team = State()
    editing_shifts = State()
    editing_birthday = State()
    editing_study_group = State()
    editing_hobbies = State()

class Broadcast(StatesGroup):
    waiting_for_text = State()
    waiting_for_button_choice = State()
    waiting_for_button_text = State()
    waiting_for_button_url = State()
# --- БАЗА ДАННЫХ ---
def get_user(tg_id: int):
    response = supabase.table("users").select("*").eq("tg_id", tg_id).execute()
    return response.data[0] if response.data else None

def get_all_users():
    response = supabase.table("users").select("tg_id").execute()
    return response.data if response.data else []

def add_user(tg_id: int, first_name: str, last_name: str):
    response = supabase.table("users").insert({
        "tg_id": tg_id, "first_name": first_name, "last_name": last_name, "balance": 1, "survey_completed": False
    }).execute()
    return response.data[0] if response.data else None

def update_user(tg_id: int, data: dict):
    supabase.table("users").update(data).eq("tg_id", tg_id).execute()

def update_balance(tg_id: int, amount: int):
    user = get_user(tg_id)
    if user:
        new_balance = user["balance"] + amount
        supabase.table("users").update({"balance": new_balance}).eq("tg_id", tg_id).execute()
        return new_balance
    return None

def add_vibes(tg_id: int, amount: int):
    """Единая функция начисления/списания вайбов пользователю и его команде."""
    user = get_user(tg_id)
    if not user:
        return None, None
    
    # Обновляем баланс пользователя (всегда)
    new_user_balance = user["balance"] + amount
    supabase.table("users").update({"balance": new_user_balance}).eq("tg_id", tg_id).execute()
    
    # Обновляем баланс команды ТОЛЬКО при положительном начислении
    team_name = user.get("team")
    team_updated = False
    if amount > 0 and team_name and team_name != "Не состою в команде":
        team_res = supabase.table("teams").select("balance").eq("name", team_name).execute()
        if team_res.data:
            new_team_balance = team_res.data[0]["balance"] + amount
            supabase.table("teams").update({"balance": new_team_balance}).eq("name", team_name).execute()
            team_updated = True
            
    return new_user_balance, team_name if team_updated else None

def get_teams_rating():
    """Получает рейтинг команд из отдельной таблицы."""
    response = supabase.table("teams").select("name, balance").order("balance", desc=True).execute()
    return response.data if response.data else []

def get_user_inventory(user_tg_id: int):
    response = supabase.table("user_inventory").select("item_id").eq("user_tg_id", user_tg_id).execute()
    return [item['item_id'] for item in response.data] if response.data else []

def buy_item(user_tg_id: int, item_id: int, price: int):
    """Покупает предмет, если хватает вайбов."""
    user = get_user(user_tg_id)
    if user['balance'] < price:
        return False, "Не хватает вайбов!"
    
    add_vibes(user_tg_id, -price)
    
    supabase.table("user_inventory").insert({
        "user_tg_id": user_tg_id,
        "item_id": item_id
    }).execute()
    return True, "Покупка успешна!"

# --- ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ ---
def generate_pet_image(user_tg_id: int):
    """Генерирует картинку питомца с надетой одеждой в правильном порядке."""
    user = get_user(user_tg_id)
    pet_id = user.get('equipped_pet_id')
    if not pet_id:
        return None
    
    # Открываем базовое изображение питомца
    pet_item = supabase.table("items").select("filename").eq("id", pet_id).execute().data[0]
    base_image = Image.open(f"assets/pets/{pet_item['filename']}").convert("RGBA")
    
    # Получаем ID надетых предметов
    equipped_ids = [
        user.get('equipped_bottom_id'),
        user.get('equipped_top_id'),
        user.get('equipped_head_id')
    ]
    equipped_ids = [eid for eid in equipped_ids if eid]
    
    if not equipped_ids:
        return base_image
    
    # Получаем filenames в том же порядке (bottom → top → head)
    clothes_items = supabase.table("items").select("id", "filename").in_("id", equipped_ids).execute().data
    
    # Создаём словарь для быстрого доступа
    items_dict = {item['id']: item['filename'] for item in clothes_items}
    
    # Накладываем одежду в правильном порядке: низ → верх → голова
    for equipped_id in equipped_ids:
        if equipped_id in items_dict:
            cloth_image = Image.open(f"assets/clothes/{items_dict[equipped_id]}").convert("RGBA")
            base_image = Image.alpha_composite(base_image, cloth_image)
    
    return base_image

def prepare_sticker_image(base_image: Image.Image, text: str):
    img = base_image.resize((512, 512), Image.Resampling.LANCZOS).convert("RGBA")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("assets/font.ttf", 50)
    except IOError:
        font = ImageFont.load_default()
        
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    text_width, text_height = right - left, bottom - top
    x, y = (512 - text_width) / 2, 512 - text_height - 40 
    
    draw.rounded_rectangle([x - 20, y - 20, x + text_width + 20, y + text_height + 20], radius=20, fill=(0, 0, 0, 180))
    draw.text((x, y), text, font=font, fill="white")
    return img

# --- ОБРАБОТЧИКИ: РЕГИСТРАЦИЯ И ВЫБОР ПИТОМЦА ---
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    tg_id = message.from_user.id
    user = get_user(tg_id)
    if user:
        await message.answer(f"С возвращением, {user['first_name']}! 👋\nТвой баланс: {user['balance']} {vibes_word(user['balance'])}", reply_markup=get_main_keyboard(tg_id))
    else:
        await state.set_state(Registration.waiting_for_first_name)
        await message.answer("Привет! Давай познакомимся. Как твое имя?")

@dp.message(Registration.waiting_for_first_name)
async def process_first_name(message: Message, state: FSMContext):
    await state.update_data(first_name=message.text)
    await message.answer("Отлично! А теперь фамилия?")
    await state.set_state(Registration.waiting_for_last_name)

@dp.message(Registration.waiting_for_last_name)
async def process_last_name(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    data = await state.get_data()
    user = add_user(tg_id, data.get("first_name", "Неизвестно"), message.text)
    if user:
        await state.clear()
        await show_pet_selection(message)
    else:
        await message.answer("Ошибка при регистрации. Попробуй /start")

async def show_pet_selection(target):
    pets = supabase.table("items").select("*").eq("type", "pet").execute().data
    kb = []
    for i in range(0, len(pets), 2):
        row = [InlineKeyboardButton(text=pets[i]['name'], callback_data=f"preview_pet_{pets[i]['id']}")]
        if i + 1 < len(pets): row.append(InlineKeyboardButton(text=pets[i+1]['name'], callback_data=f"preview_pet_{pets[i+1]['id']}"))
        kb.append(row)
    await target.answer("🎨 Выбери цвет своего стикмана:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("preview_pet_"))
async def preview_pet(callback: CallbackQuery):
    pet_id = int(callback.data.replace("preview_pet_", ""))
    pet_item = supabase.table("items").select("filename").eq("id", pet_id).execute().data[0]
    img = Image.open(f"assets/pets/{pet_item['filename']}").convert("RGBA")
    
    bio = io.BytesIO(); img.save(bio, format='PNG'); bio.seek(0)
    photo_file = BufferedInputFile(file=bio.read(), filename="preview.png")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Оставить этот", callback_data=f"confirm_pet_{pet_id}")],
        [InlineKeyboardButton(text="🔄 Выбрать другой", callback_data="choose_another_pet")]
    ])
    
    await callback.message.delete() # Удаляем сообщение с кнопками цветов
    await callback.message.answer_photo(photo=photo_file, caption="Отличный выбор! \n\n⚠️ Сменить цвет позже можно будет, но это стоит **10 вайбов**.\n\nПодтвердить выбор?", reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "choose_another_pet")
async def choose_another_pet(callback: CallbackQuery):
    await callback.message.delete() # Удаляем фото
    await show_pet_selection(callback.message) # Снова показываем цвета
    await callback.answer()

@dp.callback_query(F.data.startswith("confirm_pet_"))
async def confirm_pet(callback: CallbackQuery):
    pet_id = int(callback.data.replace("confirm_pet_", ""))
    update_user(callback.from_user.id, {"equipped_pet_id": pet_id})
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Заполнить анкету (+5 вайбов)", callback_data="start_survey")],
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip_survey")]
    ])
    
    # Меняем текст и кнопки на том же фото
    await callback.message.edit_caption(caption="🎉 Твой стикман готов и ждет приключений!\n\nТеперь давай заполним небольшую анкету. Это займет пару минут, а взамен ты получишь **5 вайбов**!", reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "skip_survey")
async def skip_survey(callback: CallbackQuery):
    update_user(callback.from_user.id, {"survey_completed": True})
    await callback.message.edit_caption(caption="Хорошо! Ты всегда можешь вернуться к анкете через главное меню.")
    await callback.message.answer("Главное меню:", reply_markup=get_main_keyboard(callback.from_user.id))
    await callback.answer()

# --- ОБРАБОТЧИКИ МЕНЮ ---
@dp.message(F.text == "👤 Мой профиль")
async def handle_profile(message: Message):
    tg_id = message.from_user.id
    user = get_user(tg_id)
    if not user:
        return await message.answer("Сначала зарегистрируйся через /start", reply_markup=get_main_keyboard(tg_id))
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Баланс", callback_data="profile_balance"),
         InlineKeyboardButton(text="🐾 Мой питомец", callback_data="profile_pet")]
    ])
    await message.answer("👤 **Мой профиль**\n\nВыбери раздел:", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "profile_balance")
async def profile_balance(callback: CallbackQuery):
    tg_id = callback.from_user.id
    user = get_user(tg_id)
    if user:
        text = f"💰 Твой баланс: {user['balance']} {vibes_word(user['balance'])}"
        
        # Показываем рейтинг команд
        teams_rating = get_teams_rating()
        if teams_rating:
            text += "\n\n🏆 **Рейтинг команд:**\n"
            for i, team in enumerate(teams_rating, 1):
                medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
                text += f"{medal} {team['name']}: {team['balance']} {vibes_word(team['balance'])}\n"
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✨ Получить вайб", callback_data="claim_vibe_inline")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="profile_back")]
        ])
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "profile_pet")
async def profile_pet(callback: CallbackQuery):
    tg_id = callback.from_user.id
    image = generate_pet_image(tg_id)
    if image:
        bio = io.BytesIO()
        image.save(bio, format='PNG')
        bio.seek(0)
        photo_file = BufferedInputFile(file=bio.read(), filename="my_pet.png")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎨 Создать стикерпак (20 вайбов)", callback_data="create_sticker_pack")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="profile_back_image")]
        ])
        
        await callback.message.edit_media(
            media=InputMediaPhoto(media=photo_file, caption="Твой уникальный стикман! 🎨"),
            reply_markup=kb
        )
    else:
        await callback.answer("Сначала выбери базового питомца в магазине!", show_alert=True)
    await callback.answer()

@dp.callback_query(F.data == "profile_back")
async def profile_back(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Баланс", callback_data="profile_balance"),
         InlineKeyboardButton(text="🐾 Мой питомец", callback_data="profile_pet")]
    ])
    await callback.message.edit_text(
        "👤 **Мой профиль**\n\nВыбери раздел:",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "profile_back_image")
async def profile_back(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Баланс", callback_data="profile_balance"),
         InlineKeyboardButton(text="🐾 Мой питомец", callback_data="profile_pet")]
    ])
    await callback.message.delete()
    await callback.message.answer(
        "👤 **Мой профиль**\n\nВыбери раздел:",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "claim_vibe_inline")
async def claim_vibe_inline(callback: CallbackQuery):
    tg_id = callback.from_user.id
    user = get_user(tg_id)
    if user:
        new_balance, team_name = add_vibes(tg_id, 1)
        text = f"Тебе +1 вайб! ✨\nТвой баланс: {new_balance} {vibes_word(new_balance)}"
        if team_name:
            text += f"\n\n🎯 Твоя команда **{team_name}** тоже получила +1 вайб!"
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✨ Получить ещё", callback_data="claim_vibe_inline")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="profile_back")]
        ])
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

@dp.message(F.text == "📋 Задания")
async def handle_tasks(message: Message):
    tg_id = message.from_user.id
    user = get_user(tg_id)
    if not user:
        return await message.answer("Сначала зарегистрируйся через /start", reply_markup=get_main_keyboard(tg_id))
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Получить вайб", callback_data="claim_vibe_inline")],
        [InlineKeyboardButton(text="🎰 Рулетка", callback_data="roulette_menu")]
    ])
    await message.answer("📋 **Задания**\n\nВыполняй задания и получай вайбы!", reply_markup=kb, parse_mode="Markdown")

@dp.message(F.text == "🛠 Админ-панель")
async def show_admin_panel(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return await message.answer("У тебя нет прав администратора.")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Рассылки", callback_data="admin_broadcast_menu"),
         InlineKeyboardButton(text="👥 Команды", callback_data="admin_teams_menu")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="admin_exit")]
    ])
    await message.answer("🛠 **Админ-панель**\n\nВыбери раздел:", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "admin_broadcast_menu")
async def admin_broadcast_menu(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("Нет доступа", show_alert=True)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Всем пользователям", callback_data="broadcast_start")],
        [InlineKeyboardButton(text="🧪 Тест (только мне)", callback_data="broadcast_test")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]
    ])
    await callback.message.edit_text("📢 **Рассылки**\n\nВыбери режим:", reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "admin_teams_menu")
async def admin_teams_menu(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("Нет доступа", show_alert=True)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏆 Рейтинг команд", callback_data="admin_teams_rating")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]
    ])
    await callback.message.edit_text("👥 **Команды**\n\nВыбери действие:", reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "admin_teams_rating")
async def admin_teams_rating(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("Нет доступа", show_alert=True)
    
    teams_rating = get_teams_rating()
    if teams_rating:
        text = "🏆 **Рейтинг команд:**\n\n"
        for i, team in enumerate(teams_rating, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            text += f"{medal} {team['name']}: {team['balance']} {vibes_word(team['balance'])}\n"
    else:
        text = "Нет данных о командах."
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_teams_menu")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("Нет доступа", show_alert=True)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Рассылки", callback_data="admin_broadcast_menu"),
         InlineKeyboardButton(text="👥 Команды", callback_data="admin_teams_menu")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="admin_exit")]
    ])
    await callback.message.edit_text("🛠 **Админ-панель**\n\nВыбери раздел:", reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "admin_exit")
async def admin_exit(callback: CallbackQuery):
    await callback.message.edit_text("Вернулся в главное меню 👇")
    await callback.answer()

@dp.callback_query(F.data.in_(["broadcast_start", "broadcast_test"]))
async def broadcast_mode(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("Нет доступа", show_alert=True)
    
    is_test = callback.data == "broadcast_test"
    await state.update_data(is_test=is_test)
    await state.set_state(Broadcast.waiting_for_text)
    
    if is_test:
        text = "🧪 **Тестовая рассылка**\n\nПришли текст сообщения, оно придёт только тебе.\n\nОтмена — /cancel"
    else:
        text = "📢 **Режим рассылки**\n\nПришли текст сообщения, который нужно разослать всем пользователям.\n\n_Поддерживается Markdown: *жирный*, _курсив_, `код`_\n\nОтмена — /cancel"
        
    await callback.message.edit_text(text, parse_mode="Markdown")
    await callback.answer()

# --- ОБРАБОТЧИКИ: РАССЫЛКА (Продолжение) ---

@dp.message(Broadcast.waiting_for_text)
async def process_broadcast_text(message: Message, state: FSMContext):
    await state.update_data(text=message.text)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, добавить", callback_data="broadcast_btn_yes"), 
         InlineKeyboardButton(text="Нет, без кнопки", callback_data="broadcast_btn_no")]
    ])
    await message.answer("Добавить под сообщением Inline-кнопку (например, 'Перейти к анкете')?", reply_markup=kb)
    await state.set_state(Broadcast.waiting_for_button_choice)

@dp.callback_query(F.data.in_(["broadcast_btn_yes", "broadcast_btn_no"]))
async def process_button_choice(callback: CallbackQuery, state: FSMContext):
    if callback.data == "broadcast_btn_yes":
        await callback.message.edit_text("Отлично! Напиши текст для кнопки (например: 'Открыть анкету'):")
        await state.set_state(Broadcast.waiting_for_button_text)
    else:
        await callback.message.edit_text("Понял, запускаю рассылку без кнопки...")
        is_test = (await state.get_data()).get("is_test", False)
        await process_broadcast(callback.message.chat.id, state, is_test)
    await callback.answer()

@dp.message(Broadcast.waiting_for_button_text)
async def process_button_text(message: Message, state: FSMContext):
    await state.update_data(button_text=message.text)
    await message.answer("Теперь пришли ссылку (URL), куда должна вести кнопка (например, https://t.me/your_bot):")
    await state.set_state(Broadcast.waiting_for_button_url)

@dp.message(Broadcast.waiting_for_button_url)
async def process_button_url(message: Message, state: FSMContext):
    if not message.text.startswith("http"):
        return await message.answer("Ссылка должна начинаться с http:// или https://. Попробуй еще раз:")
    
    await state.update_data(button_url=message.text)
    await message.answer("Принял! Запускаю рассылку с кнопкой...")
    is_test = (await state.get_data()).get("is_test", False)
    await process_broadcast(message.chat.id, state, is_test)

async def send_broadcast_message(chat_id: int, text: str, button_text: str = None, button_url: str = None):
    """Отправляет одно сообщение с учетом кнопок."""
    keyboard = None
    if button_text and button_url:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=button_text, url=button_url)
        ]])
    await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="Markdown")

async def process_broadcast(chat_id: int, state: FSMContext, is_test: bool = False):
    """Логика массовой отправки."""
    data = await state.get_data()
    text = data.get("text", "")
    btn_text = data.get("button_text")
    btn_url = data.get("button_url")
    
    await state.clear()
    
    if is_test:
        await bot.send_message(chat_id, f"🧪 *ТЕСТОВАЯ РАССЫЛКА*\n\n{text}", parse_mode="Markdown", 
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=btn_text, url=btn_url)]]) if btn_text else None)
        await bot.send_message(chat_id, "✅ Тестовое сообщение отправлено!")
        return

    await bot.send_message(chat_id, "🚀 Запускаю рассылку... Это может занять некоторое время.")
    
    users = get_all_users()
    success_count = 0
    error_count = 0
    
    for user in users:
        try:
            await send_broadcast_message(user['tg_id'], text, btn_text, btn_url)
            success_count += 1
            await asyncio.sleep(0.05) # Задержка 50мс для безопасности
        except Exception as e:
            error_count += 1
            logger.error(f"Ошибка отправки пользователю {user['tg_id']}: {e}")
            
    report = f"✅ Рассылка завершена!\n\n📊 Статистика:\n• Доставлено: {success_count}\n• Ошибок: {error_count}\n• Всего пользователей: {len(users)}"
    await bot.send_message(chat_id, report)

# --- ОБРАБОТЧИКИ: РУЛЕТКА ---

@dp.callback_query(F.data == "roulette_menu")
async def roulette_menu(callback: CallbackQuery):
    tg_id = callback.from_user.id
    user = get_user(tg_id)
    
    today_str = datetime.now().date().isoformat()
    free_available = user.get('free_spin_date') != today_str
    paid_available = user.get('paid_spin_date') != today_str
    
    text = "🎰 **Рулетка**\n\n"
    text += f"💰 Твой баланс: {user['balance']} {vibes_word(user['balance'])}\n\n"
    
    kb = []
    
    if free_available:
        kb.append([InlineKeyboardButton(text="🆓 Бесплатная попытка (1-5 вайбов)", callback_data="roulette_free")])
    else:
        kb.append([InlineKeyboardButton(text="✅ Бесплатная попытка использована", callback_data="roulette_free_used")])
    
    if paid_available:
        kb.append([InlineKeyboardButton(text="💎 Платная попытка за 5 вайбов (1-15 вайбов)", callback_data="roulette_paid")])
    else:
        kb.append([InlineKeyboardButton(text="✅ Платная попытка использована", callback_data="roulette_paid_used")])
    
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="tasks_back")])
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "roulette_free_used")
async def roulette_free_used(callback: CallbackQuery):
    await callback.answer("Бесплатная попытка уже использована сегодня! Возвращайся завтра 🎰", show_alert=True)

@dp.callback_query(F.data == "roulette_paid_used")
async def roulette_paid_used(callback: CallbackQuery):
    await callback.answer("Платная попытка уже использована сегодня! Возвращайся завтра 💎", show_alert=True)

@dp.callback_query(F.data == "roulette_free")
async def roulette_free(callback: CallbackQuery):
    tg_id = callback.from_user.id
    user = get_user(tg_id)
    
    today_str = datetime.now().date().isoformat()
    if user.get('free_spin_date') == today_str:
        return await callback.answer("Бесплатная попытка уже использована сегодня!", show_alert=True)
    
    update_user(tg_id, {"free_spin_date": today_str})

    dice_msg = await callback.message.answer("🎰 Крутим рулетку...")
    dice_result = await bot.send_dice(chat_id=tg_id, emoji="🎰")

    await dice_msg.delete()
    await asyncio.sleep(2)
    
    dice_value = dice_result.dice.value
    
    # Генерируем выигрыш от 1 до 50 на основе результата dice
    win_amount = (dice_value % 5) + 1
    
    new_balance, team_name = add_vibes(tg_id, win_amount)
    
    text = f"🎉 **Поздравляем!**\n\n"
    text += f"Ты выиграл **{win_amount} {vibes_word(win_amount)}**!\n\n"
    text += f"💰 Твой новый баланс: {new_balance} {vibes_word(new_balance)}"
    
    if team_name:
        text += f"\n\n🎯 Твоя команда **{team_name}** тоже получила +{win_amount} {vibes_word(win_amount)}!"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎰 Вернуться к рулетке", callback_data="roulette_menu")],
        [InlineKeyboardButton(text="⬅️ Назад к заданиям", callback_data="tasks_back")]
    ])
    
    await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "roulette_paid")
async def roulette_paid(callback: CallbackQuery):
    tg_id = callback.from_user.id
    user = get_user(tg_id)
    
    today_str = datetime.now().date().isoformat()
    if user.get('paid_spin_date') == today_str:
        return await callback.answer("Платная попытка уже использована сегодня!", show_alert=True)
    
    if user['balance'] < 5:
        return await callback.answer("Не хватает вайбов! Нужно 5 🪙", show_alert=True)

    new_balance_after_bet, _ = add_vibes(tg_id, -5)
    
    update_user(tg_id, {"paid_spin_date": today_str})

    dice_msg = await callback.message.answer("💎 Крутим рулетку...")
    dice_result = await bot.send_dice(chat_id=tg_id, emoji="🎰")
    
    await dice_msg.delete()
    await asyncio.sleep(1)
    dice_value = dice_result.dice.value

    win_amount = (dice_value % 15) + 1
    final_balance, team_name = add_vibes(tg_id, win_amount)
    net_win = win_amount - 10
    
    text = f"💎 **Поздравляем!**\n\n"
    text += f"Ставка: 5 вайбов\n"
    text += f"Выигрыш: {win_amount} {vibes_word(win_amount)}\n"
    text += f"Чистый выигрыш: **{net_win} {vibes_word(net_win)}**\n\n"
    text += f"💰 Твой новый баланс: {final_balance} {vibes_word(final_balance)}"
    
    if team_name:
        text += f"\n\n🎯 Твоя команда **{team_name}** тоже получила +{win_amount} {vibes_word(win_amount)}!"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Вернуться к рулетке", callback_data="roulette_menu")],
        [InlineKeyboardButton(text="⬅️ Назад к заданиям", callback_data="tasks_back")]
    ])
    
    await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "tasks_back")
async def tasks_back(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Получить вайб", callback_data="claim_vibe_inline")],
        [InlineKeyboardButton(text="🎰 Рулетка", callback_data="roulette_menu")]
    ])
    await callback.message.edit_text("📋 **Задания**\n\nВыполняй задания и получай вайбы!", reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

# --- ОБРАБОТЧИКИ: АНКЕТА ---
@dp.callback_query(F.data == "start_survey")
async def start_survey(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_caption(caption="Отлично! В какой вожатской команде ты состоишь?")
    await callback.message.answer("Выбери команду:", reply_markup=get_team_keyboard())
    await state.set_state(Survey.waiting_for_team)
    await callback.answer()

@dp.callback_query(Survey.waiting_for_team, F.data.startswith("team_"))
async def process_team(callback: CallbackQuery, state: FSMContext):
    await state.update_data(team=callback.data.replace("team_", ""))
    await callback.message.answer("Сколько смен ты отработал всего? Напиши число, например: 3")
    await state.set_state(Survey.waiting_for_shifts)
    await callback.answer()

@dp.message(Survey.waiting_for_shifts)
async def process_shifts(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Введи число.")
    await state.update_data(experience=int(message.text))
    await message.answer("Теперь укажи дату рождения в формате ДД.ММ.ГГГГ")
    await state.set_state(Survey.waiting_for_birthday)

@dp.message(Survey.waiting_for_birthday)
async def process_birthday(message: Message, state: FSMContext):
    if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", message.text): return await message.answer("Неверный формат (ДД.ММ.ГГГГ).")
    await state.update_data(birthday=message.text)
    await message.answer("Укажи свою группу обучения (или 'Не помню'):")
    await state.set_state(Survey.waiting_for_study_group)

@dp.message(Survey.waiting_for_study_group)
async def process_study_group(message: Message, state: FSMContext):
    await state.update_data(study_group=message.text)
    await message.answer("Последний вопрос! Расскажи кратко о своих хобби или талантах:")
    await state.set_state(Survey.waiting_for_hobbies)

@dp.message(Survey.waiting_for_hobbies)
async def process_hobbies(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    data = await state.get_data()
    update_user(tg_id, {
        "team": data.get("team"), "experience": data.get("experience"), "birthday": data.get("birthday"),
        "study_group": data.get("study_group"), "hobbies": message.text, "survey_completed": True
    })
    
    user = get_user(tg_id)
    new_balance, team_name = add_vibes(tg_id, 5)
    
    text = (f"🎉 Анкета успешно заполнена! Спасибо!\n"
            f"Тебе начислено **5 вайбов** за активность.\n"
            f"Твой текущий баланс: {new_balance} {vibes_word(new_balance)}")
    if team_name:
        text += f"\n\n🎯 Твоя команда **{team_name}** тоже получила +5 вайбов!"
    
    await state.clear()
    await message.answer(text, reply_markup=get_main_keyboard(tg_id), parse_mode="Markdown")

@dp.message(F.text == "📋 Посмотреть анкету")
async def handle_survey_menu(message: Message):
    user = get_user(message.from_user.id)
    if not user: return await message.answer("Сначала /start", reply_markup=get_main_keyboard(message.from_user.id))

    if not user.get("survey_completed"):
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📝 Заполнить анкету (+5 вайбов)", callback_data="start_survey")]])
        await message.answer("Ты ещё не заполнил анкету. Это займет пару минут, а взамен **5 вайбов**!", reply_markup=kb)
    else:
        text = (f"📋 **Твоя анкета:**\n\n👥 Команда: {user['team'] or 'Не указано'}\n🔥 Опыт: {user['experience'] or 'Не указано'}\n🎂 ДР: {user['birthday'] or 'Не указано'}\n📚 Группа: {user['study_group'] or 'Не указано'}\n🎨 Хобби: {user['hobbies'] or 'Не указано'}\n\nХочешь изменить?")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Команда", callback_data="edit_team"), InlineKeyboardButton(text="✏️ Опыт", callback_data="edit_shifts")],
            [InlineKeyboardButton(text="✏️ ДР", callback_data="edit_birthday"), InlineKeyboardButton(text="✏️ Группа", callback_data="edit_study_group")],
            [InlineKeyboardButton(text="✏️ Хобби", callback_data="edit_hobbies")]
        ])
        await message.answer(text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("edit_"))
async def start_edit(callback: CallbackQuery, state: FSMContext):
    field = callback.data.replace("edit_", "")
    state_map = {"team": Survey.editing_team, "shifts": Survey.editing_shifts, "birthday": Survey.editing_birthday, "study_group": Survey.editing_study_group, "hobbies": Survey.editing_hobbies}
    prompts = {"team": "Выбери новую команду:", "shifts": "Введи новое кол-во смен:", "birthday": "Новая дата рождения (ДД.ММ.ГГГГ):", "study_group": "Новая группа:", "hobbies": "Новые хобби:"}
    
    if field == "team": await callback.message.answer(prompts[field], reply_markup=get_team_keyboard())
    else: await callback.message.answer(prompts[field])
    await state.set_state(state_map[field])
    await callback.answer()

@dp.callback_query(Survey.editing_team, F.data.startswith("team_"))
async def save_edit_team(callback: CallbackQuery, state: FSMContext):
    update_user(callback.from_user.id, {"team": callback.data.replace("team_", "")})
    await state.clear()
    await callback.message.answer("✅ Команда обновлена!", reply_markup=get_main_keyboard(callback.from_user.id))
    await callback.answer()

@dp.message(Survey.editing_shifts)
async def save_edit_shifts(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Введи число.")
    update_user(message.from_user.id, {"experience": int(message.text)})
    await state.clear()
    await message.answer("✅ Опыт обновлен!", reply_markup=get_main_keyboard(message.from_user.id))

@dp.message(Survey.editing_birthday)
async def save_edit_birthday(message: Message, state: FSMContext):
    if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", message.text): return await message.answer("Неверный формат.")
    update_user(message.from_user.id, {"birthday": message.text})
    await state.clear()
    await message.answer("✅ ДР обновлено!", reply_markup=get_main_keyboard(message.from_user.id))

@dp.message(Survey.editing_study_group)
async def save_edit_study_group(message: Message, state: FSMContext):
    update_user(message.from_user.id, {"study_group": message.text})
    await state.clear()
    await message.answer("✅ Группа обновлена!", reply_markup=get_main_keyboard(message.from_user.id))

@dp.message(Survey.editing_hobbies)
async def save_edit_hobbies(message: Message, state: FSMContext):
    update_user(message.from_user.id, {"hobbies": message.text})
    await state.clear()
    await message.answer("✅ Хобби обновлены!", reply_markup=get_main_keyboard(message.from_user.id))

# --- ОБРАБОТЧИКИ: МАГАЗИН ---
async def edit_or_send_text(target, text: str, reply_markup=None, parse_mode=None):
    is_bot_message = getattr(target, 'from_user', None) and getattr(target.from_user, 'is_bot', False)
    
    if not is_bot_message:
        await target.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        try:
            await target.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except TelegramBadRequest:
            try:
                await target.delete()
            except Exception:
                pass 
            await target.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)

async def show_shop_menu(tg_id: int, target):
    """Вспомогательная функция для отображения магазина."""
    user = get_user(tg_id)
    if not user or not user.get('equipped_pet_id'):
        await edit_or_send_text(target, "Сначала выбери цвет своего стикмана!", reply_markup=get_main_keyboard(tg_id))
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎩 Голова", callback_data="shop_cat_head"), 
         InlineKeyboardButton(text="👕 Верх", callback_data="shop_cat_top"), 
         InlineKeyboardButton(text="👖 Низ", callback_data="shop_cat_bottom")]
    ])
    await edit_or_send_text(target, "🛒 **Магазин одежды**\n\nВыбери категорию:", reply_markup=kb, parse_mode="Markdown")

@dp.message(F.text == "🛒 Магазин")
async def shop_menu(message: Message):
    await show_shop_menu(message.from_user.id, message)

@dp.callback_query(F.data == "shop_back")
async def shop_back(callback: CallbackQuery):
    await show_shop_menu(callback.from_user.id, callback.message)
    await callback.answer()
    
@dp.callback_query(F.data.startswith("shop_cat_"))
async def shop_category(callback: CallbackQuery):
    category = callback.data.replace("shop_cat_", "")
    response = supabase.table("items").select("group_name").eq("category", category).execute()
    groups = list(set([item['group_name'] for item in response.data if item.get('group_name')]))
    kb = [[InlineKeyboardButton(text=g, callback_data=f"shop_group_{category}_{g}")] for g in groups]
    kb.append([InlineKeyboardButton(text="🔙 Назад в магазин", callback_data="shop_back")])
    await edit_or_send_text(callback.message, f"🛒 Категория: {category}\nВыбери предмет:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()


@dp.callback_query(F.data.startswith("shop_group_"))
async def shop_item(callback: CallbackQuery):
    parts = callback.data.replace("shop_group_", "", 1).split("_", 1)
    category, group_name = parts[0], parts[1]
    items = supabase.table("items").select("*").eq("category", category).eq("group_name", group_name).execute().data
    inventory = get_user_inventory(callback.from_user.id)
    kb = []
    for item in items:
        status = "✅ В инвентаре" if item['id'] in inventory else f"{item['price']} вайбов"
        kb.append([InlineKeyboardButton(text=f"{item['name']} — {status}", callback_data=f"shop_color_{item['id']}")])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"shop_cat_{category}")])
    await edit_or_send_text(callback.message, f"🛒 {group_name}\nВыбери цвет:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@dp.callback_query(F.data.startswith("shop_color_"))
async def shop_color_preview(callback: CallbackQuery):
    item_id = int(callback.data.replace("shop_color_", ""))
    item = supabase.table("items").select("*").eq("id", item_id).execute().data[0]
    tg_id = callback.from_user.id
    user = get_user(tg_id)
    inventory = get_user_inventory(tg_id)
    
    # Генерируем превью: питомец + этот предмет
    pet_item = supabase.table("items").select("filename").eq("id", user['equipped_pet_id']).execute().data[0]
    preview_img = Image.open(f"assets/pets/{pet_item['filename']}").convert("RGBA")
    cloth_image = Image.open(f"assets/clothes/{item['filename']}").convert("RGBA")
    preview_img = Image.alpha_composite(preview_img, cloth_image)
    
    bio = io.BytesIO(); preview_img.save(bio, format='PNG'); bio.seek(0)
    photo_file = BufferedInputFile(file=bio.read(), filename="preview.png")
    
    kb = []
    if item['id'] in inventory:
        equipped_slot = f"equipped_{item['category']}_id"
        if user.get(equipped_slot) == item['id']:
            kb.append([InlineKeyboardButton(text="✅ Сейчас надето", callback_data="noop")])
            kb.append([InlineKeyboardButton(text="❌ Снять", callback_data=f"unequip_{item['category']}")])
        else:
            kb.append([InlineKeyboardButton(text="👕 Надеть", callback_data=f"equip_{item['id']}")])
    else:
        kb.append([InlineKeyboardButton(text=f"🛒 Купить за {item['price']} вайбов", callback_data=f"buy_{item['id']}")])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"shop_group_{item['category']}_{item['group_name']}")])
    
    await callback.message.delete()
    await callback.message.answer_photo(photo=photo_file, caption=f"**{item['name']}**\nЦена: {item['price']} вайбов", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("buy_"))
async def buy_item_callback(callback: CallbackQuery):
    item_id = int(callback.data.replace("buy_", ""))
    item = supabase.table("items").select("*").eq("id", item_id).execute().data[0]
    success, msg = buy_item(callback.from_user.id, item_id, item['price'])
    if success:
        await callback.answer("✅ Куплено!", show_alert=True)
        kb = [
            [InlineKeyboardButton(text="👕 Надеть", callback_data=f"equip_{item['id']}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"shop_group_{item['category']}_{item['group_name']}")]
        ]
        await callback.message.edit_caption(caption=f"**{item['name']}**\n✅ Куплено!", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")
    else:
        await callback.answer(msg, show_alert=True)

@dp.callback_query(F.data.startswith("equip_"))
async def equip_item_callback(callback: CallbackQuery):
    item_id = int(callback.data.replace("equip_", ""))
    item = supabase.table("items").select("*").eq("id", item_id).execute().data[0]
    update_user(callback.from_user.id, {f"equipped_{item['category']}_id": item_id})
    await callback.answer("✅ Надето!", show_alert=True)
    kb = [
        [InlineKeyboardButton(text="✅ Сейчас надето", callback_data="noop")],
        [InlineKeyboardButton(text="❌ Снять", callback_data=f"unequip_{item['category']}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"shop_group_{item['category']}_{item['group_name']}")]
    ]
    await callback.message.edit_caption(reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("unequip_"))
async def unequip_item_callback(callback: CallbackQuery):
    category = callback.data.replace("unequip_", "")
    tg_id = callback.from_user.id
    user = get_user(tg_id)
    current_item_id = user.get(f"equipped_{category}_id")
    item = supabase.table("items").select("*").eq("id", current_item_id).execute().data[0]
    
    update_user(tg_id, {f"equipped_{category}_id": None})
    await callback.answer("Снято", show_alert=True)
    kb = [
        [InlineKeyboardButton(text="👕 Надеть", callback_data=f"equip_{current_item_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"shop_group_{item['category']}_{item['group_name']}")]
    ]
    await callback.message.edit_caption(reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery):
    await callback.answer("Это currentItem", show_alert=True)

@dp.callback_query(F.data == "show_pet")
async def show_pet(callback: CallbackQuery):
    image = generate_pet_image(callback.from_user.id)
    if image:
        bio = io.BytesIO(); image.save(bio, format='PNG'); bio.seek(0)
        photo_file = BufferedInputFile(file=bio.read(), filename="my_pet.png")
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎨 Создать стикерпак (20 вайбов)", callback_data="create_sticker_pack")]])
        await callback.message.answer_photo(photo=photo_file, caption="Твой уникальный стикман! 🎨", reply_markup=kb)
    else:
        await callback.answer("Сначала выбери питомца!", show_alert=True)

# --- СТИКЕРПАК И ОСНОВНЫЕ КНОПКИ (Без изменений) ---
@dp.callback_query(F.data == "create_sticker_pack")
async def create_sticker_pack_handler(callback: CallbackQuery):
    tg_id = callback.from_user.id
    user = get_user(tg_id)
    if user['balance'] < 20: return await callback.answer("Не хватает вайбов!", show_alert=True)
    base_img = generate_pet_image(tg_id)
    if not base_img: return await callback.answer("Сначала выбери питомца!", show_alert=True)
    await callback.answer("Генерирую... ⏳")
    
    stickers_data = [{"text": "ОК", "emoji": "👌"}, {"text": "Понимаемо", "emoji": "🤝"}, {"text": "Привет!", "emoji": "✌️"}]
    input_stickers = []
    for data in stickers_data:
        sticker_img = prepare_sticker_image(base_img, data["text"])
        bio = io.BytesIO(); sticker_img.save(bio, format='PNG'); bio.seek(0)
        sticker_file = BufferedInputFile(file=bio.read(), filename=f"sticker.png")
        input_stickers.append(InputSticker(sticker=sticker_file, format="static", emoji_list=[data["emoji"]]))
        
    pack_name = f"mgt_pet_{tg_id}_{random.randint(1000, 9999)}_by_{BOT_USERNAME}"
    try:
        await bot.create_new_sticker_set(user_id=tg_id, name=pack_name, title=f"Стикеры {user['first_name']}", stickers=input_stickers, sticker_type="regular")
        new_balance, _ = add_vibes(tg_id, -20)
        text = (f"🎉 Твой личный стикерпак готов!\n"
                f"С баланса списано 20 вайбов.\n"
                f"Твой новый баланс: {new_balance} {vibes_word(new_balance)}\n\n"
                f"👉 <a href='https://t.me/addstickers/{pack_name}'>Добавить стикеры в Telegram</a>")
            
        await callback.message.answer(text, disable_web_page_preview=True, parse_mode="HTML")
    except TelegramAPIError as e:
        logger.error(f"Sticker error: {e}")
        await callback.message.answer("⚠️ Ошибка создания пака. Попробуй позже.")

# --- FALLBACK ---
@dp.message()
async def catch_all_messages(message: Message, state: FSMContext):
    await message.answer("Я тебя не понимаю. Используй кнопки меню 👇", reply_markup=get_main_keyboard(message.from_user.id))

@dp.callback_query()
async def catch_all_callbacks(callback: CallbackQuery):
    await callback.answer("Эта кнопка не активна")

# --- ЗАПУСК ---
async def get_bot_username():
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username

async def main():
    logger.info("🚀 Бот запускается...")
    await bot.delete_webhook(drop_pending_updates=True)
    await get_bot_username()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())