"""
Telegram бот астролог.

Возможности:
  1. Натальная карта через kerykeion (Swiss Ephemeris) — точный расчёт.
  2. Прогнозы на день/неделю/месяц с учётом транзитов.
  3. Астропрофили: «Я» + другие люди, взаимодействие между ними.
  4. Свободный AI-диалог: персонализированный ассистент,
     который знает натальные карты, текущие транзиты,
     и даёт индивидуальные советы.
  5. Расклады Таро через текстовый диалог (синергия с астрологией).
"""

import os
import sys
import asyncio
import logging
import re
from datetime import datetime, timedelta
from collections import defaultdict

import httpx
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from groq import AsyncGroq

from astro_engine import (
    calculate_natal_chart, format_chart_text, format_full_chart_text,
    build_houses_prompt, build_partner_prompt,
    build_compatibility_prompt, get_transits, calculate_compatibility,
    build_natal_summary, get_current_transits, format_transits_text,
)
from database import AstroDatabase
from keyboards import (
    main_menu, settings_menu, cancel_menu,
    forecast_period_kb,
    astroprofile_list_kb, no_profiles_kb,
    profile_actions_kb, confirm_delete_kb,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

TG_TOKEN = os.getenv("TG_BOT_TOKEN")
GROQ_KEY = os.getenv("GROQ_API_KEY")

if not TG_TOKEN:
    logger.critical("TG_BOT_TOKEN не задан!")
    sys.exit(1)
if not GROQ_KEY:
    logger.warning("GROQ_API_KEY не задан — AI-функции будут недоступны.")

bot         = Bot(token=TG_TOKEN)
storage     = MemoryStorage()
dp          = Dispatcher(storage=storage)
http_client = httpx.AsyncClient(timeout=15.0)
groq_client = AsyncGroq(api_key=GROQ_KEY, http_client=http_client) if GROQ_KEY else None
db          = AstroDatabase()

# ── История диалогов (in-memory, последние N сообщений на пользователя) ──────
MAX_HISTORY = 20
chat_histories: dict[int, list[dict]] = defaultdict(list)

# ── Активный астропрофиль (user_id → {"type": "self"} или {"type": "other", "id": ..., "name": ..., "chart": ...})
active_profiles: dict[int, dict] = {}

# ── TimezoneFinder ────────────────────────────────────────────────────────────
_tf = None

def get_tf():
    global _tf
    if _tf is None:
        try:
            from timezonefinder import TimezoneFinder
            _tf = TimezoneFinder()
            logger.info("TimezoneFinder инициализирован.")
        except ImportError:
            _tf = False
            logger.warning("timezonefinder не установлен, timezone через API.")
    return _tf


# ── FSM состояния ─────────────────────────────────────────────────────────────

class Registration(StatesGroup):
    date = State()
    time = State()
    city = State()


class ProfileAdd(StatesGroup):
    name = State()
    date = State()
    time = State()
    city = State()


class LocationUpdate(StatesGroup):
    city = State()


class ForecastCustom(StatesGroup):
    date = State()


# ── Геокодинг ─────────────────────────────────────────────────────────────────

async def geocode_city(city_name: str) -> dict | None:
    try:
        resp = await http_client.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": city_name, "format": "json", "limit": 1, "addressdetails": 0},
            headers={"User-Agent": "AstroBot/1.0"},
        )
        resp.raise_for_status()
        results = resp.json()
        if not results:
            return None

        lat          = float(results[0]["lat"])
        lon          = float(results[0]["lon"])
        display_name = results[0].get("display_name", city_name).split(",")[0].strip()

        tz = "UTC"
        tf = get_tf()
        if tf:
            tz = tf.timezone_at(lat=lat, lng=lon) or "UTC"
        else:
            try:
                r2 = await http_client.get(
                    "https://timeapi.io/api/TimeZone/coordinate",
                    params={"latitude": lat, "longitude": lon},
                    timeout=5.0,
                )
                if r2.status_code == 200:
                    tz = r2.json().get("timeZone", "UTC")
            except Exception:
                pass

        return {"lat": lat, "lon": lon, "timezone": tz, "display_name": display_name}

    except Exception as e:
        logger.error(f"Geocoding error '{city_name}': {e}")
        return None


# ── Вспомогательные функции ───────────────────────────────────────────────────

def validate_date(text: str) -> tuple:
    m = re.match(r'^(\d{2})\.(\d{2})\.(\d{4})$', text.strip())
    if not m:
        return False, "Формат: ДД.ММ.ГГГГ (пример: 15.05.1990)"
    day, month, year = map(int, m.groups())
    try:
        datetime(year, month, day)
        if year < 1900 or year > 2030:
            return False, "Год должен быть между 1900 и 2030"
        return True, ""
    except ValueError:
        return False, "Неверная дата"


