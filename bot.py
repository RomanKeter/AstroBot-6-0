"""
Telegram бот астролог.

Возможности:
  1. Натальная карта через kerykeion (Swiss Ephemeris) — точный расчёт.
  2. Прогнозы на день/неделю/месяц с учётом транзитов.
  3. Партнёры: совместимость, карты, прогнозы — через свободный диалог.
  4. Свободный AI-диалог: дружеский, краткий, с кнопкой «Подробнее».
  5. Нечёткий поиск партнёров по имени (склонения русского языка).
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
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from groq import AsyncGroq

from astro_engine import (
    calculate_natal_chart, format_chart_text, format_full_chart_text,
    build_houses_prompt, build_partner_prompt,
    build_compatibility_prompt, get_transits, calculate_compatibility,
    build_natal_summary, get_current_transits, format_transits_text,
    calculate_transit_aspects, analyze_element_balance,
)
from database import AstroDatabase
from keyboards import (
    main_menu, settings_menu, cancel_menu,
    forecast_period_kb,
    partners_list_kb, no_partners_kb,
    partner_actions_kb, confirm_delete_kb,
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

# ── История диалогов (in-memory) ──────────────────────────────────────────────
MAX_HISTORY = 20
chat_histories: dict[int, list[dict]] = defaultdict(list)

# ── Хранение контекста для кнопки «Подробнее» ────────────────────────────────
# Ключ: f"{user_id}:{message_id}" → dict с контекстом ответа
detail_context: dict[str, dict] = {}
MAX_DETAIL_CONTEXTS = 50  # Максимум записей на пользователя

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


async def send_long(message: types.Message, text: str, reply_markup=None, **kwargs):
    """Отправить длинный текст, разбив на части. Кнопку ставим только на последнюю часть."""
    parts = []
    for i in range(0, len(text), 4000):
        parts.append(text[i:i + 4000])

    sent_messages = []
    for idx, part in enumerate(parts):
        is_last = (idx == len(parts) - 1)
        rm = reply_markup if is_last else None
        sent = await message.answer(part, reply_markup=rm, **kwargs)
        sent_messages.append(sent)

    return sent_messages


def _detail_button(user_id: int, msg_id: int) -> InlineKeyboardMarkup:
    """Создать кнопку «Подробнее» с callback_data."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Подробнее", callback_data=f"detail:{user_id}:{msg_id}")]
    ])


def _save_detail_context(user_id: int, msg_id: int, context: dict):
    """Сохранить контекст для кнопки Подробнее."""
    key = f"{user_id}:{msg_id}"
    detail_context[key] = context

    # Очистка старых записей для этого пользователя
    user_keys = [k for k in detail_context if k.startswith(f"{user_id}:")]
    if len(user_keys) > MAX_DETAIL_CONTEXTS:
        # Удаляем самые старые
        for old_key in sorted(user_keys)[:len(user_keys) - MAX_DETAIL_CONTEXTS]:
            detail_context.pop(old_key, None)


