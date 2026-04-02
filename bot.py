"""
Telegram бот астролог + Таро.

Возможности:
  1. Натальная карта через kerykeion (Swiss Ephemeris) — точный расчёт.
  2. Прогнозы на день/неделю/месяц с учётом транзитов.
  3. Астропрофиль: свои данные + данные других людей.
  4. Свободный AI-диалог: персонализированный астрологический ассистент.
  5. Таро: расклады, Аркан дня, синтез с астрологией.
"""

import os
import sys
import asyncio
import logging
import re
import json
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
from tarot_engine import (
    draw_cards, draw_single_arcana, format_card, format_spread,
    build_tarot_prompt_for_astro, build_arcana_day_prompt,
)
from database import AstroDatabase
from keyboards import (
    main_menu, settings_menu, cancel_menu,
    forecast_period_kb,
    partners_list_kb, no_partners_kb,
    partner_actions_kb, confirm_delete_kb,
    tarot_button, profile_select_kb,
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

# ── Контекст для кнопок «Подробнее» и «Таро» ────────────────────────────────
# Хранит последний ответ бота для каждого пользователя
last_bot_context: dict[int, dict] = {}
# Хранит выбранный профиль (None = свой, partner_id = чужой)
active_profile: dict[int, int | None] = {}

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


class PartnerAdd(StatesGroup):
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


def _get_astro_context(user: dict) -> tuple[str, str, str]:
    """Возвращает (natal_summary, transits_text, current_city)."""
    chart = user.get('chart', {})
    natal_summary = build_natal_summary(chart)
    try:
        transits = get_current_transits()
        transits_text = format_transits_text(transits)
    except Exception:
        transits_text = "(транзиты недоступны)"
    current_city = user.get('current_city') or user.get('city', 'не указан')
    return natal_summary, transits_text, current_city


def _build_system_prompt(user: dict) -> str:
    chart = user.get('chart', {})
    natal_summary, transits_text, current_city = _get_astro_context(user)
    current_tz = user.get('current_timezone') or user.get('timezone', 'UTC')
    now = datetime.now()

    system_prompt = f"""Ты — профессиональный астролог-консультант с глубокими знаниями западной астрологии.
Ты ведёшь персональный диалог с человеком, чью натальную карту ты знаешь.

═══ НАТАЛЬНАЯ КАРТА КЛИЕНТА ═══
{natal_summary}

Солнечный знак: {chart.get('sun_sign', '?')}
Лунный знак: {chart.get('moon_sign', '?')}
Асцендент: {chart.get('ascendant', '?')}

═══ ТЕКУЩАЯ АСТРОЛОГИЧЕСКАЯ ОБСТАНОВКА ═══
Дата: {now.strftime('%d.%m.%Y')}, время: {now.strftime('%H:%M')} UTC
Текущие транзиты планет:
{transits_text}

═══ МЕСТОПОЛОЖЕНИЕ КЛИЕНТА ═══
Регион: {current_city} (часовой пояс: {current_tz})

═══ ПРАВИЛА ПОВЕДЕНИЯ ═══
1. Ты ВСЕГДА опираешься на натальную карту клиента при ответах.
2. Ты учитываешь текущие транзиты и их аспекты к натальным планетам.
3. Ты даёшь строго ИНДИВИДУАЛЬНЫЕ советы — не общие гороскопы.
4. Отвечай КРАТКО и ДРУЖЕЛЮБНО — как друг-астролог. Используй эмодзи.
5. НЕ упоминай планеты, аспекты, дома в основном ответе — просто дай совет.
6. Если предстоит трудный период — предупреди мягко, поддержи, скажи когда наладится.
7. Длина ответа: 50–200 слов максимум. Кратко и по делу.
8. Отвечай на русском языке.
9. Не используй HTML-разметку — обычный текст с эмодзи."""

    return system_prompt


# ── /start ────────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user = db.get_user(message.from_user.id)
    if user:
        chart = user.get('chart', {})
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

    chat_histories.pop(message.from_user.id, None)

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
                        chart_override: dict = None, location_override: str = None):
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

    prompt = (
        f"Ты астролог. Составь прогноз на {date_label}.\n\n"
        f"Натальная карта:\n{build_natal_summary(chart)}\n\n"
        f"Текущие транзиты:\n{transits_text}\n\n"
        f"Текущее местоположение: {current_city} (часовой пояс: {current_tz})\n\n"
        f"Структура прогноза:\n{structure}\n\n"
        f"ВАЖНО: Учитывай взаимодействие транзитных планет с натальными. "
        f"Тон: конкретный, практичный. Учитывай местоположение."
    )

    result = await ask_groq(prompt, max_tokens=1500)
    if result:
        # Сохраняем контекст для кнопки «Расклад Таро»
        last_bot_context[user_id] = {
            "type": "forecast",
            "period": period,
            "response": result,
            "chart": chart,
        }
        await msg.edit_text(result, reply_markup=tarot_button())
    else:
        await msg.edit_text("⚠️ Не удалось получить прогноз. Попробуй позже.")


