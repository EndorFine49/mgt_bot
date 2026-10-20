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

def get_user_inventory(user_tg_id: int):
    response = supabase.table("user_inventory").select("item_id").eq("user_tg_id", user_tg_id).execute()
    return [item['item_id'] for item in response.data] if response.data else []

def buy_item(user_tg_id: int, item_id: int, price: int):
    user = get_user(user_tg_id)
    if user['balance'] < price: return False, "Не хватает вайбов!"
    update_balance(user_tg_id, -price)
    supabase.table("user_inventory").insert({"user_tg_id": user_tg_id, "item_id": item_id}).execute()
    return True, "Покупка успешна!"

# --- ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ ---
def generate_pet_image(user_tg_id: int):
    user = get_user(user_tg_id)
    pet_id = user.get('equipped_pet_id')
    if not pet_id: return None
    
    pet_item = supabase.table("items").select("filename").eq("id", pet_id).execute().data[0]
    base_image = Image.open(f"assets/pets/{pet_item['filename']}").convert("RGBA")
    
    # Берем только то, что реально надето (слоты)
    equipped_ids = [user.get('equipped_head_id'), user.get('equipped_top_id'), user.get('equipped_bottom_id')]
    equipped_ids = [eid for eid in equipped_ids if eid]
    
    if not equipped_ids: return base_image
        
    clothes_items = supabase.table("items").select("filename").in_("id", equipped_ids).execute().data
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

# --- ОБРАБОТЧИКИ: РЕГИСТРАЦИЯ И ВЫБОР ПИТОМЦА ---
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
    await callback.message.answer("Главное меню:", reply_markup=main_menu_keyboard)
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
    update_user(tg_id, {"team": data.get("team"), "experience": data.get("experience"), "birthday": data.get("birthday"), "study_group": data.get("study_group"), "hobbies": message.text, "survey_completed": True})
    new_balance = update_balance(tg_id, 5)
    await state.clear()
    await message.answer(f"🎉 Анкета заполнена! Тебе +5 вайбов.\nБаланс: {new_balance} {vibes_word(new_balance)}", reply_markup=main_menu_keyboard)

@dp.message(F.text == "📋 Посмотреть анкету")
async def handle_survey_menu(message: Message):
    user = get_user(message.from_user.id)
    if not user: return await message.answer("Сначала /start", reply_markup=main_menu_keyboard)

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
    await callback.message.answer("✅ Команда обновлена!", reply_markup=main_menu_keyboard)
    await callback.answer()

@dp.message(Survey.editing_shifts)
async def save_edit_shifts(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Введи число.")
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

# --- ОБРАБОТЧИКИ: МАГАЗИН ---
@dp.message(F.text == "🛒 Магазин")
async def shop_menu(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎩 Голова", callback_data="shop_cat_head"), InlineKeyboardButton(text="👕 Верх", callback_data="shop_cat_top"), InlineKeyboardButton(text="👖 Низ", callback_data="shop_cat_bottom")],
        [InlineKeyboardButton(text="🐾 Мой питомец", callback_data="show_pet")]
    ])
    await message.answer("🛒 **Магазин одежды**\n\nВыбери категорию:", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("shop_cat_"))
async def shop_category(callback: CallbackQuery):
    category = callback.data.replace("shop_cat_", "")
    response = supabase.table("items").select("group_name").eq("category", category).execute()
    groups = list(set([item['group_name'] for item in response.data if item.get('group_name')]))
    
    kb = [[InlineKeyboardButton(text=g, callback_data=f"shop_group_{category}_{g}")] for g in groups]
    kb.append([InlineKeyboardButton(text="🔙 Назад в магазин", callback_data="shop_back")])
    
    # Удаляем текущее сообщение (может быть фото) и отправляем новое
    await callback.message.delete()
    await callback.message.answer(f"🛒 Категория: {category}\nВыбери предмет:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@dp.callback_query(F.data == "shop_back")
async def shop_back(callback: CallbackQuery):
    await callback.message.delete()
    await shop_menu(callback.message)
    await callback.answer()

@dp.callback_query(F.data.startswith("shop_group_"))
async def shop_item(callback: CallbackQuery):
    parts = callback.data.replace("shop_group_", "").split("_", 1)
    category, group_name = parts[0], parts[1]
    items = supabase.table("items").select("*").eq("category", category).eq("group_name", group_name).execute().data
    inventory = get_user_inventory(callback.from_user.id)
    
    kb = []
    for item in items:
        status = "✅ В инвентаре" if item['id'] in inventory else f"{item['price']} вайбов"
        kb.append([InlineKeyboardButton(text=f"{item['name']} — {status}", callback_data=f"shop_color_{item['id']}")])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"shop_cat_{category}")])
    
    # Удаляем текущее сообщение (может быть фото) и отправляем новое
    await callback.message.delete()
    await callback.message.answer(f"🛒 {group_name}\nВыбери цвет:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
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

# --- СТikЕРПАК И ОСНОВНЫЕ КНОПКИ (Без изменений) ---
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
        update_balance(tg_id, -20)
        await callback.message.answer(f"🎉 Пак готов! Списано 20 вайбов.\n👉 <a href='https://t.me/addstickers/{pack_name}'>Добавить стикеры</a>", disable_web_page_preview=True, parse_mode="HTML")
    except TelegramAPIError as e:
        logger.error(f"Sticker error: {e}")
        await callback.message.answer("⚠️ Ошибка создания пака. Попробуй позже.")

@dp.message(F.text == "✨ Получить вайб")
async def handle_claim_vibe(message: Message):
    user = get_user(message.from_user.id)
    if user:
        new_balance = update_balance(message.from_user.id, 1)
        await message.answer(f"Тебе +1 вайб! ✨\nБаланс: {new_balance} {vibes_word(new_balance)}")
    else: await message.answer("Сначала /start", reply_markup=main_menu_keyboard)

@dp.message(F.text == "💰 Баланс")
async def handle_balance(message: Message):
    user = get_user(message.from_user.id)
    if user: await message.answer(f"Твой баланс: {user['balance']} {vibes_word(user['balance'])}")
    else: await message.answer("Сначала /start", reply_markup=main_menu_keyboard)

# --- FALLBACK ---
@dp.message()
async def catch_all_messages(message: Message, state: FSMContext):
    await message.answer("Я тебя не понимаю. Используй кнопки меню 👇", reply_markup=main_menu_keyboard)

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