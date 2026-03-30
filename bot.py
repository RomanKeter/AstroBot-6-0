"""
Telegram бот астролог — исправленная версия.
Изменения:
  - Геокодинг через Nominatim вместо хардкод-списка городов
  - Timezone определяется автоматически по координатам
  - Новый раздел: совместимость с партнёром
  - Прогноз учитывает текущее местоположение пользователя
"""

import os
import sys
import asyncio
import logging
import re
from datetime import datetime

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
    calculate_natal_chart, format_chart_text,
    get_transits, calculate_compatibility
)
from database import AstroDatabase
from keyboards import main_menu, settings_menu, cancel_menu

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

bot = Bot(token=TG_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

http_client = httpx.AsyncClient(timeout=10.0)
groq_client = AsyncGroq(api_key=GROQ_KEY, http_client=http_client) if GROQ_KEY else None

db = AstroDatabase()


# ─── FSM состояния ────────────────────────────────────────────────────────────

class Registration(StatesGroup):
    date = State()
    time = State()
    city = State()


class PartnerRegistration(StatesGroup):
    name = State()
    date = State()
    time = State()
    city = State()


class LocationUpdate(StatesGroup):
    city = State()


# ─── Геокодинг ────────────────────────────────────────────────────────────────

async def geocode_city(city_name: str) -> dict | None:
    """
    Получить координаты и timezone города через Nominatim (OpenStreetMap).
    Возвращает {'lat': float, 'lon': float, 'timezone': str, 'display_name': str}
    или None если город не найден.
    """
    try:
        # Шаг 1: координаты через Nominatim
        resp = await http_client.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": city_name,
                "format": "json",
                "limit": 1,
                "addressdetails": 0,
            },
            headers={"User-Agent": "AstroBot/1.0 (telegram bot)"},
        )
        resp.raise_for_status()
        results = resp.json()

        if not results:
            return None

        lat = float(results[0]["lat"])
        lon = float(results[0]["lon"])
        display_name = results[0].get("display_name", city_name).split(",")[0].strip()

        # Шаг 2: timezone по координатам через timezonefinder (без сети)
        try:
            from timezonefinder import TimezoneFinder
            tf = TimezoneFinder()
            tz = tf.timezone_at(lat=lat, lng=lon) or "UTC"
        except ImportError:
            # Запасной вариант: TimeZoneDB API (бесплатный ключ не нужен для простых запросов)
            tz_resp = await http_client.get(
                "https://timeapi.io/api/TimeZone/coordinate",
                params={"latitude": lat, "longitude": lon},
            )
            if tz_resp.status_code == 200:
                tz = tz_resp.json().get("timeZone", "UTC")
            else:
                tz = "UTC"

        return {"lat": lat, "lon": lon, "timezone": tz, "display_name": display_name}

    except Exception as e:
        logger.error(f"Geocoding error for '{city_name}': {e}")
        return None


# ─── Вспомогательные функции ──────────────────────────────────────────────────

def validate_date(text: str) -> tuple:
    pattern = r'^(\d{2})\.(\d{2})\.(\d{4})$'
    match = re.match(pattern, text.strip())
    if not match:
        return False, "Формат: ДД.ММ.ГГГГ (пример: 15.05.1990)"
    day, month, year = map(int, match.groups())
    try:
        datetime(year, month, day)
        if year < 1900 or year > 2026:
            return False, "Год должен быть между 1900 и 2026"
        return True, ""
    except ValueError:
        return False, "Неверная дата (проверь день и месяц)"


def validate_time(text: str) -> tuple:
    text = text.strip().lower()
    if text in ['не знаю', 'нет', '-', 'неизвестно']:
        return True, "", "12:00"
    pattern = r'^(\d{1,2}):(\d{2})$'
    match = re.match(pattern, text)
    if not match:
        return False, "Формат: ЧЧ:ММ (пример: 14:30) или 'не знаю'", ""
    hour, minute = map(int, match.groups())
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return False, "Время должно быть от 00:00 до 23:59", ""
    return True, "", f"{hour:02d}:{minute:02d}"


