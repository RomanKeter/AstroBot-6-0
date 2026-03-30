"""
Telegram бот астролог с кнопками и валидацией.
"""

import os
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

from astro_engine import calculate_natal_chart, format_chart_text, get_transits
from database import AstroDatabase
from geocoder import geocode_city
from keyboards import main_menu, settings_menu, cancel_menu

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Проверка обязательных переменных
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not TG_BOT_TOKEN:
    raise RuntimeError("TG_BOT_TOKEN не задан в .env")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY не задан в .env")

# Инициализация
bot = Bot(token=TG_BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

http_client = httpx.AsyncClient()
groq_client = AsyncGroq(api_key=GROQ_API_KEY, http_client=http_client)

db = AstroDatabase()

GROQ_MODEL = "llama-3.3-70b-versatile"


# ==================== FSM состояния ====================

class Registration(StatesGroup):
    date = State()
    time = State()
    city = State()


class Settings(StatesGroup):
    menu = State()


# ==================== Валидация ====================

def validate_date(text: str) -> tuple[bool, str]:
    """Проверить формат даты ДД.ММ.ГГГГ."""
    match = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", text.strip())
    if not match:
        return False, "Формат: ДД.ММ.ГГГГ (пример: 15.05.1990)"

    day, month, year = map(int, match.groups())
    try:
        datetime(year, month, day)
    except ValueError:
        return False, "Неверная дата (проверь день и месяц)"

    if not 1900 <= year <= 2026:
        return False, "Год должен быть между 1900 и 2026"

    return True, ""


def validate_time(text: str) -> tuple[bool, str, str]:
    """Проверить формат времени ЧЧ:ММ или 'не знаю'."""
    text = text.strip().lower()

    if text in ("не знаю", "нет", "-", "неизвестно"):
        return True, "", "12:00"

    match = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if not match:
        return False, "Формат: ЧЧ:ММ (пример: 14:30) или 'не знаю'", ""

    hour, minute = map(int, match.groups())
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return False, "Время должно быть от 00:00 до 23:59", ""

    return True, "", f"{hour:02d}:{minute:02d}"


# ==================== Вспомогательные ====================

async def _ask_groq(prompt: str, max_tokens: int = 1000) -> str | None:
    """Безопасный вызов Groq API."""
    try:
        response = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Groq API error: {e}")
        return None


def _get_chart_summary(chart: dict) -> str:
    """Безопасно получить краткую сводку карты."""
    sun = chart.get("sun_sign") or "?"
    moon = chart.get("moon_sign") or "неизвестно"
    asc = chart.get("ascendant") or "неизвестно"
    return f"Солнце: {sun}\nЛуна: {moon}\nАсцендент: {asc}"


# ==================== /start ====================

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Приветствие."""
    await state.clear()
    user = db.get_user(message.from_user.id)

    if user and user.get("chart"):
        chart = user["chart"]
        name = user.get("first_name") or "друг"
        sun_sign = chart.get("sun_sign", "?")
        await message.answer(
            f"✨ С возвращением, {name}!\n"
            f"Твой знак: {sun_sign}",
            reply_markup=main_menu(),
        )
    else:
        await message.answer(
            "🔮 Добро пожаловать в Астро-Бот!\n\n"
            "Я помогу разобраться в звёздах.\n"
            "Для начала нужно ввести данные рождения.",
            reply_markup=settings_menu(),
        )


# ==================== Регистрация / Изменение данных ====================

@dp.message(F.text == "✏️ Изменить данные")
async def start_registration(message: types.Message, state: FSMContext):
    """Начать ввод данных."""
    await state.clear()
    await state.set_state(Registration.date)

    await message.answer(
        "Введи дату рождения:\n<code>ДД.ММ.ГГГГ</code>\n\n"
        "Пример: <code>15.05.1990</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_menu(),
    )


@dp.message(F.text == "❌ Отмена")
async def cancel_registration(message: types.Message, state: FSMContext):
    """Отмена ввода данных — возврат в главное меню."""
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu())


@dp.message(Registration.date)
async def process_date(message: types.Message, state: FSMContext):
    """Обработка даты."""
    is_valid, error_msg = validate_date(message.text)
    if not is_valid:
        await message.answer(f"❌ {error_msg}\n\nПопробуй снова:")
        return

    await state.update_data(date=message.text.strip())
    await state.set_state(Registration.time)

    await message.answer(
        "Теперь время рождения:\n<code>ЧЧ:ММ</code>\n\n"
        "Если не знаешь — напиши <code>не знаю</code>",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Registration.time)
async def process_time(message: types.Message, state: FSMContext):
    """Обработка времени."""
    is_valid, error_msg, normalized_time = validate_time(message.text)
    if not is_valid:
        await message.answer(f"❌ {error_msg}\n\nПопробуй снова:")
        return

    await state.update_data(time=normalized_time)
    await state.set_state(Registration.city)

    await message.answer(
        "В каком городе ты родился(ась)?\n"
        "(Например: Москва, Санкт-Петербург, Минск)",
        reply_markup=cancel_menu(),
    )


@dp.message(Registration.city)
async def process_city(message: types.Message, state: FSMContext):
    """Геокодинг города, расчёт карты, сохранение."""
    city = message.text.strip()
    if len(city) < 2:
        await message.answer("❌ Название города слишком короткое. Попробуй снова:")
        return

    data = await state.get_data()
    day, month, year = map(int, data["date"].split("."))
    hour, minute = map(int, data["time"].split(":"))

    # Геокодинг через geopy
    geo = await geocode_city(city)
    if geo is None:
        await message.answer(
            "❌ Не удалось найти город. Проверь написание и попробуй снова:"
        )
        return

    lat, lon, resolved_city = geo

    # Расчёт карты
    chart = calculate_natal_chart(year, month, day, hour, minute, lat, lon, city=resolved_city)

    # Сохранение
    db.save_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        birth_date=f"{year}-{month:02d}-{day:02d}",
        birth_time=data["time"] if data["time"] != "12:00" else None,
        city=resolved_city,
        lat=lat,
        lon=lon,
        chart=chart,
    )

    await state.clear()

    chart_text = format_chart_text(chart, {"city": resolved_city, "lat": lat, "lon": lon})

    await message.answer(
        f"✨ <b>Твоя натальная карта</b>\n\n"
        f"<pre>{chart_text}</pre>\n\n"
        f"Используй кнопки ниже для прогнозов!",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
    )


# ==================== Основные функции ====================

@dp.message(F.text == "🔮 Моя карта")
@dp.message(Command("natal"))
async def show_natal(message: types.Message):
    """Показать натальную карту с толкованием."""
    user = db.get_user(message.from_user.id)
    if not user or not user.get("chart"):
        await message.answer("Сначала введи данные рождения:", reply_markup=settings_menu())
        return

    chart = user["chart"]
    chart_text = format_chart_text(chart, user)

    msg = await message.answer("🔮 Генерирую толкование...")

    summary = _get_chart_summary(chart)
    prompt = (
        f"Ты астролог. Дай краткий разбор натальной карты:\n\n"
        f"{summary}\n\n"
        f"Структура:\n"
        f"1. Суть личности (Солнце)\n"
        f"2. Эмоции (Луна)\n"
        f"3. Внешность/поведение (Асцендент)\n"
        f"4. 3 сильные качества\n"
        f"5. Совет\n\n"
        f"Тон: вдохновляющий."
    )

    interpretation = await _ask_groq(prompt)

    if interpretation:
        await msg.edit_text(
            f"<b>🔮 Твоя натальная карта</b>\n\n"
            f"<pre>{chart_text}</pre>\n\n"
            f"{interpretation}",
            parse_mode=ParseMode.HTML,
        )
        db.save_reading(message.from_user.id, "natal", interpretation)
    else:
        await msg.edit_text(
            f"<b>🔮 Твоя карта</b>\n\n<pre>{chart_text}</pre>",
            parse_mode=ParseMode.HTML,
        )


@dp.message(F.text == "📅 Прогноз")
@dp.message(Command("forecast"))
async def show_forecast(message: types.Message):
    """Прогноз на сегодня."""
    user = db.get_user(message.from_user.id)
    if not user or not user.get("chart"):
        await message.answer("Сначала введи данные:", reply_markup=settings_menu())
        return

    chart = user["chart"]

    msg = await message.answer("🔮 Анализирую транзиты...")

    transits = get_transits(chart)
    transit_text = ""
    if transits:
        lines = [f"• {t['natal_planet']} в {t['aspect']} к {t['transit_planet']}" for t in transits[:3]]
        transit_text = "\n".join(lines)

    summary = _get_chart_summary(chart)
    prompt = (
        f"Ты астролог. Сделай прогноз на сегодня ({datetime.now().strftime('%d.%m.%Y')}).\n\n"
        f"Натальная карта:\n{summary}\n\n"
        f"Активные транзиты:\n{transit_text or 'Нет точных аспектов'}\n\n"
        f"Структура:\n🌅 Утро\n☀️ День\n🌙 Вечер\n⭐ Главный совет\n\n"
        f"Тон: конкретный, практичный."
    )

    forecast = await _ask_groq(prompt)

    if forecast:
        await msg.edit_text(forecast)
        db.save_reading(message.from_user.id, "forecast", forecast)
    else:
        await msg.edit_text("⚠️ Не удалось получить прогноз. Попробуй позже.")


# ==================== Настройки и навигация ====================

@dp.message(F.text == "⚙️ Настройки")
async def show_settings(message: types.Message):
    """Открыть меню настроек."""
    await message.answer("⚙️ Настройки:", reply_markup=settings_menu())


@dp.message(F.text == "📜 История")
async def show_history(message: types.Message):
    """Показать последние прогнозы."""
    readings = db.get_readings(message.from_user.id, limit=5)
    if not readings:
        await message.answer("📜 История пуста. Запроси прогноз или карту!")
        return

    lines = []
    for r in readings:
        date = r.get("created_at", "")[:16]
        rtype = "🔮 Карта" if r["reading_type"] == "natal" else "📅 Прогноз"
        preview = (r.get("content") or "")[:80] + "..."
        lines.append(f"<b>{rtype}</b> ({date})\n{preview}\n")

    await message.answer(
        "📜 <b>Последние запросы:</b>\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


@dp.message(F.text == "🔙 Назад")
async def back_to_main(message: types.Message):
    """Вернуться в главное меню."""
    await message.answer("Главное меню:", reply_markup=main_menu())


@dp.message(F.text == "❓ Помощь")
@dp.message(Command("help"))
async def show_help(message: types.Message):
    """Помощь."""
    await message.answer(
        "🔮 <b>Команды бота:</b>\n\n"
        "<b>🔮 Моя карта</b> — натальная карта и толкование\n"
        "<b>📅 Прогноз</b> — прогноз на сегодня\n"
        "<b>⚙️ Настройки</b> — изменить данные рождения\n"
        "<b>📜 История</b> — последние запросы\n\n"
        "Данные сохраняются для персональных расчётов.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
    )


# ==================== Запуск ====================

async def main():
    logger.info("Бот запускается...")
    try:
        await dp.start_polling(bot)
    finally:
        await http_client.aclose()
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    asyncio.run(main())