def _build_system_prompt(user: dict, partners: list = None) -> str:
    """
    Системный промпт: дружеский, краткий, без астро-терминов в ответе.
    Астрология используется ВНУТРЕННЕ для формирования ответа.
    """
    chart = user.get('chart', {})
    natal_summary = build_natal_summary(chart)

    current_city = user.get('current_city') or user.get('city', 'не указан')
    current_tz = user.get('current_timezone') or user.get('timezone', 'UTC')

    try:
        transits = get_current_transits()
        transits_text = format_transits_text(transits)

        # Транзитные аспекты к натальным планетам
        natal_planets = chart.get('planets', {})
        transit_aspects = calculate_transit_aspects(natal_planets, transits)
        transit_aspects_text = ""
        if transit_aspects:
            lines = []
            for a in transit_aspects:
                lines.append(f"  Тр.{a['transit_planet']} {a['aspect']} нат.{a['natal_planet']} ({a['nature']})")
            transit_aspects_text = "\n".join(lines)
    except Exception:
        transits_text = "(транзиты недоступны)"
        transit_aspects_text = ""

    # Баланс стихий
    try:
        balance = analyze_element_balance(chart)
        balance_text = (f"Доминирующая стихия: {balance['dominant_element']}, "
                       f"слабая: {balance['weak_element']}, "
                       f"крест: {balance['dominant_quality']}")
    except Exception:
        balance_text = ""

    now = datetime.now()

    # Информация о партнёрах
    partners_info = ""
    if partners:
        p_lines = []
        for p in partners:
            p_chart = p.get('chart', {})
            p_lines.append(
                f"  - {p['name']}: ☉{p_chart.get('sun_sign','?')} ☽{p_chart.get('moon_sign','?')} "
                f"Асц:{p_chart.get('ascendant','?')}"
            )
        partners_info = "\n\nЛюДИ В ОКРУЖЕНИИ КЛИЕНТА (добавленные):\n" + "\n".join(p_lines)
        partners_info += ("\n\nВАЖНО: Когда клиент упоминает имя — ищи совпадение среди этих людей. "
                         "Учитывай все формы имени (склонения): Лена/Лены/Леной/Лену = одно имя. "
                         "Если клиент спрашивает о ком-то из списка — используй их натальную карту для ответа.")

    system_prompt = f"""Ты — дружеский астрологический помощник. Ты общаешься как близкий друг, который хорошо разбирается в астрологии.

═══ НАТАЛЬНАЯ КАРТА КЛИЕНТА (для внутреннего анализа) ═══
{natal_summary}

Солнечный знак: {chart.get('sun_sign', '?')}
Лунный знак: {chart.get('moon_sign', '?')}
Асцендент: {chart.get('ascendant', '?')}
{balance_text}

═══ ТЕКУЩАЯ АСТРОЛОГИЧЕСКАЯ ОБСТАНОВКА ═══
Дата: {now.strftime('%d.%m.%Y')}, время: {now.strftime('%H:%M')} UTC
Транзиты:
{transits_text}

Транзитные аспекты к натальной карте клиента:
{transit_aspects_text or '(нет значимых аспектов)'}
{partners_info}

═══ МЕСТОПОЛОЖЕНИЕ ═══
Регион: {current_city} ({current_tz})

═══ ПРАВИЛА ОТВЕТА ═══
1. Отвечай КРАТКО (3-7 предложений), дружелюбно, с эмодзи 😊✨🌟💫
2. НЕ упоминай названия планет, аспектов, домов, транзитов в ответе!
   ПЛОХО: "Твоя Луна в Скорпионе создаёт квадратуру с транзитным Сатурном..."
   ХОРОШО: "Сейчас может ощущаться внутреннее напряжение, но это временно 💪"
3. Говори о чувствах, ситуациях и практических советах — НЕ об астрологических механизмах.
4. Если предстоит сложный период — ОБЯЗАТЕЛЬНО:
   - Предупреди мягко и с поддержкой
   - Скажи когда примерно станет легче
   - Дай 1-2 конкретных совета как пережить
   - Подбодри! 🫂
5. Если спрашивают о конкретном человеке — ответь про отношения с ним/ней простым языком.
6. Для вопросов совместимости — расскажи простыми словами, без астро-терминов.
7. Отвечай на русском, используй эмодзи уместно, но не перебарщивай.
8. Тон: как лучший друг, который всегда поддержит и подскажет.
9. Если клиент спрашивает о выборе времени — дай конкретные даты/дни."""

    return system_prompt