async def ask_groq(prompt: str, max_tokens: int = 1000) -> str | None:
    if not groq_client:
        return None
    try:
        response = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=max_tokens
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Groq API error: {e}")
        return None


# ─── Команды ──────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user = db.get_user(message.from_user.id)
    if user:
        chart = user.get('chart', {})
        await message.answer(
            f"✨ С возвращением, {user.get('first_name') or 'друг'}!\n"
            f"Твой знак: {chart.get('sun_sign', '?')}",
            reply_markup=main_menu()
        )
    else:
        await message.answer(
            "🔮 Добро пожаловать в Астро-Бот!\n\n"
            "Нажми '✏️ Изменить данные' чтобы начать.",
            reply_markup=settings_menu()
        )


# ─── Регистрация ──────────────────────────────────────────────────────────────

@dp.message(F.text == "❌ Отмена")
async def cancel_registration(message: types.Message, state: FSMContext):
    await state.clear()
    user = db.get_user(message.from_user.id)
    markup = main_menu() if user else settings_menu()
    await message.answer("Главное меню:", reply_markup=markup)


@dp.message(F.text == "✏️ Изменить данные")
async def start_registration(message: types.Message, state: FSMContext):
    await state.set_state(Registration.date)
    await message.answer(
        "Введи дату рождения:\n<code>ДД.ММ.ГГГГ</code>\n\nПример: <code>15.05.1990</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_menu()
    )


@dp.message(Registration.date)
async def process_date(message: types.Message, state: FSMContext):
    is_valid, error_msg = validate_date(message.text)
    if not is_valid:
        await message.answer(f"❌ {error_msg}\n\nПопробуй снова:")
        return
    await state.update_data(date=message.text.strip())
    await state.set_state(Registration.time)
    await message.answer(
        "Теперь время рождения:\n<code>ЧЧ:ММ</code>\n\n"
        "Если не знаешь — напиши <code>не знаю</code>",
        parse_mode=ParseMode.HTML
    )


@dp.message(Registration.time)
async def process_time(message: types.Message, state: FSMContext):
    is_valid, error_msg, normalized_time = validate_time(message.text)
    if not is_valid:
        await message.answer(f"❌ {error_msg}\n\nПопробуй снова:")
        return
    await state.update_data(time=normalized_time)
    await state.set_state(Registration.city)
    await message.answer(
        "В каком городе ты родился?\n(Например: Москва, Берлин, Нью-Йорк)",
        reply_markup=cancel_menu()
    )


@dp.message(Registration.city)
async def process_city(message: types.Message, state: FSMContext):
    city = message.text.strip()
    if len(city) < 2:
        await message.answer("❌ Название города слишком короткое. Попробуй снова:")
        return

    msg = await message.answer("🔍 Ищу город...")

    # Геокодинг — исправление бага с хардкод-списком
    geo = await geocode_city(city)
    if not geo:
        await msg.edit_text(
            f"❌ Город «{city}» не найден.\n"
            "Попробуй написать название на русском или английском языке."
        )
        return

    lat      = geo["lat"]
    lon      = geo["lon"]
    timezone = geo["timezone"]
    display  = geo["display_name"]

    data = await state.get_data()
    day, month, year = map(int, data['date'].split('.'))
    hour, minute     = map(int, data['time'].split(':'))

    chart = calculate_natal_chart(year, month, day, hour, minute, lat, lon, city, timezone)

    db.save_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        birth_date=f"{year}-{month:02d}-{day:02d}",
        birth_time=data['time'] if data['time'] != '12:00' else None,
        city=display,
        lat=lat,
        lon=lon,
        chart=chart,
        timezone=timezone  # исправление бага: сохраняем реальный timezone
    )

    await state.clear()

    chart_text = format_chart_text(chart, {'city': display})
    await msg.edit_text(
        f"✨ <b>Твоя натальная карта</b>\n\n"
        f"<pre>{chart_text}</pre>\n\n"
        f"🕐 Часовой пояс: {timezone}\n\n"
        f"Используй кнопки ниже для прогнозов!",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu()
    )