def validate_time(text: str) -> tuple:
    t = text.strip().lower()
    if t in ['не знаю', 'нет', '-', 'неизвестно']:
        return True, "", "12:00"
    m = re.match(r'^(\d{1,2}):(\d{2})$', t)
    if not m:
        return False, "Формат: ЧЧ:ММ (пример: 14:30) или 'не знаю'", ""
    h, mn = map(int, m.groups())
    if not (0 <= h <= 23 and 0 <= mn <= 59):
        return False, "Время от 00:00 до 23:59", ""
    return True, "", f"{h:02d}:{mn:02d}"


def _deduplicate_name(existing_names: list[str], desired_name: str) -> str:
    """Если имя уже есть — добавить цифру: Саша → Саша1, Саша2..."""
    if desired_name not in existing_names:
        return desired_name
    i = 1
    while f"{desired_name}{i}" in existing_names:
        i += 1
    return f"{desired_name}{i}"


async def ask_groq(prompt: str, max_tokens: int = 1200) -> str | None:
    if not groq_client:
        return None
    try:
        resp = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content
    except Exception as e:
        logger.error(f"Groq error: {e}")
        return None


async def ask_groq_chat(messages: list[dict], max_tokens: int = 1500) -> str | None:
    """Вызов Groq с полной историей сообщений (system + user/assistant)."""
    if not groq_client:
        return None
    try:
        resp = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.7,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content
    except Exception as e:
        logger.error(f"Groq chat error: {e}")
        return None


async def send_long(message: types.Message, text: str, **kwargs):
    """Отправить длинный текст, разбив на части по 4000 символов."""
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i + 4000], **kwargs)


def _get_active_profile_info(user_id: int) -> dict | None:
    """Возвращает информацию об активном профиле или None."""
    return active_profiles.get(user_id)


def _build_system_prompt(user: dict, profile_info: dict | None = None) -> str:
    """
    Строит системный промпт для персонализированного AI-ассистента.
    Учитывает активный астропрофиль для правильного обращения.
    """
    chart = user.get('chart', {})
    user_natal_summary = build_natal_summary(chart)
    user_name = user.get('first_name') or 'пользователь'

    current_city = user.get('current_city') or user.get('city', 'не указан')
    current_tz = user.get('current_timezone') or user.get('timezone', 'UTC')

    # Текущие транзиты
    try:
        transits = get_current_transits()
        transits_text = format_transits_text(transits)
    except Exception:
        transits_text = "(транзиты недоступны)"

    now = datetime.now()

    # Определяем контекст обращения
    if profile_info and profile_info.get("type") == "other":
        target_name = profile_info["name"]
        target_chart = profile_info.get("chart", {})
        target_natal = build_natal_summary(target_chart)
        addressing_rules = f"""═══ РЕЖИМ: АНАЛИЗ ДРУГОГО ЧЕЛОВЕКА ═══
Ты анализируешь человека по имени {target_name}.
Натальная карта {target_name}:
{target_natal}

Солнечный знак: {target_chart.get('sun_sign', '?')}
Лунный знак: {target_chart.get('moon_sign', '?')}
Асцендент: {target_chart.get('ascendant', '?')}

ПРАВИЛА ОБРАЩЕНИЯ:
- Говори о {target_name} в ТРЕТЬЕМ ЛИЦЕ: "{target_name} сегодня...", "Ему/ей рекомендуется..."
- ВСЕ прогнозы, расклады и советы — для {target_name}, НЕ для пользователя.
- Пользователь спрашивает о {target_name}, отвечай про {target_name}.

Натальная карта пользователя (для контекста взаимодействий):
{user_natal_summary}
"""
    else:
        # Режим «Я» — обращение во втором лице
        target_name = user_name
        addressing_rules = f"""═══ РЕЖИМ: ЛИЧНЫЙ ПРОФИЛЬ ═══
Ты обращаешься к пользователю напрямую, во ВТОРОМ ЛИЦЕ.
Пример: "Вам сегодня рекомендуется...", "У вас сейчас транзит..."

Натальная карта пользователя:
{user_natal_summary}

Солнечный знак: {chart.get('sun_sign', '?')}
Лунный знак: {chart.get('moon_sign', '?')}
Асцендент: {chart.get('ascendant', '?')}
"""

    # Собираем список всех астропрофилей для возможных взаимодействий
    profiles_context = ""
    try:
        partners = db.get_partners(user.get('user_id', 0))
        if partners:
            names = [p['name'] for p in partners]
            profiles_context = f"\n═══ ДОСТУПНЫЕ АСТРОПРОФИЛИ ═══\nЛюди в астропрофилях пользователя: {', '.join(names)}\nПользователь может спросить о взаимодействии между ними или попросить расклад на кого-то из них.\n"
    except Exception:
        pass

    system_prompt = f"""Ты — профессиональный астролог-консультант и таролог с глубокими знаниями западной астрологии и Таро.
Ты ведёшь персональный диалог.

{addressing_rules}

═══ ТЕКУЩАЯ АСТРОЛОГИЧЕСКАЯ ОБСТАНОВКА ═══
Дата: {now.strftime('%d.%m.%Y')}, время: {now.strftime('%H:%M')} UTC
Текущие транзиты планет:
{transits_text}
{profiles_context}
═══ МЕСТОПОЛОЖЕНИЕ ═══
Регион: {current_city} (часовой пояс: {current_tz})

═══ ПРАВИЛА ПОВЕДЕНИЯ ═══
1. ВСЕГДА опирайся на натальную карту при ответах.
2. Учитывай текущие транзиты и их аспекты к натальным планетам.
3. Давай строго ИНДИВИДУАЛЬНЫЕ советы — не общие гороскопы.
4. Если спрашивают о ситуации (бизнес, отношения, здоровье),
   анализируй задействованные дома и планеты, давай конкретный совет.
5. Для выбора оптимального времени — подбирай на основе транзитов и натальной карты.
6. Тон: тёплый, но профессиональный. Конкретные рекомендации.
7. Отвечай на русском языке.
8. Не используй HTML-разметку, пиши текстом с эмодзи.
9. Если спрашивают не по астрологии/Таро — мягко направь разговор обратно.
10. Длина ответа: 150–400 слов.

═══ РАСКЛАДЫ ТАРО ═══
- Если пользователь просит расклад Таро, делай его.
- Выбирай карты (3-7 карт в зависимости от вопроса), описывай каждую.
- ОБЯЗАТЕЛЬНО трактуй карты в СИНЕРГИИ с астрологическим профилем и текущими транзитами.
- Пример: если у человека хороший день для путешествий по транзитам и выпала Колесница или Мир —
  укажи что это взаимно усиливает значение и дай уверенный совет.
- Если карты противоречат астрологии — объясни нюансы.

═══ ВЗАИМОДЕЙСТВИЕ ПРОФИЛЕЙ ═══
- Если пользователь упоминает имя из астропрофилей, анализируй взаимодействие
  между натальными картами (синастрия), давай советы по отношениям.
- Используй карты Таро для углублённого анализа взаимодействий.

═══ ОБЯЗАТЕЛЬНО В КОНЦЕ КАЖДОГО ОТВЕТА ═══
Всегда завершай ответ предложением — что ещё можно посмотреть или сделать.
Примеры:
- "Хотите разобрать эту ситуацию подробнее с помощью расклада Таро? 🃏"
- "Могу посмотреть, какой период будет наиболее благоприятным для этого дела ⏰"
- "Хотите узнать, как текущие транзиты влияют на вашу карьеру? 💼"
- "Могу сделать расклад на совместимость с кем-то из ваших астропрофилей 💑"
Предложение должно быть КОНТЕКСТНЫМ — связано с темой разговора.
"""

    return system_prompt