def _build_detail_prompt(original_message: str, response: str, user: dict, partners_context: str = "") -> str:
    """Промпт для генерации подробного астрологического обоснования."""
    chart = user.get('chart', {})
    natal_summary = build_natal_summary(chart)

    try:
        transits = get_current_transits()
        transits_text = format_transits_text(transits)
    except Exception:
        transits_text = ""

    return f"""Ты профессиональный астролог. Тебе нужно ОБОСНОВАТЬ свой предыдущий ответ с помощью астрологических данных.

НАТАЛЬНАЯ КАРТА КЛИЕНТА:
{natal_summary}

ТЕКУЩИЕ ТРАНЗИТЫ:
{transits_text}

{partners_context}

ВОПРОС КЛИЕНТА БЫЛ:
{original_message}

ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ БЫЛ:
{response}

ЗАДАЧА: Объясни астрологическое обоснование этого ответа. Укажи:
- Какие планеты и аспекты влияют на ситуацию
- Какие транзиты сейчас активны и как они взаимодействуют с натальной картой
- Какие дома затронуты
- Почему ты дал именно такой совет

Используй профессиональную астрологическую терминологию (западная традиция).
Формат: структурированный, с заголовками. Объём: 200-400 слов."""


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
            f"Просто напиши мне свой вопрос — я отвечу как друг, "
            f"а если захочешь узнать астрологические детали, "
            f"нажми кнопку «Подробнее» под ответом 🔮",
            reply_markup=main_menu()
        )
    else:
        await message.answer(
            "🔮 Привет! Я твой астрологический помощник!\n\n"
            "Нажми '✏️ Изменить данные' чтобы ввести дату рождения, "
            "и я смогу давать тебе персональные советы ✨",
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
        f"Теперь просто пиши мне любые вопросы! 🔮",
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
    elif period == "tomorrow":
        tmr        = now + timedelta(days=1)
        date_label = f"завтра, {tmr.strftime('%d.%m.%Y')}"
    elif period == "week":
        end        = now + timedelta(days=7)
        date_label = f"неделю ({now.strftime('%d.%m')}–{end.strftime('%d.%m.%Y')})"
    elif period == "month":
        date_label = f"месяц ({now.strftime('%B %Y')})"
    else:
        date_label = custom_date

    msg = await message.answer(f"🔮 Составляю прогноз на {date_label}...")

    try:
        transits = get_current_transits()
        transits_text = format_transits_text(transits)
    except Exception:
        transits_text = ""

    prompt = (
        f"Ты дружеский астролог-помощник. Составь прогноз на {date_label}.\n\n"
        f"Натальная карта (для анализа, НЕ упоминай в ответе):\n{build_natal_summary(chart)}\n\n"
        f"Текущие транзиты (для анализа, НЕ упоминай в ответе):\n{transits_text}\n\n"
        f"Местоположение: {current_city} ({current_tz})\n\n"
        f"ПРАВИЛА:\n"
        f"- Пиши как друг, с эмодзи\n"
        f"- НЕ упоминай планеты, аспекты, дома\n"
        f"- Пиши о чувствах, событиях, практических советах\n"
        f"- Если будет сложный период — предупреди мягко и скажи когда станет легче\n"
        f"- Объём: 5-10 предложений"
    )

    result = await ask_groq(prompt, max_tokens=800)
    if result:
        await msg.delete()
        sent = await send_long(message, result)

        # Добавляем кнопку «Подробнее» к последнему сообщению
        if sent:
            last_msg = sent[-1]
            _save_detail_context(user_id, last_msg.message_id, {
                'question': f"Прогноз на {date_label}",
                'response': result,
                'chart': chart,
            })
            # Редактируем последнее сообщение, добавляя кнопку
            try:
                await last_msg.edit_text(
                    last_msg.text,
                    reply_markup=_detail_button(user_id, last_msg.message_id)
                )
            except Exception:
                pass
    else:
        await msg.edit_text("⚠️ Не удалось получить прогноз. Попробуй позже.")


# ── Партнёры ──────────────────────────────────────────────────────────────────

@dp.message(F.text == "👥 Люди")
@dp.message(F.text == "👥 Партнёры")
async def partners_menu(message: types.Message):
    user = db.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала введи свои данные рождения:", reply_markup=settings_menu())
        return

    partners = db.get_partners(message.from_user.id)
    if partners:
        names = ", ".join(p['name'] for p in partners)
        await message.answer(
            f"👥 <b>Твои люди</b> ({len(partners)}):\n{names}\n\n"
            f"Ты можешь просто написать мне вопрос с упоминанием имени, "
            f"например:\n"
            f"• «Как лучше договориться с Леной?»\n"
            f"• «Какая совместимость у меня с Сашей?»\n"
            f"• «Расскажи про характер Димы»\n\n"
            f"Или выбери из списка:",
            parse_mode=ParseMode.HTML,
            reply_markup=partners_list_kb(partners)
        )
    else:
        await message.answer(
            "👥 У тебя пока нет добавленных людей.\n\n"
            "Добавь человека, чтобы я мог анализировать "
            "ваши отношения, совместимость и многое другое!",
            reply_markup=no_partners_kb()
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
            f"👥 <b>Твои люди</b> — {len(partners)} чел.\n\nВыбери:",
            parse_mode=ParseMode.HTML,
            reply_markup=partners_list_kb(partners)
        )
    else:
        await callback.message.edit_text(
            "👥 Список пуст.",
            reply_markup=no_partners_kb()
        )


@dp.callback_query(F.data == "partner:add")
async def partner_add_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(PartnerAdd.name)
    await callback.message.answer("Как зовут этого человека? (имя или прозвище)",
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
        f"✅ <b>{data['partner_name']}</b> добавлен(а)!\n\n"
        f"<pre>{chart_text}</pre>\n\n"
        f"Теперь ты можешь спрашивать о {data['partner_name']} в свободном чате! 💬",
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

    # Краткий дружеский ответ
    msg = await callback.message.answer("✨ Анализирую вашу совместимость...")

    prompt = (
        f"Ты дружеский астрологический помощник. Расскажи о совместимости двух людей.\n\n"
        f"Карта 1 (мой клиент):\n{build_natal_summary(my_chart)}\n\n"
        f"Карта 2 ({name}):\n{build_natal_summary(p_chart)}\n\n"
        f"Балл совместимости: {compat['score']}% ({compat['level']})\n\n"
        f"ПРАВИЛА:\n"
        f"- Пиши как друг, с эмодзи\n"
        f"- НЕ упоминай планеты, аспекты, дома\n"
        f"- Расскажи простыми словами: что их объединяет, где могут быть трения\n"
        f"- Дай практический совет\n"
        f"- Объём: 5-8 предложений"
    )

    result = await ask_groq(prompt, max_tokens=600)
    if result:
        await msg.delete()
        sent = await send_long(callback.message, f"{compat['emoji']} Совместимость с {name}: {compat['score']}%\n\n{result}")
        if sent:
            last_msg = sent[-1]
            _save_detail_context(callback.from_user.id, last_msg.message_id, {
                'question': f"Совместимость с {name}",
                'response': result,
                'chart': my_chart,
                'partner_chart': p_chart,
                'partner_name': name,
                'compat': compat,
            })
            try:
                await last_msg.edit_text(
                    last_msg.text,
                    reply_markup=_detail_button(callback.from_user.id, last_msg.message_id)
                )
            except Exception:
                pass
    else:
        await msg.edit_text("⚠️ Не удалось получить анализ.")


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
        f"Удалить <b>{name}</b> из списка?",
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_delete_kb(pid)
    )


@dp.callback_query(F.data.startswith("partner:confirm_delete:"))
async def partner_delete_do(callback: types.CallbackQuery):
    pid = int(callback.data.split(":")[2])
    await callback.answer()
    db.delete_partner(pid, callback.from_user.id)

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


# ── Кнопка «Подробнее» ───────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("detail:"))
async def handle_detail_button(callback: types.CallbackQuery):
    """Обработка нажатия кнопки «Подробнее» — выдать астрологическое обоснование."""
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("❌ Ошибка")
        return

    target_user_id = int(parts[1])
    target_msg_id = int(parts[2])

    # Проверяем что это тот же пользователь
    if callback.from_user.id != target_user_id:
        await callback.answer("Это не твоя кнопка 😊")
        return

    key = f"{target_user_id}:{target_msg_id}"
    ctx = detail_context.get(key)

    if not ctx:
        await callback.answer("Контекст устарел, задай вопрос заново")
        return

    await callback.answer("🔮 Готовлю астрологическое обоснование...")

    user = db.get_user(callback.from_user.id)
    if not user:
        await callback.message.answer("❌ Данные не найдены.")
        return

    # Собираем контекст партнёров если есть
    partners_ctx = ""
    if 'partner_chart' in ctx:
        p_name = ctx.get('partner_name', '?')
        partners_ctx = f"\nКарта партнёра ({p_name}):\n{build_natal_summary(ctx['partner_chart'])}"

    prompt = _build_detail_prompt(
        original_message=ctx.get('question', ''),
        response=ctx.get('response', ''),
        user=user,
        partners_context=partners_ctx
    )

    msg = await callback.message.answer("🔍 Анализирую астрологические данные...")
    result = await ask_groq(prompt, max_tokens=1200)

    if result:
        await msg.delete()
        await send_long(callback.message, f"🔍 <b>Астрологическое обоснование:</b>\n\n{result}",
                       parse_mode=ParseMode.HTML)
    else:
        await msg.edit_text("⚠️ Не удалось получить обоснование. Попробуй позже.")


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
        "🔮 <b>Что я умею:</b>\n\n"
        "💬 <b>Просто напиши мне</b> — я отвечу как друг с учётом твоей карты\n"
        "🔮 <b>Моя карта</b> — полная натальная карта\n"
        "📅 <b>Прогноз</b> — на день/неделю/месяц\n"
        "👥 <b>Люди</b> — добавь людей и спрашивай о них\n"
        "📍 <b>Регион</b> — для более точных прогнозов\n\n"
        "💡 <b>Примеры вопросов:</b>\n"
        "• «Как пройдёт мой день?»\n"
        "• «Как мне лучше общаться с Леной?»\n"
        "• «Какая у нас совместимость с Сашей?»\n"
        "• «Когда лучше начать новый проект?»\n\n"
        "🔍 Под каждым ответом есть кнопка <b>«Подробнее»</b> — "
        "нажми, чтобы увидеть астрологическое обоснование!",
        parse_mode=ParseMode.HTML, reply_markup=main_menu()
    )