# ─── Натальная карта ──────────────────────────────────────────────────────────

@dp.message(F.text == "🔮 Моя карта")
@dp.message(Command("natal"))
async def show_natal(message: types.Message):
    user = db.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала введи данные рождения:", reply_markup=settings_menu())
        return

    chart = user.get('chart', {})
    chart_text = format_chart_text(chart, user)
    msg = await message.answer("🔮 Генерирую толкование...")

    prompt = f"""Ты астролог. Дай разбор натальной карты:

Солнце: {chart.get('sun_sign')}
Луна: {chart.get('moon_sign')}
Асцендент: {chart.get('ascendant')}
Город рождения: {user.get('city', '?')}

Структура:
1. Суть личности (Солнце)
2. Эмоции (Луна)
3. Внешность/поведение (Асцендент)
4. 3 сильные качества
5. Совет

Тон: вдохновляющий."""

    interpretation = await ask_groq(prompt)
    if interpretation:
        await msg.edit_text(
            f"<b>🔮 Твоя натальная карта</b>\n\n"
            f"<pre>{chart_text}</pre>\n\n{interpretation}",
            parse_mode=ParseMode.HTML
        )
    else:
        await msg.edit_text(
            f"<b>🔮 Твоя карта</b>\n\n<pre>{chart_text}</pre>",
            parse_mode=ParseMode.HTML
        )


# ─── Прогноз ──────────────────────────────────────────────────────────────────

@dp.message(F.text == "📅 Прогноз")
@dp.message(Command("forecast"))
async def show_forecast(message: types.Message):
    user = db.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала введи данные:", reply_markup=settings_menu())
        return

    chart = user.get('chart', {})
    msg = await message.answer("🔮 Анализирую транзиты...")

    # Учитываем текущее местоположение (если задано)
    current_city = user.get('current_city') or user.get('city', '?')
    current_tz   = user.get('current_timezone') or user.get('timezone', 'UTC')

    transits = get_transits(chart)
    transit_text = ""
    if transits:
        transit_lines = [f"• {t['natal_planet']} в {t['aspect']} к {t['transit_planet']}" for t in transits[:3]]
        transit_text = "\n".join(transit_lines)

    today = datetime.now().strftime("%d.%m.%Y")

    prompt = f"""Ты астролог. Сделай прогноз на {today}.

Натальная карта:
Солнце: {chart.get('sun_sign', '?')}
Луна: {chart.get('moon_sign', '?')}
Асцендент: {chart.get('ascendant', '?')}

Текущее местоположение: {current_city} (часовой пояс: {current_tz})
Город рождения: {user.get('city', '?')}

Активные транзиты:
{transit_text or 'Нет точных аспектов'}

Структура:
🌅 Утро
☀️ День
🌙 Вечер
⭐ Главный совет

Тон: конкретный, практичный. Учитывай местоположение при рекомендациях."""

    result = await ask_groq(prompt)
    if result:
        await msg.edit_text(result, parse_mode=ParseMode.HTML)
    else:
        await msg.edit_text("⚠️ Не удалось получить прогноз. Попробуй позже.")


# ─── Совместимость ────────────────────────────────────────────────────────────

@dp.message(F.text == "💑 Совместимость")
@dp.message(Command("compat"))
async def start_compatibility(message: types.Message, state: FSMContext):
    user = db.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала введи свои данные рождения:", reply_markup=settings_menu())
        return

    # Показать список существующих партнёров
    partners = db.get_partners(message.from_user.id)
    if partners:
        lines = ["👥 <b>Твои партнёры:</b>\n"]
        for i, p in enumerate(partners, 1):
            lines.append(f"{i}. {p.get('name', '?')} — {p.get('birth_date', '?')}")
        lines.append("\nДобавить нового партнёра:")
        await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

    await state.set_state(PartnerRegistration.name)
    await message.answer(
        "Как зовут партнёра?\n(Введи имя или прозвище)",
        reply_markup=cancel_menu()
    )


