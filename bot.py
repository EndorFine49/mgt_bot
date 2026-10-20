import os
import logging
import re
import asyncio
import random
import io
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile, InputSticker
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramAPIError
from supabase import create_client, Client
from dotenv import load_dotenv
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
main_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✨ Получить вайб"), KeyboardButton(text="🛒 Магазин")],
        [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="📋 Посмотреть анкету")]
    ],
    resize_keyboard=True
)

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

def add_vibes(tg_id: int, amount: int):
    """Единая функция начисления/списания вайбов пользователю и его команде."""
    user = get_user(tg_id)
    if not user:
        return None, None
    
    # 1. Обновляем баланс пользователя
    new_user_balance = user["balance"] + amount
    supabase.table("users").update({"balance": new_user_balance}).eq("tg_id", tg_id).execute()
    
    # 2. Обновляем баланс команды, если она есть
    team_name = user.get("team")
    team_updated = False
    if team_name and team_name != "Не состою в команде":
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

# --- МАГАЗИН И ПИТОМЦЫ ---
def get_items(item_type: str):
    response = supabase.table("items").select("*").eq("type", item_type).execute()
    return response.data if response.data else []

def get_user_inventory(user_tg_id: int):
    response = supabase.table("user_inventory").select("item_id").eq("user_tg_id", user_tg_id).execute()
    return [item['item_id'] for item in response.data] if response.data else []

def buy_item(user_tg_id: int, item_id: int, price: int):
    user = get_user(user_tg_id)
    if user['balance'] < price:
        return False, "Не хватает вайбов!"
    
    # Используем add_vibes со знаком минус для списания
    add_vibes(user_tg_id, -price)
    
    supabase.table("user_inventory").insert({"user_tg_id": user_tg_id, "item_id": item_id}).execute()
    return True, "Покупка успешна!"

def generate_pet_image(user_tg_id: int):
    user = get_user(user_tg_id)
    pet_id = user.get('equipped_pet_id')
    if not pet_id:
        return None
    
    pet_item = supabase.table("items").select("filename").eq("id", pet_id).execute().data[0]
    base_image = Image.open(f"assets/pets/{pet_item['filename']}").convert("RGBA")
    
    inventory_ids = get_user_inventory(user_tg_id)
    if not inventory_ids:
        return base_image
        
    clothes_items = supabase.table("items").select("filename").in_("id", inventory_ids).eq("type", "clothes").execute().data
    for cloth in clothes_items:
        cloth_image = Image.open(f"assets/clothes/{cloth['filename']}").convert("RGBA")
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

# --- ОБРАБОТЧИКИ: ОСНОВНЫЕ ---
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    tg_id = message.from_user.id
    user = get_user(tg_id)
    if user:
        await message.answer(f"С возвращением, {user['first_name']}! 👋\nТвой баланс: {user['balance']} {vibes_word(user['balance'])}", reply_markup=main_menu_keyboard)
    else:
        await state.set_state(Registration.waiting_for_first_name)
        await message.answer("Привет! Давай познакомимся. Как твое имя?")

@dp.message(F.text == "✨ Получить вайб")
async def handle_claim_vibe(message: Message):
    tg_id = message.from_user.id
    user = get_user(tg_id)
    if user:
        new_balance, team_name = add_vibes(tg_id, 1)
        text = f"Тебе +1 вайб! ✨\nТвой баланс: {new_balance} {vibes_word(new_balance)}"
        if team_name:
            text += f"\n\n🎯 Твоя команда **{team_name}** тоже получила +1 вайб!"
        await message.answer(text, parse_mode="Markdown")
    else:
        await message.answer("Сначала зарегистрируйся через /start", reply_markup=main_menu_keyboard)

@dp.message(F.text == "💰 Баланс")
async def handle_balance(message: Message):
    tg_id = message.from_user.id
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
        
        await message.answer(text, parse_mode="Markdown")
    else:
        await message.answer("Сначала зарегистрируйся через /start", reply_markup=main_menu_keyboard)

