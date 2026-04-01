"""
Клавиатуры бота.
"""

from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton,
)


# ── Reply-клавиатуры ──────────────────────────────────────────────────────────

def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔮 Моя карта"),     KeyboardButton(text="📅 Прогноз")],
            [KeyboardButton(text="👥 Люди"),           KeyboardButton(text="📍 Мой регион")],
            [KeyboardButton(text="⚙️ Настройки"),     KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True
    )


def settings_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✏️ Изменить данные")],
            [KeyboardButton(text="📜 История"), KeyboardButton(text="🔙 Назад")],
        ],
        resize_keyboard=True
    )


def cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True
    )


def remove() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


# ── Инлайн: выбор периода прогноза ───────────────────────────────────────────

def forecast_period_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📆 Сегодня",     callback_data="forecast:today"),
            InlineKeyboardButton(text="📅 Завтра",      callback_data="forecast:tomorrow"),
        ],
        [
            InlineKeyboardButton(text="🗓 Эта неделя",  callback_data="forecast:week"),
            InlineKeyboardButton(text="📅 Этот месяц",  callback_data="forecast:month"),
        ],
        [
            InlineKeyboardButton(text="✏️ Своя дата",  callback_data="forecast:custom"),
        ],
    ])


# ── Инлайн: список людей ──────────────────────────────────────────────────

def partners_list_kb(partners: list) -> InlineKeyboardMarkup:
    """Кнопка на каждого человека + добавить нового."""
    rows = []
    for p in partners:
        rows.append([InlineKeyboardButton(
            text=f"👤 {p['name']}  ({p.get('birth_date', '?')})",
            callback_data=f"partner:view:{p['id']}"
        )])
    rows.append([InlineKeyboardButton(
        text="➕ Добавить человека",
        callback_data="partner:add"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def no_partners_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➕ Добавить человека", callback_data="partner:add")
    ]])


# ── Инлайн: меню конкретного человека ────────────────────────────────────────

def partner_actions_kb(partner_id: int) -> InlineKeyboardMarkup:
    """Что можно сделать с выбранным человеком."""
    pid = str(partner_id)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💑 Совместимость",      callback_data=f"partner:compat:{pid}")],
        [InlineKeyboardButton(text="🔮 Натальная карта",    callback_data=f"partner:natal:{pid}")],
        [InlineKeyboardButton(text="📅 Прогноз для него",   callback_data=f"partner:forecast:{pid}")],
        [InlineKeyboardButton(text="🗑 Удалить профиль",   callback_data=f"partner:delete:{pid}")],
        [InlineKeyboardButton(text="◀️ Назад к списку",     callback_data="partner:list")],
    ])


def confirm_delete_kb(partner_id: int) -> InlineKeyboardMarkup:
    pid = str(partner_id)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"partner:confirm_delete:{pid}"),
            InlineKeyboardButton(text="❌ Отмена",       callback_data=f"partner:view:{pid}"),
        ]
    ])