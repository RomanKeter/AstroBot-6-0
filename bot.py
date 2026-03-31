"""
Telegram бот астролог.

Исправления в этой версии:
  1. Зависание геокодинга: TimezoneFinder инициализируется один раз при старте,
     а не при каждом запросе (было: ~3 сек загрузка файла при каждом запросе).
  2. Прогноз: выбор периода через инлайн-кнопки (сегодня/завтра/неделя/месяц/своя дата).
  3. Натальная карта: полная западная карта с 12 домами + AI-интерпретация каждого дома.
  4. Партнёры: раздел «👥 Партнёры» с выбором из базы, у каждого партнёра своё меню
     (совместимость / натальная карта / прогноз / удалить).
"""

import os
import sys
import asyncio
import logging
import re
from datetime import datetime, timedelta

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

# ── TimezoneFinder: инициализируется ОДИН РАЗ при старте ─────────────────────
# Решение зависания: при первом вызове tf = TimezoneFinder() идёт загрузка
# ~100 МБ файла эфемерид — это занимает 2–4 секунды. Если создавать объект
# при каждом запросе города, бот «зависает» в момент геокодинга.
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
    """
    Координаты + timezone через Nominatim (бесплатно, без ключа).
    TimezoneFinder используется из уже загруженного singleton — без зависания.
    """
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

        # Timezone — используем уже инициализированный объект (без зависания)
        tz = "UTC"
        tf = get_tf()
        if tf:
            tz = tf.timezone_at(lat=lat, lng=lon) or "UTC"
        else:
            # Запасной вариант если timezonefinder не установлен
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


async def send_long(message: types.Message, text: str, **kwargs):
    """Отправить длинный текст, разбив на части по 4000 символов."""
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i + 4000], **kwargs)