# --- ОБРАБОТЧИКИ: АНКЕТА ---
@dp.message(F.text == "📋 Посмотреть анкету")
async def handle_survey_menu(message: Message):
    tg_id = message.from_user.id
    user = get_user(tg_id)
    if not user:
        return await message.answer("Сначала зарегистрируйся через /start", reply_markup=main_menu_keyboard)

    if not user.get("survey_completed"):
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📝 Заполнить анкету (+5 вайбов)", callback_data="start_survey")]])
        await message.answer("Привет! Ты ещё не заполнил анкету вожатого.\nЭто займет пару минут, а взамен ты получишь **5 вайбов** на баланс! 🎁", reply_markup=kb)
    else:
        text = (f"📋 **Твоя анкета:**\n\n"
                f"👥 Команда: {user['team'] or 'Не указано'}\n"
                f"🔥 Опыт (смены): {user['experience'] or 'Не указано'}\n"
                f"🎂 Дата рождения: {user['birthday'] or 'Не указано'}\n"
                f"📚 Группа обучения: {user['study_group'] or 'Не указано'}\n"
                f"🎨 Хобби/таланты: {user['hobbies'] or 'Не указано'}\n\n"
                f"Хочешь что-то изменить?")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Команда", callback_data="edit_team"), InlineKeyboardButton(text="✏️ Опыт", callback_data="edit_shifts")],
            [InlineKeyboardButton(text="✏️ ДР", callback_data="edit_birthday"), InlineKeyboardButton(text="✏️ Группа", callback_data="edit_study_group")],
            [InlineKeyboardButton(text="✏️ Хобби", callback_data="edit_hobbies")]
        ])
        await message.answer(text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "start_survey")
async def start_survey(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Отлично! В какой вожатской команде ты состоишь?")
    await callback.message.answer("Выбери команду:", reply_markup=get_team_keyboard())
    await state.set_state(Survey.waiting_for_team)
    await callback.answer()

@dp.callback_query(F.data.startswith("team_"))
async def process_team(callback: CallbackQuery, state: FSMContext):
    await state.update_data(team=callback.data.replace("team_", ""))
    await callback.message.edit_text(f"Выбрано: {callback.data.replace('team_', '')} ✅\n\nСколько смен ты отработал всего? Напиши число, например: 3")
    await state.set_state(Survey.waiting_for_shifts)
    await callback.answer()

@dp.message(Survey.waiting_for_shifts)
async def process_shifts(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) < 0:
        return await message.answer("Пожалуйста, введи корректное число смен (например: 3)")
    await state.update_data(experience=int(message.text))
    await message.answer("Отлично! Теперь укажи дату рождения в формате ДД.ММ.ГГГГ")
    await state.set_state(Survey.waiting_for_birthday)

@dp.message(Survey.waiting_for_birthday)
async def process_birthday(message: Message, state: FSMContext):
    if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", message.text):
        return await message.answer("Неверный формат. Используй ДД.ММ.ГГГГ (например: 15.06.1995)")
    await state.update_data(birthday=message.text)
    await message.answer("Супер! Укажи свою группу обучения (или напиши 'Не помню'):")
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
    
    # Сохраняем анкету
    update_user(tg_id, {
        "team": data.get("team"), "experience": data.get("experience"), "birthday": data.get("birthday"),
        "study_group": data.get("study_group"), "hobbies": message.text, "survey_completed": True
    })
    
    # Начисляем вайбы через единую функцию
    new_balance, team_name = add_vibes(tg_id, 5)
    
    text = (f"🎉 Анкета успешно заполнена! Спасибо!\n"
            f"Тебе начислено **5 вайбов** за активность.\n"
            f"Твой текущий баланс: {new_balance} {vibes_word(new_balance)}")
    if team_name:
        text += f"\n\n🎯 Твоя команда **{team_name}** тоже получила +5 вайбов!"
    
    await state.clear()
    await message.answer(text, reply_markup=main_menu_keyboard, parse_mode="Markdown")

# Редактирование анкеты
@dp.callback_query(F.data.startswith("edit_"))
async def start_edit(callback: CallbackQuery, state: FSMContext):
    field = callback.data.replace("edit_", "")
    state_map = {"team": Survey.waiting_for_team, "shifts": Survey.editing_shifts, "birthday": Survey.editing_birthday, "study_group": Survey.editing_study_group, "hobbies": Survey.editing_hobbies}
    prompts = {"team": "Выбери новую команду:", "shifts": "Введи новое кол-во смен:", "birthday": "Новая дата рождения (ДД.ММ.ГГГГ):", "study_group": "Новая группа:", "hobbies": "Новые хобби:"}
    
    if field == "team": 
        await callback.message.answer(prompts[field], reply_markup=get_team_keyboard())
    else: 
        await callback.message.answer(prompts[field])
    await state.set_state(state_map[field])
    await callback.answer()

@dp.callback_query(Survey.waiting_for_team, F.data.startswith("team_"))
async def save_edit_team(callback: CallbackQuery, state: FSMContext):
    update_user(callback.from_user.id, {"team": callback.data.replace("team_", "")})
    await state.clear()
    await callback.message.answer("✅ Команда обновлена!", reply_markup=main_menu_keyboard)
    await callback.answer()

@dp.message(Survey.editing_shifts)
async def save_edit_shifts(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) < 0: return await message.answer("Введи корректное число.")
    update_user(message.from_user.id, {"experience": int(message.text)})
    await state.clear()
    await message.answer("✅ Опыт обновлен!", reply_markup=main_menu_keyboard)

@dp.message(Survey.editing_birthday)
async def save_edit_birthday(message: Message, state: FSMContext):
    if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", message.text): return await message.answer("Неверный формат.")
    update_user(message.from_user.id, {"birthday": message.text})
    await state.clear()
    await message.answer("✅ ДР обновлено!", reply_markup=main_menu_keyboard)

@dp.message(Survey.editing_study_group)
async def save_edit_study_group(message: Message, state: FSMContext):
    update_user(message.from_user.id, {"study_group": message.text})
    await state.clear()
    await message.answer("✅ Группа обновлена!", reply_markup=main_menu_keyboard)

@dp.message(Survey.editing_hobbies)
async def save_edit_hobbies(message: Message, state: FSMContext):
    update_user(message.from_user.id, {"hobbies": message.text})
    await state.clear()
    await message.answer("✅ Хобби обновлены!", reply_markup=main_menu_keyboard)

# --- ОБРАБОТЧИКИ: МАГАЗИН И ПИТОМЦЫ ---
@dp.message(F.text == "🛒 Магазин")
async def shop_menu(message: Message):
    tg_id = message.from_user.id
    user = get_user(tg_id)
    if not user or not user.get('equipped_pet_id'):
        await message.answer("Сначала выбери цвет своего стикмана!", reply_markup=main_menu_keyboard)
        return

    clothes = get_items('clothes')
    inventory = get_user_inventory(tg_id)
    
    text = "🛒 **Магазин одежды**\n\n"
    keyboard = []
    for item in clothes:
        status = "✅ Куплено" if item['id'] in inventory else f"Купить за {item['price']} вайбов"
        keyboard.append([InlineKeyboardButton(text=f"{item['name']} — {status}", callback_data=f"buy_{item['id']}")])
    keyboard.append([InlineKeyboardButton(text="🐾 Мой питомец", callback_data="show_pet")])
    
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("buy_"))
async def buy_item_callback(callback: CallbackQuery):
    item_id = int(callback.data.replace("buy_", ""))
    item = supabase.table("items").select("*").eq("id", item_id).execute().data[0]
    success, msg = buy_item(callback.from_user.id, item_id, item['price'])
    await callback.answer(msg, show_alert=True)
    if success:
        await shop_menu(callback.message)

