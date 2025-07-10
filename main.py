from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters,
    PicklePersistence, CallbackQueryHandler, JobQueue
)
import datetime
import google.generativeai as genai
import json
import os
import matplotlib.pyplot as plt
import io
import random
from collections import defaultdict

# Создаем глобальную переменную и настраиваем API-ключ при старте
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    
# --- Константы и глобальные переменные ---
WEIGHT_LOG_FILE = "weight_log.json"
USER_PROFILES_FILE = "user_profiles.json"
PERSISTENCE_FILE = "my_bot_data.pkl"
user_profiles_data = {}

# Состояния
(SETUP_STATE_NONE, SETUP_STATE_GENDER, SETUP_STATE_AGE, SETUP_STATE_HEIGHT,
 SETUP_STATE_WEIGHT_INITIAL, SETUP_STATE_ACTIVITY, SETUP_STATE_DIET_GOAL,
 SETUP_STATE_LOGGING_FOOD_AWAITING_INPUT, SETUP_STATE_ADDING_PREFERENCE,
 SETUP_STATE_ADDING_EXCLUSION, SETUP_STATE_AWAITING_FRIDGE_INGREDIENTS) = range(11)

# --- Вспомогательные функции ---

def load_json_data(filepath, default_value=None):
    if default_value is None: default_value = {}
    if not os.path.exists(filepath): return default_value
    try:
        with open(filepath, "r", encoding="utf-8") as f: return json.load(f)
    except (json.JSONDecodeError, Exception) as e:
        print(f"Error reading {filepath}: {e}"); return default_value

def save_weight(chat_id, weight):
    data = load_json_data(WEIGHT_LOG_FILE)
    today = datetime.date.today().isoformat()
    data.setdefault(str(chat_id), {})[today] = weight
    with open(WEIGHT_LOG_FILE, "w", encoding="utf-8") as f: json.dump(data, f, indent=4)

def get_latest_weight(chat_id):
    data = load_json_data(WEIGHT_LOG_FILE)
    user_weights = data.get(str(chat_id), {})
    if user_weights: return user_weights[sorted(user_weights.keys())[-1]]
    return None

async def calculate_target_calories_and_pfc(user_id):
    user_profile = user_profiles_data.get(str(user_id))
    latest_weight = get_latest_weight(str(user_id))
    if not all([user_profile, latest_weight]): return None, None, None, None
    gender, age, height, activity = user_profile.get('gender'), user_profile.get('age'), user_profile.get('height'), user_profile.get('activity')
    diet_goal = user_profile.get('diet_goal', 'баланс')
    if gender == 'мужской': bmr = (10 * latest_weight) + (6.25 * height) - (5 * age) + 5
    else: bmr = (10 * latest_weight) + (6.25 * height) - (5 * age) - 161
    activity_factors = [1.2, 1.375, 1.55, 1.725, 1.9]
    maintenance_calories = int(bmr * activity_factors[activity - 1])
    target_calories = int(maintenance_calories * 0.8)
    if diet_goal == 'белок': protein, fat, carbs = int((target_calories * 0.30) / 4), int((target_calories * 0.30) / 9), int((target_calories * 0.40) / 4)
    elif diet_goal == 'низкоугл': protein, fat, carbs = int((target_calories * 0.25) / 4), int((target_calories * 0.50) / 9), int((target_calories * 0.25) / 4)
    else: protein, fat, carbs = int((target_calories * 0.20) / 4), int((target_calories * 0.30) / 9), int((target_calories * 0.50) / 4)
    return target_calories, protein, fat, carbs

