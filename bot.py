import os
import logging
import re
import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from supabase import create_client, Client
from dotenv import load_dotenv
from PIL import Image
import io
from aiogram.types import BufferedInputFile
from PIL import Image, ImageDraw, ImageFont
from aiogram.types import BufferedInputFile, InputSticker
from aiogram.exceptions import TelegramAPIError

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

def update_balance(tg_id: int, amount: int):
    user = get_user(tg_id)
    if user:
        new_balance = user["balance"] + amount
        supabase.table("users").update({"balance": new_balance}).eq("tg_id", tg_id).execute()
        return new_balance
    return None

# --- МАГАЗИН И ПИТОМЦЫ ---

def get_items(item_type: str):
    """Получает список товаров определенного типа (pet или clothes)."""
    response = supabase.table("items").select("*").eq("type", item_type).execute()
    return response.data if response.data else []

def get_user_inventory(user_tg_id: int):
    """Получает список ID товаров, купленных пользователем."""
    response = supabase.table("user_inventory").select("item_id").eq("user_tg_id", user_tg_id).execute()
    return [item['item_id'] for item in response.data] if response.data else []

def buy_item(user_tg_id: int, item_id: int, price: int):
    """Покупает предмет, если хватает вайбов."""
    user = get_user(user_tg_id)
    if user['balance'] < price:
        return False, "Не хватает вайбов!"
    
    # Списываем вайбы
    update_balance(user_tg_id, -price)
    
    # Добавляем в инвентарь
    supabase.table("user_inventory").insert({
        "user_tg_id": user_tg_id,
        "item_id": item_id
    }).execute()
    
    return True, "Покупка успешна!"

def generate_pet_image(user_tg_id: int):
    """Генерирует картинку питомца с надетой одеждой."""
    user = get_user(user_tg_id)
    pet_id = user.get('equipped_pet_id')
    
    if not pet_id:
        return None
        
    # Находим файл питомца
    pet_item = supabase.table("items").select("filename").eq("id", pet_id).execute().data[0]
    base_image = Image.open(f"assets/pets/{pet_item['filename']}").convert("RGBA")
    
    # Находим всю купленную одежду пользователя
    inventory_ids = get_user_inventory(user_tg_id)
    if not inventory_ids:
        return base_image
        
    clothes_items = supabase.table("items").select("filename").in_("id", inventory_ids).eq("type", "clothes").execute().data
    
    # Накладываем одежду слоями
    for cloth in clothes_items:
        cloth_image = Image.open(f"assets/clothes/{cloth['filename']}").convert("RGBA")
        base_image = Image.alpha_composite(base_image, cloth_image)
        
    return base_image

def prepare_sticker_image(base_image: Image.Image, text: str):
    """Изменяет размер до 512x512 и добавляет текст снизу."""
    # 1. Приводим к размеру 512x512 (требование Telegram)
    img = base_image.resize((512, 512), Image.Resampling.LANCZOS).convert("RGBA")
    draw = ImageDraw.Draw(img)
    
    # 2. Загружаем шрифт (если файла нет, используем стандартный, но он будет мелким)
    try:
        font = ImageFont.truetype("assets/font.ttf", 48)
    except IOError:
        font = ImageFont.load_default()
        print("ВНИМАНИЕ: Файл assets/font.ttf не найден! Текст может быть мелким.")
    
    # 3. Вычисляем позицию текста (по центру снизу)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = (512 - text_width) / 2
    y = 512 - text_height - 30 # Отступ 30px от низа
    
    # 4. Рисуем текст с черной обводкой для читаемости на любом фоне
    outline_color = "black"
    fill_color = "white"
    
    # Рисуем обводку (смещая текст на 2 пикселя в 4 стороны)
    draw.text((x-2, y-2), text, font=font, fill=outline_color)
    draw.text((x+2, y-2), text, font=font, fill=outline_color)
    draw.text((x-2, y+2), text, font=font, fill=outline_color)
    draw.text((x+2, y+2), text, font=font, fill=outline_color)
    
    # Рисуем основной текст
    draw.text((x, y), text, font=font, fill=fill_color)
    
    return img

# --- ФУНКЦИЯ РАССЫЛКИ ---
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

# --- ОБРАБОТЧИКИ: ОСНОВНЫЕ ---

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
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