# ── /start ────────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user = db.get_user(message.from_user.id)
    if user:
        chart = user.get('chart', {})
        # При старте — активный профиль «Я»
        active_profiles[message.from_user.id] = {"type": "self"}
        await message.answer(
            f"✨ С возвращением, {user.get('first_name') or 'друг'}!\n"
            f"Твой знак: {chart.get('sun_sign', '?')}\n\n"
            f"Можешь использовать кнопки меню или просто написать мне свой вопрос — "
            f"я отвечу с учётом твоей натальной карты! 🔮",
            reply_markup=main_menu()
        )
    else:
        await message.answer(
            "🔮 Добро пожаловать в Астро-Бот!\n\n"
            "Нажми '✏️ Изменить данные' чтобы ввести дату рождения.",
            reply_markup=settings_menu()
        )


# ── Регистрация ───────────────────────────────────────────────────────────────

@dp.message(F.text == "❌ Отмена")
async def cancel_any(message: types.Message, state: FSMContext):
    await state.clear()
    user = db.get_user(message.from_user.id)
    await message.answer("Главное меню:", reply_markup=main_menu() if user else settings_menu())


@dp.message(F.text == "✏️ Изменить данные")
async def start_registration(message: types.Message, state: FSMContext):
    await state.set_state(Registration.date)
    await message.answer(
        "Введи дату рождения:\n<code>ДД.ММ.ГГГГ</code>\n\nПример: <code>15.05.1990</code>",
        parse_mode=ParseMode.HTML, reply_markup=cancel_menu()
    )


@dp.message(Registration.date)
async def reg_date(message: types.Message, state: FSMContext):
    ok, err = validate_date(message.text)
    if not ok:
        await message.answer(f"❌ {err}\n\nПопробуй снова:")
        return
    await state.update_data(date=message.text.strip())
    await state.set_state(Registration.time)
    await message.answer(
        "Время рождения:\n<code>ЧЧ:ММ</code>\n\nНе знаешь — напиши <code>не знаю</code>",
        parse_mode=ParseMode.HTML
    )


@dp.message(Registration.time)
async def reg_time(message: types.Message, state: FSMContext):
    ok, err, norm = validate_time(message.text)
    if not ok:
        await message.answer(f"❌ {err}\n\nПопробуй снова:")
        return
    await state.update_data(time=norm)
    await state.set_state(Registration.city)
    await message.answer("Город рождения? (Например: Москва, Берлин, Нью-Йорк)",
                         reply_markup=cancel_menu())