def create_pfc_pie_chart(pfc_data):
    labels = ['Белки', 'Жиры', 'Углеводы']
    sizes = [pfc_data.get('protein', 0), pfc_data.get('fat', 0), pfc_data.get('carbs', 0)]
    if sum(sizes) == 0: sizes = [1, 1, 1]
    colors = ['#ff9999','#66b3ff','#99ff99']
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.pie(sizes, labels=labels, autopct='%1.1f%%', startangle=90, colors=colors)
    ax.axis('equal'); plt.title(f"Баланс БЖУ (в граммах)\nБ: {sizes[0]}г, Ж: {sizes[1]}г, У: {sizes[2]}г")
    buf = io.BytesIO(); plt.savefig(buf, format='png'); buf.seek(0); plt.close()
    return buf

async def generate_personalized_menu_with_llm(user_profile, calorie_target, pfc_targets, num_days=1, meal_to_replace=None):
    # Проверяем, доступен ли API-ключ
    if not GEMINI_API_KEY:
        return {"weekly_plan": [], "shopping_list": ["ОШИБКА: API-ключ для Gemini не настроен."]}

    # Выбираем модель
    model = genai.GenerativeModel('gemini-pro')
    
    # Создаем очень подробный промпт для нейросети
    # Мы просим ее вернуть ответ строго в формате JSON, чтобы наш код мог его прочитать
    prompt = f"""
    Выступи в роли диетолога. Создай план питания на {num_days} дней для пользователя со следующими параметрами:
    - Пол: {user_profile.get('gender')}
    - Возраст: {user_profile.get('age')}
    - Рост: {user_profile.get('height')} см
    - Уровень активности: {user_profile.get('activity')} из 5
    - Цель диеты: {user_profile.get('diet_goal')}

    Суточная цель по калориям: примерно {calorie_target} ккал.
    Цель по БЖУ: Белки ~{pfc_targets['p']}г, Жиры ~{pfc_targets['f']}г, Углеводы ~{pfc_targets['c']}г.

    Пожалуйста, верни ответ ИСКЛЮЧИТЕЛЬНО в формате JSON. Не добавляй никакого текста до или после JSON.
    Структура JSON должна быть следующей:
    {{
      "weekly_plan": [
        {{
          "day_name": "День 1",
          "meals": [
            {{
              "meal_name": "Завтрак (Название блюда)",
              "items": [{{"food_item": "название продукта", "grams": 100}}],
              "total_calories": 350,
              "total_protein": 20,
              "total_fat": 10,
              "total_carbs": 45,
              "recipe": "Краткий рецепт приготовления."
            }},
            {{
              "meal_name": "Обед (Название блюда)",
              "items": [], "total_calories": 550, "total_protein": 40, "total_fat": 20, "total_carbs": 50, "recipe": "..."
            }},
            {{
              "meal_name": "Ужин (Название блюда)",
              "items": [], "total_calories": 400, "total_protein": 30, "total_fat": 15, "total_carbs": 35, "recipe": "..."
            }}
          ]
        }}
      ],
      "shopping_list": ["Продукт 1: X г", "Продукт 2: Y г"]
    }}
    Создай разнообразные и простые блюда. Список покупок должен включать все ингредиенты на {num_days} дней.
    """

    try:
        # Отправляем запрос в нейросеть
        response = await model.generate_content_async(prompt)
        
        # Очищаем ответ от лишних символов и загружаем JSON
        json_text = response.text.strip().replace("```json", "").replace("```", "")
        parsed_json = json.loads(json_text)
        return parsed_json
        
    except Exception as e:
        print(f"Ошибка при вызове API Gemini: {e}")
        return {"weekly_plan": [], "shopping_list": [f"ОШИБКА: Не удалось сгенерировать меню. {e}"]}

async def calculate_calories_from_food_list_llm(user_id, food_list_items):
    if not GEMINI_API_KEY: return None
    
    model = genai.GenerativeModel('gemini-pro')
    food_list_str = ", ".join(food_list_items)
    
    prompt = f"""
    Подсчитай КБЖУ для следующего списка съеденных продуктов: {food_list_str}.
    Верни ответ ТОЛЬКО в формате JSON, без лишнего текста. Пример:
    {{
      "calories": 500,
      "protein": 30,
      "fat": 20,
      "carbs": 50
    }}
    """
    
    try:
        response = await model.generate_content_async(prompt)
        json_text = response.text.strip().replace("```json", "").replace("```", "")
        return json.loads(json_text)
    except Exception as e:
        print(f"Ошибка при подсчете КБЖУ через API: {e}")
        return None