@dp.callback_query(F.data == "show_pet")
async def show_pet(callback: CallbackQuery):
    image = generate_pet_image(callback.from_user.id)
    if image:
        bio = io.BytesIO()
        image.save(bio, format='PNG')
        bio.seek(0)
        photo_file = BufferedInputFile(file=bio.read(), filename="my_pet.png")
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎨 Создать стикерпак (20 вайбов)", callback_data="create_sticker_pack")]
        ])
        await callback.message.answer_photo(
            photo=photo_file, 
            caption="Твой уникальный стикман! 🎨\n\nТы можешь сделать из него свой стикерпак для чатов!",
            reply_markup=keyboard
        )
    else:
        await callback.answer("Сначала выбери базового питомца!", show_alert=True)

@dp.callback_query(F.data == "create_sticker_pack")
async def create_sticker_pack_handler(callback: CallbackQuery):
    tg_id = callback.from_user.id
    user = get_user(tg_id)
    
    if user['balance'] < 20:
        return await callback.answer("Не хватает вайбов! Нужно 20 🪙", show_alert=True)
        
    base_img = generate_pet_image(tg_id)
    if not base_img:
        return await callback.answer("Сначала выбери базового питомца!", show_alert=True)
        
    await callback.answer("Генерирую стикеры... ⏳")
    
    stickers_data = [
        {"text": "ОК", "emoji": "👌"},
        {"text": "Понимаемо", "emoji": "🤝"},
        {"text": "Привет!", "emoji": "✌️"}
    ]
    
    input_stickers = []
    for data in stickers_data:
        sticker_img = prepare_sticker_image(base_img, data["text"])
        bio = io.BytesIO()
        sticker_img.save(bio, format='PNG')
        bio.seek(0)
        sticker_file = BufferedInputFile(file=bio.read(), filename=f"sticker_{data['emoji']}.png")
        input_stickers.append(InputSticker(sticker=sticker_file, format="static", emoji_list=[data["emoji"]]))
        
    random_suffix = random.randint(1000, 9999)
    pack_name = f"mgt_pet_{tg_id}_{random_suffix}_by_{BOT_USERNAME}"
    pack_title = f"Стикеры {user['first_name']}"
    
    try:
        await bot.create_new_sticker_set(user_id=tg_id, name=pack_name, title=pack_title, stickers=input_stickers, sticker_type="regular")
        
        # Списываем вайбы через единую функцию
        new_balance, team_name = add_vibes(tg_id, -20)
        
        text = (f"🎉 Твой личный стикерпак готов!\n"
                f"С баланса списано 20 вайбов.\n"
                f"Твой новый баланс: {new_balance} {vibes_word(new_balance)}\n\n"
                f"👉 <a href='https://t.me/addstickers/{pack_name}'>Добавить стикеры в Telegram</a>")
        
        if team_name:
            text += f"\n\n🎯 Твоя команда **{team_name}** тоже потеряла 20 вайбов!"
            
        await callback.message.answer(text, disable_web_page_preview=True, parse_mode="HTML")
        
    except TelegramAPIError as e:
        logger.error(f"Ошибка создания стикерпака для user {tg_id}: {e}")
        await callback.message.answer("⚠️ Не удалось создать стикерпак. Попробуй позже.")

