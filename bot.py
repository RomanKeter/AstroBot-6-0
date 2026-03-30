"""
Telegram бот астролог с кнопками и валидацией.
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

from astro_engine import calculate_natal_chart, format_chart_text, get_transits
from database import AstroDatabase
from keyboards import main_menu, settings_menu, cancel_menu

# Загрузка .env
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Проверка обязательных переменных
TG_TOKEN = os.getenv("TG_BOT_TOKEN")
GROQ_KEY = os.getenv("GROQ_API_KEY")

if not TG_TOKEN:
    logger.critical("TG_BOT_TOKEN не задан! Установи переменную окружения.")
    sys.exit(1)

if not GROQ_KEY:
    logger.warning("GROQ_API_KEY не задан — AI-функции будут недоступны.")

# Инициализация
bot = Bot(token=TG_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

http_client = httpx.AsyncClient()
groq_client = None
if GROQ_KEY:
    groq_client = AsyncGroq(api_key=GROQ_KEY, http_client=http_client)

db = AstroDatabase()


# FSM состояния
class Registration(StatesGroup):
    date = State()
    time = State()
    city = State()


# ============ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ============

def validate_date(text: str) -> tuple:
    """Проверить формат даты ДД.ММ.ГГГГ."""
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
    """Проверить формат времени ЧЧ:ММ или 'не знаю'."""
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
    """Безопасный запрос к Groq."""
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


# ============ ОБРАБОТЧИКИ КОМАНД ============

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Приветствие."""
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
            "Я помогу разобраться в звёздах. "
            "Нажми '✏️ Изменить данные' чтобы начать.",
            reply_markup=settings_menu()
        )


# ============ РЕГИСТРАЦИЯ / ИЗМЕНЕНИЕ ДАННЫХ ============

@dp.message(F.text == "❌ Отмена")
async def cancel_registration(message: types.Message, state: FSMContext):
    """Отмена — возврат в главное меню."""
    await state.clear()
    user = db.get_user(message.from_user.id)
    if user:
        await message.answer("Главное меню:", reply_markup=main_menu())
    else:
        await message.answer("Главное меню:", reply_markup=settings_menu())


@dp.message(F.text == "✏️ Изменить данные")
async def start_registration(message: types.Message, state: FSMContext):
    """Начать ввод данных."""
    await state.set_state(Registration.date)
    await message.answer(
        "Введи дату рождения:\n<code>ДД.ММ.ГГГГ</code>\n\n"
        "Пример: <code>15.05.1990</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_menu()
    )


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
        parse_mode=ParseMode.HTML
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
        "В каком городе ты родился?\n"
        "(Например: Москва, Санкт-Петербург, Минск)",
        reply_markup=cancel_menu()
    )


@dp.message(Registration.city)
async def process_city(message: types.Message, state: FSMContext):
    """Сохранение и расчёт карты."""
    city = message.text.strip()

    if len(city) < 2:
        await message.answer("❌ Название города слишком короткое. Попробуй снова:")
        return

    data = await state.get_data()
    day, month, year = map(int, data['date'].split('.'))
    hour, minute = map(int, data['time'].split(':'))

    # Координаты городов (расширенный список)
    city_coords = {
        'москва': (55.7558, 37.6173),
        'минск': (53.9045, 27.5615),
        'питер': (59.9311, 30.3609),
        'санкт-петербург': (59.9311, 30.3609),
        'киев': (50.4501, 30.5234),
        'новосибирск': (55.0084, 82.9357),
        'екатеринбург': (56.8389, 60.6057),
        'казань': (55.7887, 49.1221),
        'нижний новгород': (56.2965, 43.9361),
        'челябинск': (55.1644, 61.4368),
        'самара': (53.1959, 50.1002),
        'ростов-на-дону': (47.2357, 39.7015),
        'уфа': (54.7388, 55.9721),
        'красноярск': (56.0153, 92.8932),
        'пермь': (58.0105, 56.2502),
        'воронеж': (51.6720, 39.1843),
        'волгоград': (48.7080, 44.5133),
        'краснодар': (45.0355, 38.9753),
        'саратов': (51.5336, 46.0343),
        'тюмень': (57.1522, 65.5272),
        'тольятти': (53.5303, 49.3461),
        'ижевск': (56.8527, 53.2114),
        'барнаул': (53.3548, 83.7698),
        'иркутск': (52.2970, 104.2964),
        'хабаровск': (48.4827, 135.0837),
        'владивосток': (43.1332, 131.9113),
        'ярославль': (57.6261, 39.8845),
        'махачкала': (42.9849, 47.5047),
        'томск': (56.4846, 84.9476),
        'оренбург': (51.7682, 55.0968),
        'кемерово': (55.3500, 86.0883),
        'астана': (51.1694, 71.4491),
        'алматы': (43.2220, 76.8512),
        'гомель': (52.4345, 30.9754),
        'брест': (52.0975, 23.7341),
        'витебск': (55.1904, 30.2049),
        'гродно': (53.6688, 23.8313),
        'могилёв': (53.9045, 30.3449),
        'одесса': (46.4825, 30.7233),
        'харьков': (49.9935, 36.2304),
        'днепр': (48.4647, 35.0462),
        'львов': (49.8397, 24.0297),
        'тбилиси': (41.7151, 44.8271),
        'баку': (40.4093, 49.8671),
        'ереван': (40.1792, 44.4991),
        'ташкент': (41.2995, 69.2401),
        'бишкек': (42.8746, 74.5698),
    }

    lat, lon = city_coords.get(city.lower(), (53.9, 27.56))

    chart = calculate_natal_chart(year, month, day, hour, minute, lat, lon, city)

    db.save_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        birth_date=f"{year}-{month:02d}-{day:02d}",
        birth_time=data['time'] if data['time'] != '12:00' else None,
        city=city,
        lat=lat,
        lon=lon,
        chart=chart
    )

    await state.clear()

    chart_text = format_chart_text(chart, {'city': city, 'lat': lat, 'lon': lon})
    await message.answer(
        f"✨ <b>Твоя натальная карта</b>\n\n"
        f"<pre>{chart_text}</pre>\n\n"
        f"Используй кнопки ниже для прогнозов!",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu()
    )