@dp.message(Registration.city)
async def reg_city(message: types.Message, state: FSMContext):
    city = message.text.strip()
    if len(city) < 2:
        await message.answer("❌ Слишком короткое название. Попробуй снова:")
        return

    msg = await message.answer("🔍 Ищу город...")
    geo = await geocode_city(city)
    if not geo:
        await msg.edit_text(
            f"❌ Город «{city}» не найден.\nПопробуй написать на русском или английском."
        )
        return

    data = await state.get_data()
    day, month, year = map(int, data['date'].split('.'))
    hour, minute     = map(int, data['time'].split(':'))

    chart = calculate_natal_chart(year, month, day, hour, minute,
                                  geo["lat"], geo["lon"], city, geo["timezone"])

    db.save_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        birth_date=f"{year}-{month:02d}-{day:02d}",
        birth_time=data['time'] if data['time'] != '12:00' else None,
        city=geo["display_name"], lat=geo["lat"], lon=geo["lon"],
        chart=chart, timezone=geo["timezone"]
    )
    await state.clear()

    # Очистить историю чата и установить профиль «Я»
    chat_histories.pop(message.from_user.id, None)
    active_profiles[message.from_user.id] = {"type": "self"}

    chart_text = format_chart_text(chart, {'city': geo["display_name"]})
    await msg.edit_text(
        f"✨ <b>Данные сохранены!</b>\n\n<pre>{chart_text}</pre>\n\n"
        f"🕐 Часовой пояс: {geo['timezone']}\n\n"
        f"Теперь ты можешь задавать мне любые вопросы по астрологии — "
        f"я буду отвечать с учётом твоей карты! 🔮",
        parse_mode=ParseMode.HTML, reply_markup=main_menu()
    )


# ── Натальная карта ───────────────────────────────────────────────────────────

@dp.message(F.text == "🔮 Моя карта")
@dp.message(Command("natal"))
async def show_natal(message: types.Message):
    user = db.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала введи данные рождения:", reply_markup=settings_menu())
        return

    chart = user.get('chart', {})
    msg   = await message.answer("🔮 Строю натальную карту...")

    full_text = format_full_chart_text(chart)
    await msg.edit_text(
        f"<b>🔮 Натальная карта</b>\n\n{full_text}",
        parse_mode=ParseMode.HTML
    )

    msg2 = await message.answer("✨ Генерирую интерпретацию 12 домов...")
    interpretation = await ask_groq(build_houses_prompt(chart), max_tokens=2000)
    if interpretation:
        await msg2.delete()
        await send_long(message, interpretation, parse_mode=ParseMode.HTML)
    else:
        await msg2.edit_text("⚠️ Не удалось получить интерпретацию. Попробуй позже.")


# ── Прогноз ───────────────────────────────────────────────────────────────────

@dp.message(F.text == "📅 Прогноз")
@dp.message(Command("forecast"))
async def choose_forecast(message: types.Message):
    user = db.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала введи данные:", reply_markup=settings_menu())
        return
    await message.answer("На какой период нужен прогноз?", reply_markup=forecast_period_kb())


@dp.callback_query(F.data.startswith("forecast:"))
async def handle_forecast_cb(callback: types.CallbackQuery, state: FSMContext):
    period = callback.data.split(":")[1]
    await callback.answer()
    if period == "custom":
        await state.set_state(ForecastCustom.date)
        await callback.message.answer(
            "Введи дату прогноза:\n<code>ДД.ММ.ГГГГ</code>",
            parse_mode=ParseMode.HTML, reply_markup=cancel_menu()
        )
        return
    await _do_forecast(callback.message, callback.from_user.id, period)


@dp.message(ForecastCustom.date)
async def forecast_custom_date(message: types.Message, state: FSMContext):
    ok, err = validate_date(message.text)
    if not ok:
        await message.answer(f"❌ {err}\n\nПопробуй снова:")
        return
    await state.clear()
    await _do_forecast(message, message.from_user.id, "custom", message.text.strip())