# ── /start ────────────────────────────────────────────────────────────────────

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

    chart_text = format_chart_text(chart, {'city': geo["display_name"]})
    await msg.edit_text(
        f"✨ <b>Данные сохранены!</b>\n\n<pre>{chart_text}</pre>\n\n"
        f"🕐 Часовой пояс: {geo['timezone']}",
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

    # Шаг 1: технические данные карты
    full_text = format_full_chart_text(chart)
    await msg.edit_text(
        f"<b>🔮 Натальная карта</b>\n\n{full_text}",
        parse_mode=ParseMode.HTML
    )

    # Шаг 2: AI-интерпретация 12 домов
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
    """Общая функция генерации прогноза (для себя и для партнёра)."""
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

    prompt = (
        f"Ты астролог. Составь прогноз на {date_label}.\n\n"
        f"Натальная карта:\n"
        f"Солнце: {chart.get('sun_sign', '?')}\n"
        f"Луна: {chart.get('moon_sign', '?')}\n"
        f"Асцендент: {chart.get('ascendant', '?')}\n\n"
        f"Текущее местоположение: {current_city} (часовой пояс: {current_tz})\n\n"
        f"Структура прогноза:\n{structure}\n\n"
        f"Тон: конкретный, практичный. Учитывай местоположение."
    )

    result = await ask_groq(prompt, max_tokens=1500)
    if result:
        await msg.edit_text(result, parse_mode=ParseMode.HTML)
    else:
        await msg.edit_text("⚠️ Не удалось получить прогноз. Попробуй позже.")


# ── Партнёры ──────────────────────────────────────────────────────────────────

@dp.message(F.text == "👥 Партнёры")
async def partners_menu(message: types.Message):
    user = db.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала введи свои данные рождения:", reply_markup=settings_menu())
        return

    partners = db.get_partners(message.from_user.id)
    if partners:
        await message.answer(
            f"👥 <b>Твои партнёры</b> — {len(partners)} чел.\n\nВыбери кого-нибудь:",
            parse_mode=ParseMode.HTML,
            reply_markup=partners_list_kb(partners)
        )
    else:
        await message.answer(
            "👥 У тебя пока нет сохранённых партнёров.",
            reply_markup=no_partners_kb()
        )


# Просмотр конкретного партнёра

@dp.callback_query(F.data.startswith("partner:view:"))
async def partner_view(callback: types.CallbackQuery):
    pid      = int(callback.data.split(":")[2])
    partners = db.get_partners(callback.from_user.id)
    p        = next((x for x in partners if x['id'] == pid), None)
    await callback.answer()
    if not p:
        await callback.message.answer("❌ Партнёр не найден.")
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


# Вернуться к списку

@dp.callback_query(F.data == "partner:list")
async def partner_back_list(callback: types.CallbackQuery):
    await callback.answer()
    partners = db.get_partners(callback.from_user.id)
    if partners:
        await callback.message.edit_text(
            f"👥 <b>Твои партнёры</b> — {len(partners)} чел.\n\nВыбери кого-нибудь:",
            parse_mode=ParseMode.HTML,
            reply_markup=partners_list_kb(partners)
        )
    else:
        await callback.message.edit_text(
            "👥 Список партнёров пуст.",
            reply_markup=no_partners_kb()
        )


# Добавить нового партнёра

@dp.callback_query(F.data == "partner:add")
async def partner_add_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(PartnerAdd.name)
    await callback.message.answer("Как зовут партнёра? (имя или прозвище)",
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
    await message.answer("Город рождения партнёра:", reply_markup=cancel_menu())


@dp.message(PartnerAdd.city)
async def partner_add_city(message: types.Message, state: FSMContext):
    city = message.text.strip()
    msg  = await message.answer("🔍 Ищу город...")

    geo = await geocode_city(city)
    if not geo:
        await msg.edit_text(f"❌ Город «{city}» не найден. Попробуй снова:")
        return

    data = await state.get_data()
    day, month, year = map(int, data['partner_date'].split('.'))
    hour, minute     = map(int, data['partner_time'].split(':'))
    name             = data['partner_name']

    p_chart = calculate_natal_chart(year, month, day, hour, minute,
                                    geo["lat"], geo["lon"], city, geo["timezone"])

    pid = db.save_partner(
        user_id=message.from_user.id, name=name,
        birth_date=f"{year}-{month:02d}-{day:02d}",
        birth_time=data['partner_time'] if data['partner_time'] != '12:00' else None,
        city=geo["display_name"], lat=geo["lat"], lon=geo["lon"],
        chart=p_chart, timezone=geo["timezone"]
    )
    await state.clear()

    await msg.edit_text(
        f"✅ <b>{name}</b> сохранён!\n\n"
        f"☉ {p_chart.get('sun_sign', '?')} · ☽ {p_chart.get('moon_sign', '?')} · ↑ {p_chart.get('ascendant', '?')}",
        parse_mode=ParseMode.HTML,
        reply_markup=partner_actions_kb(pid)
    )


# Совместимость с партнёром

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
    msg = await callback.message.answer(text, parse_mode=ParseMode.HTML)

    prompt = build_compatibility_prompt(my_chart, p_chart, "Я", name, compat)
    interp = await ask_groq(prompt, max_tokens=1000)
    if interp:
        await send_long(callback.message, interp, parse_mode=ParseMode.HTML)


# Натальная карта партнёра

@dp.callback_query(F.data.startswith("partner:natal:"))
async def partner_natal(callback: types.CallbackQuery):
    pid      = int(callback.data.split(":")[2])
    await callback.answer()

    partners = db.get_partners(callback.from_user.id)
    p        = next((x for x in partners if x['id'] == pid), None)
    if not p:
        await callback.message.answer("❌ Партнёр не найден.")
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


# Прогноз для партнёра

@dp.callback_query(F.data.startswith("partner:forecast:"))
async def partner_forecast(callback: types.CallbackQuery):
    pid = int(callback.data.split(":")[2])
    await callback.answer()

    partners = db.get_partners(callback.from_user.id)
    p        = next((x for x in partners if x['id'] == pid), None)
    if not p:
        await callback.message.answer("❌ Партнёр не найден.")
        return

    chart = p.get('chart', {})
    name  = p.get('name', '?')
    city  = p.get('city', '?')

    # Прогноз сразу на сегодня
    await _do_forecast(
        callback.message,
        callback.from_user.id,
        "today",
        chart_override=chart,
        location_override=city
    )


# Удалить партнёра

@dp.callback_query(F.data.startswith("partner:delete:"))
async def partner_delete_confirm(callback: types.CallbackQuery):
    pid = int(callback.data.split(":")[2])
    await callback.answer()
    partners = db.get_partners(callback.from_user.id)
    p = next((x for x in partners if x['id'] == pid), None)
    name = p.get('name', '?') if p else '?'
    await callback.message.edit_text(
        f"Удалить <b>{name}</b> из списка партнёров?",
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
            f"✅ Удалено.\n\n👥 <b>Партнёры</b>:",
            parse_mode=ParseMode.HTML,
            reply_markup=partners_list_kb(partners)
        )
    else:
        await callback.message.edit_text(
            "✅ Удалено. Список партнёров пуст.",
            reply_markup=no_partners_kb()
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
        "<b>👥 Партнёры</b> — список партнёров: совместимость, карта, прогноз\n"
        "<b>📍 Мой регион</b> — текущее местоположение для прогнозов\n"
        "<b>⚙️ Настройки</b> — изменить данные рождения\n"
        "<b>📜 История</b> — последние прогнозы",
        parse_mode=ParseMode.HTML, reply_markup=main_menu()
    )


# ── Запуск ────────────────────────────────────────────────────────────────────

async def main():
    # Прогрев TimezoneFinder ДО начала обработки запросов.
    # Это гарантирует, что первый запрос города не будет зависать.
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