# ── Астропрофиль (замена «Партнёры») ─────────────────────────────────────────

@dp.message(F.text == "🪐 Астропрофиль")
async def astro_profile_menu(message: types.Message):
    user = db.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала введи свои данные рождения:", reply_markup=settings_menu())
        return

    partners = db.get_partners(message.from_user.id)
    chart = user.get('chart', {})

    # Сбрасываем активный профиль на «свой»
    active_profile[message.from_user.id] = None

    text = (
        f"🪐 <b>Астропрофиль</b>\n\n"
        f"👤 <b>Ты</b>: ☉ {chart.get('sun_sign', '?')} · "
        f"☽ {chart.get('moon_sign', '?')} · ↑ {chart.get('ascendant', '?')}\n\n"
    )
    if partners:
        text += f"👥 Сохранённые люди: {len(partners)}\n"
        text += "Выбери профиль для вопросов и раскладов:"
    else:
        text += "Добавь людей, чтобы смотреть их данные и совместимость."

    await message.answer(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=profile_select_kb(partners)
    )


# Обработка «Партнёры» для обратной совместимости
@dp.message(F.text == "👥 Партнёры")
async def partners_redirect(message: types.Message):
    await astro_profile_menu(message)


@dp.callback_query(F.data == "profile:self")
async def profile_select_self(callback: types.CallbackQuery):
    await callback.answer("Выбран твой профиль ✨")
    active_profile[callback.from_user.id] = None
    user = db.get_user(callback.from_user.id)
    chart = user.get('chart', {})
    await callback.message.edit_text(
        f"🪐 <b>Активный профиль: Ты</b>\n\n"
        f"☉ {chart.get('sun_sign', '?')} · ☽ {chart.get('moon_sign', '?')} · ↑ {chart.get('ascendant', '?')}\n\n"
        f"Теперь все вопросы и расклады будут для тебя.\n"
        f"Просто напиши вопрос или попроси расклад! 🔮",
        parse_mode=ParseMode.HTML
    )


@dp.callback_query(F.data.startswith("profile:person:"))
async def profile_select_person(callback: types.CallbackQuery):
    pid = int(callback.data.split(":")[2])
    partners = db.get_partners(callback.from_user.id)
    p = next((x for x in partners if x['id'] == pid), None)
    if not p:
        await callback.answer("❌ Не найден")
        return

    await callback.answer(f"Выбран профиль: {p['name']}")
    active_profile[callback.from_user.id] = pid
    chart = p.get('chart', {})
    await callback.message.edit_text(
        f"🪐 <b>Активный профиль: {p['name']}</b>\n\n"
        f"☉ {chart.get('sun_sign', '?')} · ☽ {chart.get('moon_sign', '?')} · ↑ {chart.get('ascendant', '?')}\n\n"
        f"Теперь вопросы и расклады учитывают данные {p['name']}.\n"
        f"Просто напиши вопрос или попроси расклад! 🔮",
        parse_mode=ParseMode.HTML,
        reply_markup=partner_actions_kb(pid)
    )