@dp.message(F.text == "✨ Получить вайб")
async def handle_claim_vibe(message: Message):
    tg_id = message.from_user.id
    user = get_user(tg_id)
    if user:
        new_balance = update_balance(tg_id, 1)
        await message.answer(f"Тебе +1 вайб! ✨\nТвой баланс: {new_balance} {vibes_word(new_balance)}")
    else:
        await message.answer("Сначала зарегистрируйся через /start", reply_markup=main_menu_keyboard)

@dp.message(F.text == "💰 Баланс")
async def handle_balance(message: Message):
    tg_id = message.from_user.id
    user = get_user(tg_id)
    if user:
        await message.answer(f"Твой баланс: {user['balance']} {vibes_word(user['balance'])}")
    else:
        await message.answer("Сначала зарегистрируйся через /start", reply_markup=main_menu_keyboard)

# --- ОБРАБОТЧИКИ: АНКЕТА ---
@dp.message(F.text == "📋 Посмотреть анкету")
async def handle_survey_menu(message: Message):
    tg_id = message.from_user.id
    user = get_user(tg_id)
    if not user:
        await message.answer("Сначала зарегистрируйся через /start", reply_markup=main_menu_keyboard)
        return

    if not user.get("survey_completed"):
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=" Заполнить анкету (+5 вайбов)", callback_data="start_survey")
        ]])
        await message.answer("Привет! Ты ещё не заполнил анкету вожатого.\nЭто займет пару минут, а взамен ты получишь **5 вайбов** на баланс! ", reply_markup=kb)
    else:
        text = (f"📋 **Твоя анкета:**\n\n"
                f"👥 Команда: {user['team'] or 'Не указано'}\n"
                f" Опыт (смены): {user['experience'] or 'Не указано'}\n"
                f"🎂 Дата рождения: {user['birthday'] or 'Не указано'}\n"
                f"📚 Группа обучения: {user['study_group'] or 'Не указано'}\n"
                f"🎨 Хобби/таланты: {user['hobbies'] or 'Не указано'}\n\n"
                f"Хочешь что-то изменить?")
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Опыт", callback_data="edit_shifts"), InlineKeyboardButton(text="✏️ ДР", callback_data="edit_birthday")],
            [InlineKeyboardButton(text="✏️ Группа", callback_data="edit_study_group"), InlineKeyboardButton(text="✏️ Хобби", callback_data="edit_hobbies")]
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
    team = callback.data.replace("team_", "")
    await state.update_data(team=team)
    await callback.message.edit_text(f"Выбрано: {team} ✅\n\nСколько смен ты отработал всего (включая другие лагеря)?\nНапиши число, например: 3")
    await state.set_state(Survey.waiting_for_shifts)
    await callback.answer()

@dp.message(Survey.waiting_for_shifts)
async def process_shifts(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) < 0:
        await message.answer("Пожалуйста, введи корректное число смен (например: 3)")
        return
    await state.update_data(experience=int(message.text)) # Используем experience
    await message.answer("Отлично! Теперь укажи дату рождения в формате ДД.ММ.ГГГГ\nНапример: 15.06.1995")
    await state.set_state(Survey.waiting_for_birthday)

@dp.message(Survey.waiting_for_birthday)
async def process_birthday(message: Message, state: FSMContext):
    if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", message.text):
        await message.answer("Неверный формат. Используй ДД.ММ.ГГГГ (например: 15.06.1995)")
        return
    await state.update_data(birthday=message.text)
    await message.answer("Супер! Укажи свою группу обучения (или напиши 'Не помню', если учился давно):")
    await state.set_state(Survey.waiting_for_study_group)

@dp.message(Survey.waiting_for_study_group)
async def process_study_group(message: Message, state: FSMContext):
    await state.update_data(study_group=message.text)
    await message.answer("Последний вопрос! Расскажи кратко о своих хобби, увлечениях или особых талантах:")
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
    new_balance = update_balance(tg_id, 5)
    await state.clear()
    await message.answer(
        f"🎉 Анкета успешно заполнена! Спасибо!\n"
        f"Тебе начислено **5 вайбов** за активность.\n"
        f"Твой текущий баланс: {new_balance} {vibes_word(new_balance)}",
        reply_markup=main_menu_keyboard, parse_mode="Markdown"
    )