async def _do_forecast(message: types.Message, user_id: int,
                        period: str, custom_date: str = None,
                        chart_override: dict = None, location_override: str = None,
                        target_name: str = None):
    """
    Прогноз. Если target_name задан — прогноз для третьего лица.
    """
    user  = db.get_user(user_id)
    if not user:
        return

    chart         = chart_override or user.get('chart', {})
    current_city  = location_override or user.get('current_city') or user.get('city', '?')
    current_tz    = user.get('current_timezone') or user.get('timezone', 'UTC')
    now           = datetime.now()

    if period == "today":
        date_label = f"сегодня, {now.strftime('%d.%m.%Y')}"
        structure  = "🌅 Утро\n☀️ День\n🌙 Вечер\n⭐ Главный совет"
    elif period == "tomorrow":
        tmr        = now + timedelta(days=1)
        date_label = f"завтра, {tmr.strftime('%d.%m.%Y')}"
        structure  = "🌅 Утро\n☀️ День\n🌙 Вечер\n⭐ Главный совет"
    elif period == "week":
        end        = now + timedelta(days=7)
        date_label = f"неделю ({now.strftime('%d.%m')}–{end.strftime('%d.%m.%Y')})"
        structure  = "📌 Атмосфера недели\n💼 Работа\n❤️ Личная жизнь\n💡 Лучшие дни\n⚠️ Осторожные дни\n⭐ Совет"
    elif period == "month":
        date_label = f"месяц ({now.strftime('%B %Y')})"
        structure  = "📌 Атмосфера месяца\n💼 Карьера и финансы\n❤️ Любовь\n🌱 Развитие\n📅 Ключевые периоды\n⭐ Совет"
    else:
        date_label = custom_date
        structure  = "📌 Энергия дня\n💼 Работа\n❤️ Отношения\n⭐ Совет"

    msg = await message.answer(f"🔮 Составляю прогноз на {date_label}...")

    try:
        transits = get_current_transits()
        transits_text = format_transits_text(transits)
    except Exception:
        transits_text = ""

    # Обращение: второе или третье лицо
    if target_name:
        addressing = (
            f"Прогноз для {target_name} (третье лицо). "
            f"Пиши: '{target_name} сегодня...', 'Ему/ей рекомендуется...'. "
            f"В конце предложи посмотреть что-то ещё о {target_name}."
        )
    else:
        addressing = (
            "Прогноз для пользователя (второе лицо). "
            "Пиши: 'Вам сегодня...', 'Вам рекомендуется...'. "
            "В конце предложи что-то ещё — расклад Таро, подробный анализ аспектов и т.д."
        )

    prompt = (
        f"Ты астролог. Составь прогноз на {date_label}.\n\n"
        f"{addressing}\n\n"
        f"Натальная карта:\n{build_natal_summary(chart)}\n\n"
        f"Текущие транзиты:\n{transits_text}\n\n"
        f"Текущее местоположение: {current_city} (часовой пояс: {current_tz})\n\n"
        f"Структура прогноза:\n{structure}\n\n"
        f"ВАЖНО: Учитывай взаимодействие транзитных планет с натальными. "
        f"Тон: конкретный, практичный. Учитывай местоположение."
    )

    result = await ask_groq(prompt, max_tokens=1500)
    if result:
        await msg.edit_text(result, parse_mode=ParseMode.HTML)
    else:
        await msg.edit_text("⚠️ Не удалось получить прогноз. Попробуй позже.")


# ── Астропрофили ──────────────────────────────────────────────────────────────

@dp.message(F.text == "🌟 Астропрофиль")
async def astroprofile_menu(message: types.Message):
    user = db.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала введи свои данные рождения:", reply_markup=settings_menu())
        return

    partners = db.get_partners(message.from_user.id)
    user_name = user.get('first_name') or 'Пользователь'

    # Формируем список: первый — «Я», потом остальные
    await message.answer(
        f"🌟 <b>Астропрофили</b>\n\n"
        f"Выберите профиль для работы.\n"
        f"«Я» — ваш личный профиль.\n"
        f"Остальные — добавленные вами люди.",
        parse_mode=ParseMode.HTML,
        reply_markup=astroprofile_list_kb(user_name, partners)
    )


@dp.callback_query(F.data == "profile:self")
async def profile_select_self(callback: types.CallbackQuery):
    """Выбор своего профиля «Я»."""
    await callback.answer()
    user = db.get_user(callback.from_user.id)
    if not user:
        return

    user_name = user.get('first_name') or 'Пользователь'
    active_profiles[callback.from_user.id] = {"type": "self"}
    # Очистить историю чата при смене профиля
    chat_histories.pop(callback.from_user.id, None)

    await callback.message.edit_text(
        f"🌟 <b>{user_name}</b>, вы выбрали свой астропрофиль.\n\n"
        f"Что бы вы хотели посмотреть сейчас? Можете задать любой вопрос "
        f"по астрологии или попросить расклад Таро 🔮🃏",
        parse_mode=ParseMode.HTML
    )


@dp.callback_query(F.data.startswith("profile:view:"))
async def profile_select_other(callback: types.CallbackQuery):
    """Выбор чужого профиля."""
    pid = int(callback.data.split(":")[2])
    await callback.answer()

    user = db.get_user(callback.from_user.id)
    partners = db.get_partners(callback.from_user.id)
    p = next((x for x in partners if x['id'] == pid), None)
    if not p:
        await callback.message.answer("❌ Профиль не найден.")
        return

    chart = p.get('chart', {})
    name = p.get('name', '?')

    # Установить активный профиль
    active_profiles[callback.from_user.id] = {
        "type": "other",
        "id": pid,
        "name": name,
        "chart": chart,
    }
    # Очистить историю чата при смене профиля
    chat_histories.pop(callback.from_user.id, None)

    brief = (
        f"☉ {chart.get('sun_sign', '?')} · "
        f"☽ {chart.get('moon_sign', '?')} · "
        f"↑ {chart.get('ascendant', '?')}"
    )

    await callback.message.edit_text(
        f"🌟 Вы выбрали <b>{name}</b> ({brief}).\n\n"
        f"Можете задать интересующий вопрос о {name}. "
        f"Например, попросите прогноз, расклад Таро или анализ совместимости 🔮",
        parse_mode=ParseMode.HTML,
        reply_markup=profile_actions_kb(pid)
    )