@dp.callback_query(F.data.startswith("partner:view:"))
async def partner_view(callback: types.CallbackQuery):
    pid      = int(callback.data.split(":")[2])
    partners = db.get_partners(callback.from_user.id)
    p        = next((x for x in partners if x['id'] == pid), None)
    await callback.answer()
    if not p:
        await callback.message.answer("❌ Не найден.")
        return

    chart = p.get('chart', {})
    text  = (
        f"👤 <b>{p['name']}</b>\n\n"
        f"📅 Дата: {p.get('birth_date', '?')}\n"
        f"🌍 Город: {p.get('city', '?')}\n"
        f"☉ Солнце: {chart.get('sun_sign', '?')}\n"
        f"☽ Луна: {chart.get('moon_sign', '?')}\n"
        f"↑ Асц: {chart.get('ascendant', '?')}"
    )
    await callback.message.edit_text(text, parse_mode=ParseMode.HTML,
                                      reply_markup=partner_actions_kb(pid))


@dp.callback_query(F.data == "partner:list")
async def partner_back_list(callback: types.CallbackQuery):
    await callback.answer()
    partners = db.get_partners(callback.from_user.id)
    if partners:
        await callback.message.edit_text(
            f"👥 <b>Люди</b> — {len(partners)} чел.\n\nВыбери кого-нибудь:",
            parse_mode=ParseMode.HTML,
            reply_markup=partners_list_kb(partners)
        )
    else:
        await callback.message.edit_text(
            "👥 Список людей пуст.",
            reply_markup=no_partners_kb()
        )


@dp.callback_query(F.data == "partner:add")
async def partner_add_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(PartnerAdd.name)
    await callback.message.answer("Как зовут человека? (имя или прозвище)",
                                   reply_markup=cancel_menu())


@dp.message(PartnerAdd.name)
async def partner_add_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("❌ Введи имя:")
        return
    await state.update_data(partner_name=name)
    await state.set_state(PartnerAdd.date)
    await message.answer(f"Дата рождения {name}:\n<code>ДД.ММ.ГГГГ</code>",
                         parse_mode=ParseMode.HTML)


@dp.message(PartnerAdd.date)
async def partner_add_date(message: types.Message, state: FSMContext):
    ok, err = validate_date(message.text)
    if not ok:
        await message.answer(f"❌ {err}\n\nПопробуй снова:")
        return
    await state.update_data(partner_date=message.text.strip())
    await state.set_state(PartnerAdd.time)
    await message.answer(
        "Время рождения:\n<code>ЧЧ:ММ</code>\n\nНе знаешь — напиши <code>не знаю</code>",
        parse_mode=ParseMode.HTML
    )


@dp.message(PartnerAdd.time)
async def partner_add_time(message: types.Message, state: FSMContext):
    ok, err, norm = validate_time(message.text)
    if not ok:
        await message.answer(f"❌ {err}\n\nПопробуй снова:")
        return
    await state.update_data(partner_time=norm)
    await state.set_state(PartnerAdd.city)
    await message.answer("Город рождения?")


@dp.message(PartnerAdd.city)
async def partner_add_city(message: types.Message, state: FSMContext):
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
        f"✅ <b>{data['partner_name']}</b> добавлен!\n\n"
        f"<pre>{chart_text}</pre>",
        parse_mode=ParseMode.HTML, reply_markup=partner_actions_kb(pid)
    )


@dp.callback_query(F.data.startswith("partner:compat:"))
async def partner_compat(callback: types.CallbackQuery):
    pid      = int(callback.data.split(":")[2])
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
        f"<i>Их карта: ☉ {p_chart.get('sun_sign','?')} · ☽ {p_chart.get('moon_sign','?')}</i>"
    )
    await callback.message.answer(text, parse_mode=ParseMode.HTML)

    prompt = build_compatibility_prompt(my_chart, p_chart, "Я", name, compat)
    interp = await ask_groq(prompt, max_tokens=1000)
    if interp:
        await send_long(callback.message, interp, parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("partner:natal:"))
async def partner_natal(callback: types.CallbackQuery):
    pid      = int(callback.data.split(":")[2])
    await callback.answer()

    partners = db.get_partners(callback.from_user.id)
    p        = next((x for x in partners if x['id'] == pid), None)
    if not p:
        await callback.message.answer("❌ Не найден.")
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


@dp.callback_query(F.data.startswith("partner:forecast:"))
async def partner_forecast(callback: types.CallbackQuery):
    pid = int(callback.data.split(":")[2])
    await callback.answer()

    partners = db.get_partners(callback.from_user.id)
    p        = next((x for x in partners if x['id'] == pid), None)
    if not p:
        await callback.message.answer("❌ Не найден.")
        return

    chart = p.get('chart', {})
    city  = p.get('city', '?')
    await _do_forecast(
        callback.message, callback.from_user.id, "today",
        chart_override=chart, location_override=city
    )