# Редактирование анкеты
@dp.callback_query(F.data.startswith("edit_"))
async def start_edit(callback: CallbackQuery, state: FSMContext):
    field = callback.data.replace("edit_", "")
    state_map = {"shifts": Survey.editing_shifts, "birthday": Survey.editing_birthday, "study_group": Survey.editing_study_group, "hobbies": Survey.editing_hobbies}
    prompts = {"shifts": "Введи новое количество отработанных смен:", "birthday": "Введи новую дату рождения (ДД.ММ.ГГГГ):", "study_group": "Введи новую группу обучения:", "hobbies": "Расскажи о новых хобби или талантах:"}
    await state.set_state(state_map[field])
    await callback.message.edit_text(prompts[field])
    await callback.answer()

@dp.message(Survey.editing_shifts)
async def save_edit_shifts(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) < 0: return await message.answer("Введи корректное число.")
    update_user(message.from_user.id, {"experience": int(message.text)})
    await state.clear()
    await message.answer("✅ Опыт обновлен!", reply_markup=main_menu_keyboard)

@dp.message(Survey.editing_birthday)
async def save_edit_birthday(message: Message, state: FSMContext):
    if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", message.text): return await message.answer("Неверный формат (ДД.ММ.ГГГГ).")
    update_user(message.from_user.id, {"birthday": message.text})
    await state.clear()
    await message.answer("✅ Дата рождения обновлена!", reply_markup=main_menu_keyboard)

@dp.message(Survey.editing_study_group)
async def save_edit_study_group(message: Message, state: FSMContext):
    update_user(message.from_user.id, {"study_group": message.text})
    await state.clear()
    await message.answer("✅ Группа обучения обновлена!", reply_markup=main_menu_keyboard)

@dp.message(Survey.editing_hobbies)
async def save_edit_hobbies(message: Message, state: FSMContext):
    update_user(message.from_user.id, {"hobbies": message.text})
    await state.clear()
    await message.answer("✅ Хобби обновлены!", reply_markup=main_menu_keyboard)

# --- ОБРАБОТЧИКИ: РАССЫЛКА (АДМИНКА) ---

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return await message.answer("У тебя нет прав администратора.")
    await message.answer(" *Режим рассылки*\n\nПришли текст сообщения, который нужно разослать всем пользователям.\n\n_Поддерживается Markdown: *жирный*, _курсив_, `код`_", parse_mode="Markdown")
    await state.set_state(Broadcast.waiting_for_text)

@dp.message(Command("test_broadcast"))
async def cmd_test_broadcast(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return await message.answer("У тебя нет прав администратора.")
    await message.answer("🧪 *Тестовая рассылка*\n\nПришли текст сообщения. Оно придет только тебе.", parse_mode="Markdown")
    await state.update_data(is_test=True)
    await state.set_state(Broadcast.waiting_for_text)

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        await state.clear()
        await message.answer("Действие отменено.", reply_markup=main_menu_keyboard)
    else:
        await message.answer("Нет активных действий для отмены.", reply_markup=main_menu_keyboard)

@dp.message(Broadcast.waiting_for_text)
async def process_broadcast_text(message: Message, state: FSMContext):
    await state.update_data(text=message.text)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, добавить", callback_data="broadcast_btn_yes"), InlineKeyboardButton(text="Нет, без кнопки", callback_data="broadcast_btn_no")]
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

# --- ОБРАБОТЧИКИ: МАГАЗИН И ПИТОМЦЫ ---

@dp.message(F.text == "🛒 Магазин")
async def shop_menu(message: Message):
    tg_id = message.from_user.id
    user = get_user(tg_id)
    
    if not user.get('equipped_pet_id'):
        # Если питомец не выбран, показываем только стикманов
        await message.answer("Выбери цвет своего стикмана:", reply_markup=get_pets_keyboard())
    else:
        # Иначе показываем одежду
        clothes = get_items('clothes')
        inventory = get_user_inventory(tg_id)
        
        text = "🛒 **Магазин одежды**\n\n"
        keyboard = []
        
        for item in clothes:
            status = "✅ Куплено" if item['id'] in inventory else f"Купить за {item['price']} вайбов"
            keyboard.append([InlineKeyboardButton(text=f"{item['name']} - {status}", callback_data=f"buy_{item['id']}")])
            
        keyboard.append([InlineKeyboardButton(text="🐾 Мой питомец", callback_data="show_pet")])
        
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("buy_"))
async def buy_item_callback(callback: CallbackQuery):
    item_id = int(callback.data.replace("buy_", ""))
    item = supabase.table("items").select("*").eq("id", item_id).execute().data[0]
    
    success, msg = buy_item(callback.from_user.id, item_id, item['price'])
    await callback.answer(msg, show_alert=True)
    if success:
        await shop_menu(callback.message) # Обновляем меню магазина

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
        await callback.answer("Сначала выбери базового питомца в магазине!", show_alert=True)