@dp.callback_query(F.data == "profile:add")
async def profile_add_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(ProfileAdd.name)
    await callback.message.answer("Как зовут человека? (имя или прозвище)",
                                   reply_markup=cancel_menu())


@dp.message(ProfileAdd.name)
async def profile_add_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("❌ Введи имя:")
        return

    # Дедупликация имени
    partners = db.get_partners(message.from_user.id)
    existing_names = [p['name'] for p in partners]
    unique_name = _deduplicate_name(existing_names, name)

    if unique_name != name:
        await message.answer(
            f"ℹ️ Имя «{name}» уже занято. Сохраню как «{unique_name}»."
        )

    await state.update_data(partner_name=unique_name)
    await state.set_state(ProfileAdd.date)
    await message.answer(f"Дата рождения {unique_name}:\n<code>ДД.ММ.ГГГГ</code>",
                         parse_mode=ParseMode.HTML)


@dp.message(ProfileAdd.date)
async def profile_add_date(message: types.Message, state: FSMContext):
    ok, err = validate_date(message.text)
    if not ok:
        await message.answer(f"❌ {err}\n\nПопробуй снова:")
        return
    await state.update_data(partner_date=message.text.strip())
    await state.set_state(ProfileAdd.time)
    await message.answer(
        "Время рождения:\n<code>ЧЧ:ММ</code>\n\nНе знаешь — напиши <code>не знаю</code>",
        parse_mode=ParseMode.HTML
    )


@dp.message(ProfileAdd.time)
async def profile_add_time(message: types.Message, state: FSMContext):
    ok, err, norm = validate_time(message.text)
    if not ok:
        await message.answer(f"❌ {err}\n\nПопробуй снова:")
        return
    await state.update_data(partner_time=norm)
    await state.set_state(ProfileAdd.city)
    await message.answer("Город рождения?")


@dp.message(ProfileAdd.city)
async def profile_add_city(message: types.Message, state: FSMContext):
    city = message.text.strip()
    msg  = await message.answer("🔍 Ищу город...")
    geo  = await geocode_city(city)
    if not geo:
        await msg.edit_text(f"❌ Город «{city}» не найден. Попробуй снова:")
        return

    data = await state.get_data()
    day, month, year = map(int, data['partner_date'].split('.'))
    hour, minute     = map(int, data['partner_time'].split(':'))

    chart = calculate_natal_chart(year, month, day, hour, minute,
                                  geo["lat"], geo["lon"], city, geo["timezone"])

    pid = db.save_partner(
        user_id=message.from_user.id,
        name=data['partner_name'],
        birth_date=f"{year}-{month:02d}-{day:02d}",
        birth_time=data['partner_time'],
        city=geo["display_name"],
        lat=geo["lat"], lon=geo["lon"],
        chart=chart, timezone=geo["timezone"]
    )
    await state.clear()

    chart_text = format_chart_text(chart, {'city': geo["display_name"]})
    await msg.edit_text(
        f"✅ Профиль <b>{data['partner_name']}</b> сохранён!\n\n"
        f"<pre>{chart_text}</pre>\n\n"
        f"Теперь вы можете выбрать этот профиль в разделе «Астропрофиль» "
        f"и задавать вопросы о {data['partner_name']} 🌟",
        parse_mode=ParseMode.HTML, reply_markup=profile_actions_kb(pid)
    )


@dp.callback_query(F.data.startswith("profile:compat:"))
async def profile_compat(callback: types.CallbackQuery):
    pid = int(callback.data.split(":")[2])
    await callback.answer()

    user     = db.get_user(callback.from_user.id)
    partners = db.get_partners(callback.from_user.id)
    p        = next((x for x in partners if x['id'] == pid), None)

    if not user or not p:
        await callback.message.answer("❌ Данные не найдены.")
        return

    my_chart = user.get('chart', {})
    p_chart  = p.get('chart', {})
    name     = p.get('name', '?')
    compat   = calculate_compatibility(my_chart, p_chart)

    aspects_text = "\n".join(f"• {a}" for a in compat['aspects']) or "—"
    text = (
        f"💑 <b>Совместимость с {name}</b>\n\n"
        f"{compat['emoji']} <b>{compat['level']}</b> — {compat['score']}%\n\n"
        f"<b>Аспекты:</b>\n{aspects_text}\n\n"
        f"<i>Карта {name}: ☉ {p_chart.get('sun_sign','?')} · ☽ {p_chart.get('moon_sign','?')}</i>"
    )
    await callback.message.answer(text, parse_mode=ParseMode.HTML)

    prompt = build_compatibility_prompt(my_chart, p_chart, "Я", name, compat)
    interp = await ask_groq(prompt, max_tokens=1000)
    if interp:
        await send_long(callback.message, interp, parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("profile:natal:"))