@dp.message(PartnerRegistration.name)
async def partner_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 1:
        await message.answer("❌ Введи имя:")
        return
    await state.update_data(partner_name=name)
    await state.set_state(PartnerRegistration.date)
    await message.answer(
        f"Дата рождения {name}:\n<code>ДД.ММ.ГГГГ</code>",
        parse_mode=ParseMode.HTML
    )


@dp.message(PartnerRegistration.date)
async def partner_date(message: types.Message, state: FSMContext):
    is_valid, error_msg = validate_date(message.text)
    if not is_valid:
        await message.answer(f"❌ {error_msg}\n\nПопробуй снова:")
        return
    await state.update_data(partner_date=message.text.strip())
    await state.set_state(PartnerRegistration.time)
    await message.answer(
        "Время рождения партнёра:\n<code>ЧЧ:ММ</code>\n\n"
        "Если не знаешь — напиши <code>не знаю</code>",
        parse_mode=ParseMode.HTML
    )


@dp.message(PartnerRegistration.time)
async def partner_time(message: types.Message, state: FSMContext):
    is_valid, error_msg, normalized_time = validate_time(message.text)
    if not is_valid:
        await message.answer(f"❌ {error_msg}\n\nПопробуй снова:")
        return
    await state.update_data(partner_time=normalized_time)
    await state.set_state(PartnerRegistration.city)
    await message.answer(
        "Город рождения партнёра:\n(Например: Киев, Лондон, Токио)",
        reply_markup=cancel_menu()
    )


@dp.message(PartnerRegistration.city)
async def partner_city(message: types.Message, state: FSMContext):
    city = message.text.strip()
    msg = await message.answer("🔍 Ищу город и считаю карту...")

    geo = await geocode_city(city)
    if not geo:
        await msg.edit_text(
            f"❌ Город «{city}» не найден. Попробуй ещё раз:"
        )
        return

    data = await state.get_data()
    day, month, year = map(int, data['partner_date'].split('.'))
    hour, minute     = map(int, data['partner_time'].split(':'))
    name             = data['partner_name']

    p_chart = calculate_natal_chart(
        year, month, day, hour, minute,
        geo["lat"], geo["lon"], city, geo["timezone"]
    )

    db.save_partner(
        user_id=message.from_user.id,
        name=name,
        birth_date=f"{year}-{month:02d}-{day:02d}",
        birth_time=data['partner_time'] if data['partner_time'] != '12:00' else None,
        city=geo["display_name"],
        lat=geo["lat"],
        lon=geo["lon"],
        chart=p_chart,
        timezone=geo["timezone"]
    )

    # Считаем совместимость
    user = db.get_user(message.from_user.id)
    my_chart = user.get('chart', {})
    compat = calculate_compatibility(my_chart, p_chart)

    await state.clear()

    aspects_text = "\n".join(f"• {a}" for a in compat['aspects']) if compat['aspects'] else "Нет данных"

    msg_text = (
        f"💑 <b>Совместимость с {name}</b>\n\n"
        f"{compat['emoji']} Уровень: <b>{compat['level']}</b> — {compat['score']}%\n\n"
        f"<b>Ключевые аспекты:</b>\n{aspects_text}\n\n"
        f"<i>Карта {name}: {p_chart.get('sun_sign', '?')} ☉ / {p_chart.get('moon_sign', '?')} ☽</i>"
    )

    # Запрашиваем развёрнутое толкование у AI
    groq_prompt = f"""Ты астролог. Дай подробный анализ совместимости пары.

Я:
Солнце: {my_chart.get('sun_sign')}
Луна: {my_chart.get('moon_sign')}

{name}:
Солнце: {p_chart.get('sun_sign')}
Луна: {p_chart.get('moon_sign')}

Общий балл совместимости: {compat['score']}%
Уровень: {compat['level']}

Дай:
1. Сильные стороны союза
2. Возможные трудности
3. Практический совет для пары

Тон: честный, но позитивный."""

    interpretation = await ask_groq(groq_prompt, max_tokens=700)
    if interpretation:
        msg_text += f"\n\n{interpretation}"

    await msg.edit_text(msg_text, parse_mode=ParseMode.HTML, reply_markup=main_menu())