def get_pets_keyboard():
    pets = get_items('pet')
    keyboard = []
    for pet in pets:
        keyboard.append([InlineKeyboardButton(text=pet['name'], callback_data=f"equip_pet_{pet['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@dp.callback_query(F.data.startswith("equip_pet_"))
async def equip_pet(callback: CallbackQuery):
    pet_id = int(callback.data.replace("equip_pet_", ""))
    update_user(callback.from_user.id, {"equipped_pet_id": pet_id})
    await callback.answer("Питомец выбран! Теперь тебе доступен магазин одежды.", show_alert=True)
    await shop_menu(callback.message)

@dp.callback_query(F.data == "create_sticker_pack")
async def create_sticker_pack_handler(callback: CallbackQuery):
    tg_id = callback.from_user.id
    user = get_user(tg_id)
    
    # 1. Проверка баланса
    if user['balance'] < 20:
        return await callback.answer("Не хватает вайбов! Нужно 20 🪙", show_alert=True)
        
    # 2. Проверка наличия питомца
    base_img = generate_pet_image(tg_id)
    if not base_img:
        return await callback.answer("Сначала выбери базового питомца в магазине!", show_alert=True)
        
    await callback.answer("Генерирую стикеры и создаю пак... ⏳")
    
    # 3. Данные для 3-х стикеров
    stickers_data = [
        {"text": "ок 👌", "emoji": "👌"},
        {"text": "понимаемо 🤝", "emoji": "🤝"},
        {"text": "привет ✌️", "emoji": "✌️"}
    ]
    
    input_stickers = []
    for data in stickers_data:
        # Генерируем картинку
        sticker_img = prepare_sticker_image(base_img, data["text"])
        
        # Сохраняем в память
        bio = io.BytesIO()
        sticker_img.save(bio, format='PNG')
        bio.seek(0)
        
        # Оборачиваем для aiogram
        sticker_file = BufferedInputFile(file=bio.read(), filename=f"sticker_{data['emoji']}.png")
        
        # Добавляем в список для отправки
        input_stickers.append(
            InputSticker(sticker=sticker_file, format="static", emoji_list=[data["emoji"]])
        )
        
    # 4. Формируем уникальное имя пака (должно заканчиваться на _by_имя_бота, но Telegram сам добавит это, если имя занято, или мы сделаем уникальным)
    # Для надежности добавляем часть tg_id, чтобы имя было уникальным
    pack_name = f"mgt_pet_{tg_id}"
    pack_title = f"Стикеры {user['first_name']}"
    
    try:
        # 5. Создаем стикерпак одним запросом (aiogram 3 позволяет передать список stickers сразу)
        await bot.create_new_sticker_set(
            user_id=tg_id,
            name=pack_name,
            title=pack_title,
            stickers=input_stickers,
            sticker_type="regular"
        )
        
        # 6. Списываем вайбы
        update_balance(tg_id, -20)
        
        # 7. Отправляем ссылку пользователю
        await callback.message.answer(
            f"🎉 Твой личный стикерпак готов!\n"
            f"С баланса списано 20 вайбов.\n\n"
            f"👉 <a href='https://t.me/addstickers/{pack_name}'>Добавить стикеры в Telegram</a>",
            disable_web_page_preview=True,
            parse_mode="HTML"
        )
        
    except TelegramAPIError as e:
        error_msg = str(e)
        if "name" in error_msg.lower() or "invalid" in error_msg.lower():
            await callback.message.answer(
                "⚠️ Не удалось создать стикерпак. Возможно, ты уже создавал его недавно, или имя занято. Попробуй позже!"
            )
        else:
            await callback.message.answer(f"Произошла ошибка при создании пака: {error_msg}")

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
            await message.answer(f"Приятно познакомиться, {first_name}! 🎉\nТы получил 1 вайб на старт.\nТвой баланс: {user['balance']} {vibes_word(user['balance'])}. Теперь выбери своего стикмана:", reply_markup=get_pets_keyboard())
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
    import asyncio
    asyncio.run(main())