async def profile_natal(callback: types.CallbackQuery):
    pid = int(callback.data.split(":")[2])
    await callback.answer()

    partners = db.get_partners(callback.from_user.id)
    p        = next((x for x in partners if x['id'] == pid), None)
    if not p:
        await callback.message.answer("❌ Профиль не найден.")
        return

    chart = p.get('chart', {})
    name  = p.get('name', '?')

    msg = await callback.message.answer(f"🔮 Строю карту {name}...")
    full_text = format_full_chart_text(chart)
    await msg.edit_text(
        f"<b>🔮 Натальная карта — {name}</b>\n\n{full_text}",
        parse_mode=ParseMode.HTML
    )

    msg2 = await callback.message.answer("✨ Генерирую интерпретацию...")
    interp = await ask_groq(build_partner_prompt(chart, name), max_tokens=1200)
    if interp:
        await msg2.delete()
        await send_long(callback.message, interp, parse_mode=ParseMode.HTML)
    else:
        await msg2.edit_text("⚠️ Не удалось получить интерпретацию.")


@dp.callback_query(F.data.startswith("profile:forecast:"))
async def profile_forecast(callback: types.CallbackQuery):
    pid = int(callback.data.split(":")[2])
    await callback.answer()

    partners = db.get_partners(callback.from_user.id)
    p        = next((x for x in partners if x['id'] == pid), None)
    if not p:
        await callback.message.answer("❌ Профиль не найден.")
        return

    chart = p.get('chart', {})
    city  = p.get('city', '?')
    name  = p.get('name', '?')
    await _do_forecast(
        callback.message, callback.from_user.id, "today",
        chart_override=chart, location_override=city, target_name=name
    )


@dp.callback_query(F.data.startswith("profile:delete:"))
async def profile_delete_confirm(callback: types.CallbackQuery):
    pid = int(callback.data.split(":")[2])
    await callback.answer()
    partners = db.get_partners(callback.from_user.id)
    p = next((x for x in partners if x['id'] == pid), None)
    name = p.get('name', '?') if p else '?'
    await callback.message.edit_text(
        f"Удалить <b>{name}</b> из астропрофилей?",
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_delete_kb(pid)
    )


@dp.callback_query(F.data.startswith("profile:confirm_delete:"))
async def profile_delete_do(callback: types.CallbackQuery):
    pid = int(callback.data.split(":")[2])
    await callback.answer()
    db.delete_partner(pid, callback.from_user.id)

    # Сбросить активный профиль если удалили активный
    ap = active_profiles.get(callback.from_user.id)
    if ap and ap.get("type") == "other" and ap.get("id") == pid:
        active_profiles[callback.from_user.id] = {"type": "self"}

    user = db.get_user(callback.from_user.id)
    user_name = user.get('first_name', 'Пользователь') if user else 'Пользователь'
    partners = db.get_partners(callback.from_user.id)

    await callback.message.edit_text(
        "✅ Удалено.",
        reply_markup=astroprofile_list_kb(user_name, partners) if partners else no_profiles_kb()
    )


@dp.callback_query(F.data == "profile:list")
async def profile_back_list(callback: types.CallbackQuery):
    await callback.answer()
    user = db.get_user(callback.from_user.id)
    user_name = user.get('first_name', 'Пользователь') if user else 'Пользователь'
    partners = db.get_partners(callback.from_user.id)
    await callback.message.edit_text(
        f"🌟 <b>Астропрофили</b>\n\nВыберите профиль:",
        parse_mode=ParseMode.HTML,
        reply_markup=astroprofile_list_kb(user_name, partners)
    )


# ── Текущее местоположение ────────────────────────────────────────────────────

@dp.message(F.text == "📍 Мой регион")
@dp.message(Command("location"))
async def location_start(message: types.Message, state: FSMContext):
    await state.set_state(LocationUpdate.city)
    await message.answer(
        "Введи текущий город (может отличаться от города рождения).",
        reply_markup=cancel_menu()
    )


@dp.message(LocationUpdate.city)
async def location_save(message: types.Message, state: FSMContext):
    city = message.text.strip()
    msg  = await message.answer("🔍 Определяю...")
    geo  = await geocode_city(city)
    if not geo:
        await msg.edit_text(f"❌ Город «{city}» не найден. Попробуй снова:")
        return
    db.update_current_location(
        user_id=message.from_user.id,
        current_city=geo["display_name"],
        current_lat=geo["lat"],
        current_lon=geo["lon"],
        current_timezone=geo["timezone"]
    )
    await state.clear()
    await msg.edit_text(
        f"✅ Местоположение: {geo['display_name']}\n🕐 {geo['timezone']}",
        reply_markup=main_menu()
    )