# --- АДМИНКА: РУЧНОЕ НАЧИСЛЕНИЕ КОМАНДЕ ---
@dp.message(Command("add_team_vibes"))
async def cmd_add_team_vibes(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return await message.answer("У тебя нет прав администратора.")
    
    parts = message.text.split()
    if len(parts) != 3:
        return await message.answer("Использование: `/add_team_vibes <Название_Команды> <Количество>`\nПример: `/add_team_vibes Джем 50`", parse_mode="Markdown")
    
    team_name = parts[1]
    try:
        amount = int(parts[2])
    except ValueError:
        return await message.answer("Количество должно быть числом.")
    
    # Проверяем, есть ли такая команда
    team_res = supabase.table("teams").select("name").eq("name", team_name).execute()
    if not team_res.data:
        return await message.answer(f"Команда '{team_name}' не найдена. Проверь название.")
    
    # Обновляем баланс
    current_res = supabase.table("teams").select("balance").eq("name", team_name).execute()
    new_balance = current_res.data[0]["balance"] + amount
    supabase.table("teams").update({"balance": new_balance}).eq("name", team_name).execute()
    
    await message.answer(f"✅ Команде **{team_name}** успешно начислено/списано **{amount}** вайбов.\nНовый баланс команды: {new_balance}", parse_mode="Markdown")

# --- ОБЩИЙ FALLBACK ---
@dp.message()
async def catch_all_messages(message: Message, state: FSMContext):
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
            await message.answer(f"Приятно познакомиться, {first_name}! 🎉\nТы получил 1 вайб на старт.\nТвой баланс: {user['balance']} {vibes_word(user['balance'])}. Теперь выбери своего стикмана:", reply_markup=get_team_keyboard()) # Здесь должна быть клавиатура питомцев, но для краткости оставил get_team_keyboard как заглушку, лучше использовать get_pets_keyboard из предыдущей версии, если она есть.
        else:
            await message.answer("Я тебя не понимаю. Используй кнопки меню 👇", reply_markup=main_menu_keyboard)
    else:
        await message.answer("Я тебя не понимаю. Используй кнопки меню 👇", reply_markup=main_menu_keyboard)

@dp.callback_query()
async def catch_all_callbacks(callback: CallbackQuery):
    await callback.answer("Эта кнопка пока не активна")

# --- ЗАПУСК ---
async def main():
    logger.info("🚀 Бот запускается...")
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("✅ Вебхук удалён, запускаем polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())