async def generate_recipe_from_ingredients(user_id, ingredients_text):
    if not GEMINI_API_KEY: return None

    model = genai.GenerativeModel('gemini-pro')
    prompt = f"""
    Придумай простой и здоровый рецепт из следующих ингредиентов: {ingredients_text}.
    Верни ответ ТОЛЬКО в формате JSON со следующей структурой:
    {{
      "dish_name": "Название блюда",
      "description": "Краткое описание",
      "ingredients_used": ["Ингредиент 1", "Ингредиент 2"],
      "recipe_steps": ["Шаг 1", "Шаг 2", "Шаг 3"]
    }}
    """
    try:
        response = await model.generate_content_async(prompt)
        json_text = response.text.strip().replace("```json", "").replace("```", "")
        return json.loads(json_text)
    except Exception as e:
        print(f"Ошибка при генерации рецепта через API: {e}")
        return None

### НОВЫЙ КОД: Функция для автоматической установки напоминаний ###
async def schedule_reminders_for_user(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Проверяет и устанавливает напоминания для пользователя, если их еще нет."""
    # Уникальные имена задач для каждого пользователя
    water_job_name = f"drink_water_{chat_id}"
    weigh_job_name = f"weigh_in_{chat_id}"
    
    # Проверяем, существует ли уже задача на напоминание о воде
    if not context.job_queue.get_jobs_by_name(water_job_name):
        context.job_queue.run_repeating(send_water_reminder, interval=7200, chat_id=chat_id, name=water_job_name)
        print(f"Установлено напоминание о воде для {chat_id}")

    # Проверяем, существует ли уже задача на напоминание о взвешивании
    if not context.job_queue.get_jobs_by_name(weigh_job_name):
        context.job_queue.run_daily(check_and_send_weigh_in_reminder, time=datetime.time(hour=20, minute=0), chat_id=chat_id, name=weigh_job_name)
        print(f"Установлено напоминание о взвешивании для {chat_id}")

# --- Основные команды ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_id_str = str(chat_id)
    
    # Приветствие и настройка
    if chat_id_str not in user_profiles_data:
        context.user_data['setup_step'] = SETUP_STATE_GENDER
        keyboard = [["Мужской", "Женский"]]; reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text("👋 Добро пожаловать! Давайте настроим ваш профиль.\nПожалуйста, укажите ваш пол:", reply_markup=reply_markup)
    else:
        await update.message.reply_text("👋 С возвращением! Используйте меню для навигации.", reply_markup=MAIN_REPLY_MARKUP)
        # ### ИЗМЕНЕНО: Устанавливаем напоминания для существующих пользователей ###
        await schedule_reminders_for_user(chat_id, context)


async def calculate_and_send_calories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id); user_profile = user_profiles_data.get(user_id)
    if not user_profile: await update.message.reply_text("❗ Ваш профиль не настроен. Начните с /start."); return
    targets = await calculate_target_calories_and_pfc(user_id)
    if not targets[0]: await update.message.reply_text("❗ Не удалось рассчитать цели. Убедитесь, что ваш вес записан."); return
    latest_weight = get_latest_weight(user_id)
    await update.message.reply_text(f"📊 *Ваш текущий профиль:*\nПол: {user_profile.get('gender').capitalize()}, Возраст: {user_profile.get('age')}, Рост: {user_profile.get('height')} см\nАктивность: {user_profile.get('activity')}, Цель: {user_profile.get('diet_goal', 'баланс')}\nПоследний вес: *{latest_weight}* кг.\n\n✅ *Ваша суточная цель для похудения:*\nКалории: *{targets[0]}* ккал, Белки: *{targets[1]}* г, Жиры: *{targets[2]}* г, Углеводы: *{targets[3]}* г", parse_mode='Markdown')

async def calories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await calculate_and_send_calories(update, context)

async def log_food_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['setup_step'] = SETUP_STATE_LOGGING_FOOD_AWAITING_INPUT
    context.user_data['current_food_log_session_items'] = []
    reply_markup = ReplyKeyboardMarkup([["Готово"]], resize_keyboard=True)
    await update.message.reply_text("🍽️ Вводите продукты по одному. Когда закончите, нажмите 'Готово'.", reply_markup=reply_markup)

async def progress_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id); data = load_json_data(WEIGHT_LOG_FILE)
    if chat_id not in data or not data[chat_id]: await update.message.reply_text("⚠️ Прогресс пока пуст. Запиши свой вес."); return
    log = data[chat_id]; sorted_log = sorted(log.items(), key=lambda x: x[0])
    text = "📈 Прогресс веса по дням:\n"; dates = [datetime.datetime.strptime(d, "%Y-%m-%d").date() for d, w in sorted_log]; weights = [w for d, w in sorted_log]
    for date_obj, weight in zip(dates, weights): text += f"▫️ {date_obj.strftime('%d.%m.%Y')}: {weight} кг\n"
    await update.message.reply_text(text)
    if len(dates) < 2: return
    plt.figure(figsize=(10, 6)); plt.plot(dates, weights, marker='o', linestyle='-'); plt.title("График веса", fontsize=16); plt.xlabel("Дата"); plt.ylabel("Вес (кг)"); plt.grid(True); plt.gcf().autofmt_xdate();
    buf = io.BytesIO(); plt.savefig(buf, format='png', bbox_inches='tight'); buf.seek(0); plt.close()
    await update.message.reply_photo(buf)

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await generate_and_send_menu(update, context, num_days=1)

async def weekly_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("На 3 дня", callback_data="gen_menu:3"), InlineKeyboardButton("На 5 дней", callback_data="gen_menu:5"), InlineKeyboardButton("На 7 дней", callback_data="gen_menu:7")]])
    await update.message.reply_text("На сколько дней вы хотите получить меню?", reply_markup=keyboard)

### ВСТАВЬТЕ ЭТОТ КОД ВМЕСТО СТАРОЙ ФУНКЦИИ generate_and_send_menu ###

async def generate_and_send_menu(update_or_query: Update | CallbackQuery, context: ContextTypes.DEFAULT_TYPE, num_days: int):
    # Добавляем проверку типа объекта, чтобы код работал и с командами, и с кнопками
    if isinstance(update_or_query, Update):
        # Вызов пришел от команды
        chat_id = update_or_query.effective_chat.id
        user_id = update_or_query.effective_user.id
        sender = context.bot # Отправлять сообщения будет сам бот
    else:
        # Вызов пришел от инлайн-кнопки (CallbackQuery)
        query = update_or_query
        chat_id = query.message.chat.id
        user_id = query.from_user.id
        sender = query # Отвечать будет объект query, чтобы редактировать сообщение

    targets = await calculate_target_calories_and_pfc(user_id)
    if not targets[0]:
        await context.bot.send_message(chat_id=chat_id, text="❗ Сначала настройте профиль (/start) и запишите свой вес.")
        return

    # При нажатии кнопки лучше отредактировать старое сообщение, а не слать новое
    if isinstance(sender, CallbackQuery):
        await sender.edit_message_text(text=f"📊 Генерирую ваше персональное меню на {num_days} {'день' if num_days == 1 else 'дня'}...")
    else:
        await sender.send_message(chat_id=chat_id, text=f"📊 Генерирую ваше персональное меню на {num_days} {'день' if num_days == 1 else 'дня'}...")

    # Здесь происходит вызов ВАШЕЙ функции
    user_profile = user_profiles_data.get(str(user_id), {})
    pfc_targets = {'p': targets[1], 'f': targets[2], 'c': targets[3]}
    menu_data = await generate_personalized_menu_with_llm(user_profile, targets[0], pfc_targets, num_days=num_days)

    if not menu_data or not menu_data.get('weekly_plan'):
        await context.bot.send_message(chat_id=chat_id, text="❗ Не удалось сгенерировать меню.")
        return

    context.user_data['last_weekly_menu'] = menu_data
    for day_index, day_menu in enumerate(menu_data['weekly_plan']):
        total_cals = sum(m.get('total_calories', 0) for m in day_menu['meals'])
        total_p = sum(m.get('total_protein', 0) for m in day_menu['meals'])
        total_f = sum(m.get('total_fat', 0) for m in day_menu['meals'])
        total_c = sum(m.get('total_carbs', 0) for m in day_menu['meals'])
        await context.bot.send_message(chat_id=chat_id, text=f"🍽️ *{day_menu.get('day_name', 'Ваше меню')}*\nИтог: *К ~{total_cals} | Б {total_p}г | Ж {total_f}г | У {total_c}г*", parse_mode='Markdown')
        for meal_index, meal in enumerate(day_menu['meals']):
            response_text = f"*{meal.get('meal_name', 'Прием пищи')}*\nКБЖУ: *{meal.get('total_calories', 0)} | {meal.get('total_protein', 0)} | {meal.get('total_fat', 0)} | {meal.get('total_carbs', 0)}*"
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Заменить", callback_data=f"replace:{day_index}:{meal_index}"), InlineKeyboardButton("📖 Рецепт", callback_data=f"recipe:{day_index}:{meal_index}")]])
            await context.bot.send_message(chat_id=chat_id, text=response_text, reply_markup=keyboard, parse_mode='Markdown')

    if menu_data.get('shopping_list'):
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Показать список покупок", callback_data="show_shopping_list")]])
        await context.bot.send_message(chat_id=chat_id, text="Меню сгенерировано. Показать итоговый список покупок?", reply_markup=keyboard)

async def prefs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id); profile = user_profiles_data.get(user_id, {})
    prefs = ", ".join(profile.get('preferences', [])) or "пока нет"; excls = ", ".join(profile.get('exclusions', [])) or "пока нет"
    text = f"⚙️ *Управление вашими предпочтениями*\n\n👍 *Любимые продукты*: {prefs}\n👎 *Нелюбимые*: {excls}"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("👍 Добавить любимый", callback_data="prefs:add_pref"), InlineKeyboardButton("👎 Добавить нелюбимый", callback_data="prefs:add_excl")], [InlineKeyboardButton("🗑️ Очистить списки", callback_data="prefs:clear_all")]])
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode='Markdown')

async def fridge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['setup_step'] = SETUP_STATE_AWAITING_FRIDGE_INGREDIENTS
    await update.message.reply_text("🧊 Перечислите через запятую продукты, которые у вас есть, и я попробую придумать из них блюдо.")


# --- Обработчики ---
async def inline_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    data = query.data
    if data.startswith("gen_menu:"):
        num_days = int(data.split(':')[1]); await query.edit_message_text(f"Принято! Генерирую меню на {num_days} дней...")
        await generate_and_send_menu(query, context, num_days); return
    if data == "show_shopping_list":
        menu_data = context.user_data.get('last_weekly_menu')
        if not menu_data or not menu_data.get('shopping_list'): await query.edit_message_text("Не удалось найти список покупок."); return
        shopping_list = menu_data.get('shopping_list', [])
        shopping_list_text = "🛒 *Ваш итоговый список покупок:*\n\n" + "\n".join(f"• {item}" for item in shopping_list)
        await query.edit_message_text(text=shopping_list_text, parse_mode='Markdown'); return
    if data.startswith("prefs:"):
        action = data.split(':')[1]; user_id = str(query.from_user.id)
        if action == "add_pref":
            context.user_data['setup_step'] = SETUP_STATE_ADDING_PREFERENCE; await query.message.reply_text("Какой продукт добавить в любимые?")
        elif action == "add_excl":
            context.user_data['setup_step'] = SETUP_STATE_ADDING_EXCLUSION; await query.message.reply_text("Какой продукт добавить в нелюбимые?")
        elif action == "clear_all":
            user_profiles_data[user_id]['preferences'] = []; user_profiles_data[user_id]['exclusions'] = []
            with open(USER_PROFILES_FILE, "w", encoding="utf-8") as f: json.dump(user_profiles_data, f, indent=4)
            await query.message.reply_text("✅ Ваши списки предпочтений очищены.")
        await query.edit_message_text(text=query.message.text); return
    if data.startswith("recipe:") or data.startswith("replace:"):
        try: action, day_index_str, meal_index_str = data.split(':'); day_index, meal_index = int(day_index_str), int(meal_index_str)
        except ValueError: await query.edit_message_text("Ошибка: неверные данные кнопки."); return
        menu_data = context.user_data.get('last_weekly_menu')
        if not menu_data or day_index >= len(menu_data['weekly_plan']) or meal_index >= len(menu_data['weekly_plan'][day_index]['meals']):
            await query.edit_message_text("Меню устарело, сгенерируйте новое."); return
        meal = menu_data['weekly_plan'][day_index]['meals'][meal_index]
        if action == 'recipe':
            recipe_text = meal.get('recipe', 'Рецепт не найден.')
            await context.bot.send_message(chat_id=query.message.chat_id, text=f"📖 *Рецепт для \"{meal.get('meal_name')}\"*: \n\n{recipe_text}", parse_mode='Markdown')
        elif action == 'replace':
            await query.edit_message_text(f"🔄 Ищу замену для *{meal['meal_name']}*...", parse_mode='Markdown')
            user_profile = user_profiles_data.get(str(query.from_user.id), {})
            replacement_meal = await generate_personalized_menu_with_llm(user_profile, None, None, meal_to_replace=meal)
            if not replacement_meal: await query.edit_message_text(f"Не удалось найти замену для *{meal['meal_name']}*.", parse_mode='Markdown'); return
            context.user_data['last_weekly_menu']['weekly_plan'][day_index]['meals'][meal_index] = replacement_meal
            original_meal_type = meal.get('meal_name').split('(')[0].strip()
            new_meal_name_only = replacement_meal.get('meal_name', 'Блюдо').replace(original_meal_type, "").strip()
            new_text = f"{original_meal_type}: *{new_meal_name_only}*\nКБЖУ: *{replacement_meal.get('total_calories',0)} | {replacement_meal.get('total_protein',0)} | {replacement_meal.get('total_fat',0)} | {replacement_meal.get('total_carbs',0)}*"
            new_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Заменить", callback_data=f"replace:{day_index}:{meal_index}"), InlineKeyboardButton("📖 Рецепт", callback_data=f"recipe:{day_index}:{meal_index}")]])
            await query.edit_message_text(text=new_text, reply_markup=new_keyboard, parse_mode='Markdown')

async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    handler = MAIN_MENU_HANDLERS.get(text)
    if handler:
        await handler(update, context)
        return

    chat_id = update.effective_chat.id
    chat_id_str = str(chat_id)
    user_id = str(update.effective_user.id)
    current_setup_step = context.user_data.get('setup_step')

    if text.lower().startswith("вес"):
        try: weight_value = float(text[3:].strip().replace(',', '.')); save_weight(chat_id_str, weight_value); await update.message.reply_text(f"✅ Вес сохранён: {weight_value} кг")
        except (ValueError, IndexError): await update.message.reply_text("❗ Неверный формат. Пример: Вес 80.5")
        return

    if current_setup_step is not None and current_setup_step != SETUP_STATE_LOGGING_FOOD_AWAITING_INPUT:
        if current_setup_step == SETUP_STATE_GENDER:
            if text.lower() in ['мужской', 'женский']: context.user_data['profile_gender'] = text.lower(); context.user_data['setup_step'] = SETUP_STATE_AGE; await update.message.reply_text("Сколько вам лет?", reply_markup=ReplyKeyboardRemove())
            else: await update.message.reply_text("❗ Пожалуйста, выберите пол, используя кнопки.")
        elif current_setup_step == SETUP_STATE_AGE:
            try:
                age = int(text)
                if 10 <= age <= 120: context.user_data['profile_age'] = age; context.user_data['setup_step'] = SETUP_STATE_HEIGHT; await update.message.reply_text("Какой у вас рост в см?")
                else: await update.message.reply_text("❗ Пожалуйста, введите реальный возраст (от 10 до 120).")
            except ValueError: await update.message.reply_text("❗ Пожалуйста, введите возраст числом.")
        elif current_setup_step == SETUP_STATE_HEIGHT:
            try:
                height = int(text)
                if 50 <= height <= 250: context.user_data['profile_height'] = height; context.user_data['setup_step'] = SETUP_STATE_WEIGHT_INITIAL; await update.message.reply_text("Какой ваш текущий вес в кг?")
                else: await update.message.reply_text("❗ Пожалуйста, введите реальный рост (от 50 до 250 см).")
            except ValueError: await update.message.reply_text("❗ Пожалуйста, введите рост числом.")
        elif current_setup_step == SETUP_STATE_WEIGHT_INITIAL:
            try:
                weight = float(text.replace(',', '.'));
                if 20 <= weight <= 300: save_weight(chat_id_str, weight); context.user_data['setup_step'] = SETUP_STATE_ACTIVITY; await update.message.reply_text("Какой у вас уровень физической активности? (число от 1 до 5)")
                else: await update.message.reply_text("❗ Пожалуйста, введите реальный вес (от 20 до 300 кг).")
            except ValueError: await update.message.reply_text("❗ Пожалуйста, введите вес числом (можно с точкой).")
        elif current_setup_step == SETUP_STATE_ACTIVITY:
            try:
                activity = int(text)
                if 1 <= activity <= 5:
                    context.user_data['profile_activity'] = activity; context.user_data['setup_step'] = SETUP_STATE_DIET_GOAL
                    keyboard = [["Сбалансированное похудение"], ["Похудение с акцентом на мышцы"], ["Активное жиросжигание (Низкоугл.)"]]
                    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
                    await update.message.reply_text("Отлично! И последний шаг...", reply_markup=reply_markup, parse_mode='Markdown')
                else: await update.message.reply_text("❗ Введите число от 1 до 5.")
            except ValueError: await update.message.reply_text("❗ Пожалуйста, введите уровень активности числом.")
        elif current_setup_step == SETUP_STATE_DIET_GOAL:
            goal_map = {"сбалансированное похудение": "баланс", "похудение с акцентом на мышцы": "белок", "активное жиросжигание (низкоугл.)": "низкоугл"}
            diet_goal = goal_map.get(text.lower())
            if diet_goal:
                user_profiles_data[chat_id_str] = {'gender': context.user_data.get('profile_gender'), 'age': context.user_data.get('profile_age'), 'height': context.user_data.get('profile_height'), 'activity': context.user_data.get('profile_activity'), 'diet_goal': diet_goal, 'preferences': [], 'exclusions': []}
                with open(USER_PROFILES_FILE, "w", encoding="utf-8") as f: json.dump(user_profiles_data, f, indent=4)
                for key in list(context.user_data.keys()):
                    if key.startswith('profile_') or key == 'setup_step': context.user_data.pop(key, None)
                
                await update.message.reply_text("✅ Ваш профиль полностью настроен!")
                # ### ИЗМЕНЕНО: Устанавливаем напоминания после настройки профиля ###
                await schedule_reminders_for_user(chat_id, context)
                await calculate_and_send_calories(update, context)
                await update.message.reply_text("Теперь вы можете использовать основные функции. Напоминания о воде и взвешивании включены автоматически.", reply_markup=MAIN_REPLY_MARKUP)
            else: await update.message.reply_text("❗ Пожалуйста, выберите один из вариантов с помощью кнопок.")
        return

    # ... (остальная часть handle_text_messages без изменений) ...


### НОВЫЙ КОД: Функции-колбэки для напоминаний ###
async def send_water_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет напоминание о воде."""
    job = context.job
    await context.bot.send_message(job.chat_id, text="💧 Не забудьте выпить стакан воды!")

async def check_and_send_weigh_in_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверяет, взвесился ли пользователь сегодня, и если нет - напоминает."""
    job = context.job
    chat_id_str = str(job.chat_id)
    today_iso = datetime.date.today().isoformat()
    
    weight_data = load_json_data(WEIGHT_LOG_FILE)
    user_weight_log = weight_data.get(chat_id_str, {})
    
    if today_iso not in user_weight_log:
        await context.bot.send_message(job.chat_id, text="⚖️ Напоминаю: сегодня нужно взвеситься и записать свой вес! (Пример: `вес 80.5`)")


# --- ЕДИНЫЕ ОПРЕДЕЛЕНИЯ ДЛЯ МЕНЮ ---
MAIN_MENU_HANDLERS = {
    "Показать меню на день": menu_command,
    "Меню на неделю": weekly_menu_command,
    "Рассчитать КБЖУ": calories_command,
    "Прогресс веса": progress_command,
    "Записать еду": log_food_command,
    "Предпочтения": prefs_command,
    "Что в холодильнике?": fridge_command,
}

main_keyboard_layout = [
    ["Показать меню на день", "Меню на неделю"],
    ["Рассчитать КБЖУ", "Прогресс веса"],
    ["Записать еду"],
    ["Предпочтения", "Что в холодильнике?"]
]
MAIN_REPLY_MARKUP = ReplyKeyboardMarkup(main_keyboard_layout, resize_keyboard=True)

# ===== ИЗМЕНЕННАЯ ФУНКЦИЯ MAIN =====
def main() -> None:
    """Запускает бота в режиме вебхука и включает очередь задач."""
    global user_profiles_data
    user_profiles_data = load_json_data(USER_PROFILES_FILE)
    
    persistence = PicklePersistence(filepath=PERSISTENCE_FILE)
    job_queue = JobQueue()

    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not TOKEN:
        raise ValueError("Не найден токен TELEGRAM_BOT_TOKEN в переменных окружения")

    app = ApplicationBuilder().token(TOKEN).persistence(persistence).job_queue(job_queue).build()

    # ### ИЗМЕНЕНО: Удалены команды для напоминаний ###
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("prefs", prefs_command))
    app.add_handler(CommandHandler("fridge", fridge_command))
    
    app.add_handler(CallbackQueryHandler(inline_button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))
    
    # --- НАСТРОЙКИ ВЕБХУКА ---
    PORT = int(os.environ.get('PORT', 8443))
    RENDER_EXTERNAL_URL = os.environ.get('RENDER_EXTERNAL_URL')
    if not RENDER_EXTERNAL_URL:
        raise ValueError("Не найдена переменная RENDER_EXTERNAL_URL.")

    print("Бот запускается в режиме вебхука с автоматическими напоминаниями...")
    
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        secret_token=TOKEN.split(':')[-1],
        webhook_url=RENDER_EXTERNAL_URL
    )

if __name__ == "__main__":
    main()