# ============ ОСНОВНЫЕ ФУНКЦИИ ============

@dp.message(F.text == "🔮 Моя карта")
@dp.message(Command("natal"))
async def show_natal(message: types.Message):
    """Показать натальную карту."""
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
            f"<pre>{chart_text}</pre>\n\n"
            f"{interpretation}",
            parse_mode=ParseMode.HTML
        )
    else:
        await msg.edit_text(
            f"<b>🔮 Твоя карта</b>\n\n<pre>{chart_text}</pre>",
            parse_mode=ParseMode.HTML
        )


@dp.message(F.text == "📅 Прогноз")
@dp.message(Command("forecast"))
async def show_forecast(message: types.Message):
    """Прогноз на сегодня."""
    user = db.get_user(message.from_user.id)

    if not user:
        await message.answer("Сначала введи данные:", reply_markup=settings_menu())
        return

    chart = user.get('chart', {})
    msg = await message.answer("🔮 Анализирую транзиты...")

    transits = get_transits(chart)
    transit_text = ""
    if transits:
        transit_lines = [f"• {t['natal_planet']} в {t['aspect']} к {t['transit_planet']}" for t in transits[:3]]
        transit_text = "\n".join(transit_lines)

    prompt = f"""Ты астролог. Сделай прогноз на сегодня.

Натальная карта:
Солнце: {chart.get('sun_sign', '?')}
Луна: {chart.get('moon_sign', '?')}
Асцендент: {chart.get('ascendant', '?')}

Активные транзиты:
{transit_text or 'Нет точных аспектов'}

Структура:
🌅 Утро
☀️ День
🌙 Вечер
⭐ Главный совет

Тон: конкретный, практичный."""

    result = await ask_groq(prompt)
    if result:
        await msg.edit_text(result, parse_mode=ParseMode.HTML)
    else:
        await msg.edit_text("⚠️ Не удалось получить прогноз. Попробуй позже.")


@dp.message(F.text == "⚙️ Настройки")
async def show_settings(message: types.Message):
    """Меню настроек."""
    user = db.get_user(message.from_user.id)
    if user:
        chart = user.get('chart', {})
        await message.answer(
            f"⚙️ <b>Настройки</b>\n\n"
            f"📅 Дата: {user.get('birth_date', '?')}\n"
            f"🕐 Время: {user.get('birth_time', 'не указано')}\n"
            f"📍 Город: {user.get('city', '?')}\n"
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
    """Показать историю прогнозов."""
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
    """Вернуться в главное меню."""
    await state.clear()
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
        "<b>📜 История</b> — последние прогнозы\n\n"
        "Данные сохраняются для персональных расчётов.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu()
    )


async def main():
    logger.info("Бот запускается...")
    try:
        await dp.start_polling(bot)
    finally:
        await http_client.aclose()
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    asyncio.run(main())