# ─── Текущее местоположение ───────────────────────────────────────────────────

@dp.message(F.text == "📍 Мой регион")
@dp.message(Command("location"))
async def update_location(message: types.Message, state: FSMContext):
    await state.set_state(LocationUpdate.city)
    await message.answer(
        "Введи свой текущий город (может отличаться от города рождения).\n"
        "Прогнозы будут учитывать твоё местоположение.\n\n"
        "Например: <code>Москва</code> или <code>Berlin</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_menu()
    )


@dp.message(LocationUpdate.city)
async def save_location(message: types.Message, state: FSMContext):
    city = message.text.strip()
    msg = await message.answer("🔍 Определяю местоположение...")

    geo = await geocode_city(city)
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
        f"✅ Текущее местоположение обновлено:\n"
        f"📍 {geo['display_name']}\n"
        f"🕐 Часовой пояс: {geo['timezone']}\n\n"
        f"Прогнозы теперь будут учитывать твой регион.",
        reply_markup=main_menu()
    )


# ─── Настройки, история, помощь ───────────────────────────────────────────────

@dp.message(F.text == "⚙️ Настройки")
async def show_settings(message: types.Message):
    user = db.get_user(message.from_user.id)
    if user:
        chart = user.get('chart', {})
        current = user.get('current_city')
        loc_line = f"\n📍 Текущий регион: {current}" if current else ""
        await message.answer(
            f"⚙️ <b>Настройки</b>\n\n"
            f"📅 Дата: {user.get('birth_date', '?')}\n"
            f"🕐 Время: {user.get('birth_time', 'не указано')}\n"
            f"🌍 Город рождения: {user.get('city', '?')}\n"
            f"🕰 Timezone: {user.get('timezone', '?')}{loc_line}\n"
            f"☉ Знак: {chart.get('sun_sign', '?')}",
            parse_mode=ParseMode.HTML,
            reply_markup=settings_menu()
        )
    else:
        await message.answer(
            "Данные ещё не введены. Нажми '✏️ Изменить данные'.",
            reply_markup=settings_menu()
        )


@dp.message(F.text == "📜 История")
async def show_history(message: types.Message):
    readings = db.get_readings(message.from_user.id, limit=5)
    if not readings:
        await message.answer("📜 История пуста — запроси первый прогноз!")
        return
    lines = ["📜 <b>Последние прогнозы:</b>\n"]
    for r in readings:
        date = r.get('created_at', '?')[:10]
        rtype = r.get('reading_type', '?')
        lines.append(f"• {date} — {rtype}")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@dp.message(F.text == "🔙 Назад")
async def back_to_main(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu())


@dp.message(F.text == "❓ Помощь")
@dp.message(Command("help"))
async def show_help(message: types.Message):
    await message.answer(
        "🔮 <b>Команды бота:</b>\n\n"
        "<b>🔮 Моя карта</b> — натальная карта и толкование\n"
        "<b>📅 Прогноз</b> — прогноз на сегодня\n"
        "<b>💑 Совместимость</b> — совместимость с партнёром\n"
        "<b>📍 Мой регион</b> — обновить текущее местоположение\n"
        "<b>⚙️ Настройки</b> — изменить данные рождения\n"
        "<b>📜 История</b> — последние прогнозы\n\n"
        "Данные сохраняются для персональных расчётов.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu()
    )


# ─── Запуск ───────────────────────────────────────────────────────────────────

async def main():
    logger.info("Бот запускается...")
    try:
        await dp.start_polling(bot)
    finally:
        await http_client.aclose()
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    asyncio.run(main())