@dp.callback_query(F.data.startswith("partner:delete:"))
async def partner_delete_confirm(callback: types.CallbackQuery):
    pid = int(callback.data.split(":")[2])
    await callback.answer()
    partners = db.get_partners(callback.from_user.id)
    p = next((x for x in partners if x['id'] == pid), None)
    name = p.get('name', '?') if p else '?'
    await callback.message.edit_text(
        f"Удалить <b>{name}</b>?",
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_delete_kb(pid)
    )


@dp.callback_query(F.data.startswith("partner:confirm_delete:"))
async def partner_delete_do(callback: types.CallbackQuery):
    pid = int(callback.data.split(":")[2])
    await callback.answer()
    db.delete_partner(pid, callback.from_user.id)

    # Если удалён активный профиль — сбросить на себя
    if active_profile.get(callback.from_user.id) == pid:
        active_profile[callback.from_user.id] = None

    partners = db.get_partners(callback.from_user.id)
    if partners:
        await callback.message.edit_text(
            f"✅ Удалено.\n\n👥 <b>Люди</b>:",
            parse_mode=ParseMode.HTML,
            reply_markup=partners_list_kb(partners)
        )
    else:
        await callback.message.edit_text(
            "✅ Удалено. Список пуст.",
            reply_markup=no_partners_kb()
        )


# ══════════════════════════════════════════════════════════════════════════════
# ── ТАРО ─────────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "tarot:spread")
async def handle_tarot_from_forecast(callback: types.CallbackQuery):
    """
    Кнопка «🃏 Расклад Таро» под прогнозом/ответом бота.
    Дополняет предыдущий астрологический ответ раскладом.
    """
    uid = callback.from_user.id
    await callback.answer("🃏 Тяну карты...")

    user = db.get_user(uid)
    if not user:
        await callback.message.answer("Сначала введи данные рождения.", reply_markup=settings_menu())
        return

    ctx = last_bot_context.get(uid, {})
    astro_response = ctx.get("response", "")
    chart = ctx.get("chart") or user.get('chart', {})

    natal_summary, transits_text, current_city = _get_astro_context(user)

    # Определяем тип расклада
    if ctx.get("type") == "forecast":
        # Аркан дня — дополнение к прогнозу
        card = draw_single_arcana()
        prompt = build_arcana_day_prompt(card, natal_summary, transits_text, astro_response)
        card_display = format_card(card)

        msg = await callback.message.answer(f"🃏 Тяну Аркан дня...\n\n{card_display}")
        result = await ask_groq(prompt, max_tokens=800)
        if result:
            await msg.edit_text(f"🌟 <b>Аркан дня</b>\n\n{card_display}\n\n{result}",
                                parse_mode=ParseMode.HTML)
        else:
            await msg.edit_text(f"🌟 Аркан дня: {card_display}\n\n⚠️ Не удалось интерпретировать.")
    else:
        # Расклад из 3 карт — бот придумывает позиции под контекст
        cards = draw_cards(3)

        # Генерируем позиции на основе контекста
        question = ctx.get("question", "общий вопрос")
        positions_prompt = (
            f"Человек задал вопрос: «{question}»\n"
            f"Придумай 3 позиции для расклада Таро из трёх карт, "
            f"которые помогут ответить на этот вопрос.\n"
            f"Формат: просто 3 строки, каждая — название позиции (3-5 слов).\n"
            f"Например:\n"
            f"Суть ситуации\n"
            f"Что поможет\n"
            f"К чему это ведёт\n"
            f"Ответь только 3 строки, без нумерации и пояснений."
        )
        positions_text = await ask_groq(positions_prompt, max_tokens=100)
        if positions_text:
            positions = [line.strip() for line in positions_text.strip().split('\n') if line.strip()][:3]
        else:
            positions = ["Суть ситуации", "Совет", "Результат"]

        # Дополняем до 3 если меньше
        while len(positions) < 3:
            positions.append(f"Позиция {len(positions) + 1}")

        spread_display = format_spread(cards, positions)
        msg = await callback.message.answer(f"🃏 <b>Расклад Таро</b>\n\n{spread_display}",
                                            parse_mode=ParseMode.HTML)

        # Интерпретация с астрологией
        interp_prompt = build_tarot_prompt_for_astro(
            cards, positions, natal_summary, transits_text,
            astro_response, question
        )
        result = await ask_groq(interp_prompt, max_tokens=1200)
        if result:
            await callback.message.answer(f"✨ <b>Интерпретация</b>\n\n{result}",
                                          parse_mode=ParseMode.HTML)


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
        await message.answer(
            f"⚙️ <b>Настройки</b>\n\n"
            f"📅 Дата: {user.get('birth_date', '?')}\n"
            f"🕐 Время: {user.get('birth_time', 'не указано')}\n"
            f"🌍 Город рождения: {user.get('city', '?')}\n"
            f"🕰 Timezone: {user.get('timezone', '?')}{loc_str}\n"
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
        "<b>🪐 Астропрофиль</b> — твои данные и данные других людей\n"
        "<b>📍 Мой регион</b> — текущее местоположение для прогнозов\n"
        "<b>⚙️ Настройки</b> — изменить данные рождения\n"
        "<b>📜 История</b> — последние прогнозы\n\n"
        "🃏 <b>Таро</b> — расклады доступны через кнопку после ответов бота\n\n"
        "💬 <b>Свободный диалог</b> — просто напиши мне вопрос!\n"
        "Я отвечу с учётом твоей натальной карты и текущих транзитов.",
        parse_mode=ParseMode.HTML, reply_markup=main_menu()
    )