# ── Настройки / история / помощь ─────────────────────────────────────────────

@dp.message(F.text == "⚙️ Настройки")
async def show_settings(message: types.Message):
    user = db.get_user(message.from_user.id)
    if user:
        chart   = user.get('chart', {})
        cur     = user.get('current_city')
        loc_str = f"\n📍 Текущий регион: {cur}" if cur else ""
        ap = active_profiles.get(message.from_user.id)
        if ap and ap.get("type") == "other":
            profile_str = f"\n🌟 Активный профиль: {ap['name']}"
        else:
            profile_str = "\n🌟 Активный профиль: Я"
        await message.answer(
            f"⚙️ <b>Настройки</b>\n\n"
            f"📅 Дата: {user.get('birth_date', '?')}\n"
            f"🕐 Время: {user.get('birth_time', 'не указано')}\n"
            f"🌍 Город рождения: {user.get('city', '?')}\n"
            f"🕰 Timezone: {user.get('timezone', '?')}{loc_str}{profile_str}\n"
            f"☉ Знак: {chart.get('sun_sign', '?')}",
            parse_mode=ParseMode.HTML, reply_markup=settings_menu()
        )
    else:
        await message.answer("Данные не введены.", reply_markup=settings_menu())


@dp.message(F.text == "📜 История")
async def show_history(message: types.Message):
    readings = db.get_readings(message.from_user.id, limit=5)
    if not readings:
        await message.answer("📜 История пуста.")
        return
    lines = ["📜 <b>Последние прогнозы:</b>\n"]
    for r in readings:
        lines.append(f"• {r.get('created_at','?')[:10]} — {r.get('reading_type','?')}")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@dp.message(F.text == "🔙 Назад")
async def back_main(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu())


@dp.message(F.text == "❓ Помощь")
@dp.message(Command("help"))
async def show_help(message: types.Message):
    await message.answer(
        "🔮 <b>Команды бота:</b>\n\n"
        "<b>🔮 Моя карта</b> — полная натальная карта + 12 домов\n"
        "<b>📅 Прогноз</b> — прогноз на день / неделю / месяц / дату\n"
        "<b>🌟 Астропрофиль</b> — ваши профили: «Я» и другие люди\n"
        "<b>📍 Мой регион</b> — текущее местоположение для прогнозов\n"
        "<b>⚙️ Настройки</b> — изменить данные рождения\n"
        "<b>📜 История</b> — последние прогнозы\n\n"
        "💬 <b>Свободный диалог</b> — просто напишите вопрос!\n"
        "Я отвечу с учётом натальной карты, транзитов и текущей обстановки.\n\n"
        "🃏 <b>Расклад Таро</b> — попросите в чате: «сделай расклад Таро»\n"
        "Расклад трактуется в синергии с вашим астропрофилем!",
        parse_mode=ParseMode.HTML, reply_markup=main_menu()
    )


# ── Свободный AI-диалог ──────────────────────────────────────────────────────

@dp.message()
async def free_chat(message: types.Message, state: FSMContext):
    """
    Обработчик свободных текстовых сообщений.
    Учитывает активный астропрофиль для правильного контекста.
    """
    current_state = await state.get_state()
    if current_state is not None:
        return

    user = db.get_user(message.from_user.id)
    if not user:
        await message.answer(
            "🔮 Чтобы я мог дать персональный совет, "
            "мне нужны ваши данные рождения.\n\n"
            "Нажмите '✏️ Изменить данные' чтобы начать!",
            reply_markup=settings_menu()
        )
        return

    if not groq_client:
        await message.answer("⚠️ AI-функции временно недоступны.")
        return

    user_text = message.text.strip()
    if not user_text:
        return

    msg = await message.answer("🔮 Думаю...")

    # Получаем активный профиль
    profile_info = _get_active_profile_info(message.from_user.id)

    # Строим системный промпт с учётом профиля
    system_prompt = _build_system_prompt(user, profile_info)

    uid = message.from_user.id
    chat_histories[uid].append({"role": "user", "content": user_text})

    if len(chat_histories[uid]) > MAX_HISTORY:
        chat_histories[uid] = chat_histories[uid][-MAX_HISTORY:]

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(chat_histories[uid])

    response = await ask_groq_chat(messages, max_tokens=1500)

    if response:
        chat_histories[uid].append({"role": "assistant", "content": response})
        if len(chat_histories[uid]) > MAX_HISTORY:
            chat_histories[uid] = chat_histories[uid][-MAX_HISTORY:]

        await msg.delete()
        await send_long(message, response)
    else:
        await msg.edit_text(
            "⚠️ Не удалось получить ответ. Попробуйте ещё раз через минуту."
        )


# ── Запуск ────────────────────────────────────────────────────────────────────

async def main():
    logger.info("Прогрев TimezoneFinder...")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, get_tf)
    logger.info("Бот запускается...")
    try:
        await dp.start_polling(bot)
    finally:
        await http_client.aclose()
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    asyncio.run(main())