# ── Свободный AI-диалог ──────────────────────────────────────────────────────

@dp.message()
async def free_chat(message: types.Message, state: FSMContext):
    """
    Свободный диалог с дружеским тоном.
    Автоматически находит упомянутых партнёров и включает их данные в контекст.
    """
    current_state = await state.get_state()
    if current_state is not None:
        return

    user = db.get_user(message.from_user.id)
    if not user:
        await message.answer(
            "🔮 Чтобы я мог помочь, мне нужны твои данные рождения.\n\n"
            "Нажми '✏️ Изменить данные' чтобы начать! ✨",
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

    uid = message.from_user.id

    # Находим упомянутых партнёров в тексте
    partners = db.get_partners(uid)
    mentioned_partners = db.find_partners_in_text(uid, user_text)

    # Строим системный промпт с учётом партнёров
    system_prompt = _build_system_prompt(user, partners)

    # Если упомянуты партнёры, добавляем их данные в текущее сообщение
    enriched_text = user_text
    if mentioned_partners:
        partner_info_parts = []
        for p in mentioned_partners:
            p_chart = p.get('chart', {})
            partner_info_parts.append(
                f"[СИСТЕМНАЯ ИНФОРМАЦИЯ: {p['name']} — "
                f"☉{p_chart.get('sun_sign','?')} ☽{p_chart.get('moon_sign','?')} "
                f"Асц:{p_chart.get('ascendant','?')}]"
            )
        enriched_text = user_text + "\n\n" + "\n".join(partner_info_parts)

    chat_histories[uid].append({"role": "user", "content": enriched_text})

    if len(chat_histories[uid]) > MAX_HISTORY:
        chat_histories[uid] = chat_histories[uid][-MAX_HISTORY:]

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(chat_histories[uid])

    response = await ask_groq_chat(messages, max_tokens=800)

    if response:
        chat_histories[uid].append({"role": "assistant", "content": response})
        if len(chat_histories[uid]) > MAX_HISTORY:
            chat_histories[uid] = chat_histories[uid][-MAX_HISTORY:]

        await msg.delete()
        sent = await send_long(message, response)

        # Добавляем кнопку «Подробнее»
        if sent:
            last_msg = sent[-1]

            # Сохраняем контекст для кнопки
            ctx = {
                'question': user_text,
                'response': response,
                'chart': user.get('chart', {}),
            }
            if mentioned_partners:
                ctx['partner_chart'] = mentioned_partners[0].get('chart', {})
                ctx['partner_name'] = mentioned_partners[0].get('name', '?')

            _save_detail_context(uid, last_msg.message_id, ctx)

            try:
                await last_msg.edit_text(
                    last_msg.text,
                    reply_markup=_detail_button(uid, last_msg.message_id)
                )
            except Exception:
                pass
    else:
        await msg.edit_text(
            "⚠️ Не удалось получить ответ. Попробуй через минуту."
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