# ── Свободный AI-диалог ──────────────────────────────────────────────────────

@dp.message()
async def free_chat(message: types.Message, state: FSMContext):
    """
    Обработчик свободных текстовых сообщений.
    """
    current_state = await state.get_state()
    if current_state is not None:
        return

    user = db.get_user(message.from_user.id)
    if not user:
        await message.answer(
            "🔮 Чтобы я мог дать тебе персональный совет, "
            "мне нужны твои данные рождения.\n\n"
            "Нажми '✏️ Изменить данные' чтобы начать!",
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

    # Определяем, есть ли активный профиль другого человека
    uid = message.from_user.id
    target_pid = active_profile.get(uid)
    target_chart = None
    target_name = None

    if target_pid:
        partners = db.get_partners(uid)
        p = next((x for x in partners if x['id'] == target_pid), None)
        if p:
            target_chart = p.get('chart', {})
            target_name = p.get('name', '?')

    # Строим системный промпт
    system_prompt = _build_system_prompt(user)

    # Если активен чужой профиль — дополняем промпт
    if target_chart and target_name:
        target_summary = build_natal_summary(target_chart)
        system_prompt += f"""

═══ АКТИВНЫЙ ПРОФИЛЬ: {target_name} ═══
{target_summary}
Солнце: {target_chart.get('sun_sign', '?')}, Луна: {target_chart.get('moon_sign', '?')}, Асц: {target_chart.get('ascendant', '?')}

Пользователь сейчас задаёт вопросы о {target_name}. 
Учитывай натальные данные {target_name} при ответах.
Если вопрос о совместимости — сравнивай карту клиента с картой {target_name}."""

    # Добавляем сообщение пользователя в историю
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

        # Сохраняем контекст для Таро
        last_bot_context[uid] = {
            "type": "chat",
            "response": response,
            "question": user_text,
            "chart": target_chart or user.get('chart', {}),
        }

        await msg.delete()
        # Отправляем с кнопкой «Расклад Таро»
        await send_long_with_tarot(message, response)
    else:
        await msg.edit_text(
            "⚠️ Не удалось получить ответ. Попробуй ещё раз через минуту."
        )


async def send_long_with_tarot(message: types.Message, text: str):
    """Отправить текст, к последнему сообщению прикрепить кнопку Таро."""
    parts = []
    for i in range(0, len(text), 4000):
        parts.append(text[i:i + 4000])

    for i, part in enumerate(parts):
        if i == len(parts) - 1:
            # Последняя часть — с кнопкой
            await message.answer(part, reply_markup=tarot_button())
        else:
            await message.answer(part)


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
