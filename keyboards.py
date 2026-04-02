"""
Клавиатуры для Telegram-бота.
Обновлено: «Астропрофиль» вместо «Партнёры», кнопка «Расклад Таро».
"""

from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔮 Моя карта"), KeyboardButton(text="📅 Прогноз")],
            [KeyboardButton(text="🪐 Астропрофиль"), KeyboardButton(text="📍 Мой регион")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True
    )


def settings_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✏️ Изменить данные")],
            [KeyboardButton(text="🔙 Назад")],
        ],
        resize_keyboard=True
    )


def cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True
    )


def forecast_period_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Сегодня", callback_data="forecast:today"),
            InlineKeyboardButton(text="Завтра", callback_data="forecast:tomorrow"),
        ],
        [
            InlineKeyboardButton(text="Неделя", callback_data="forecast:week"),
            InlineKeyboardButton(text="Месяц", callback_data="forecast:month"),
        ],
        [InlineKeyboardButton(text="📅 Выбрать дату", callback_data="forecast:custom")],
    ])


# ── Астропрофиль ──────────────────────────────────────────────────────────────

def profile_select_kb(partners: list) -> InlineKeyboardMarkup:
    """Клавиатура выбора профиля: свой + список людей + добавить."""
    buttons = [
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile:self")],
    ]
    for p in partners:
        buttons.append([
            InlineKeyboardButton(
                text=f"👤 {p.get('name', '?')}",
                callback_data=f"profile:person:{p['id']}"
            )
        ])
    buttons.append([
        InlineKeyboardButton(text="➕ Добавить человека", callback_data="partner:add")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ── Партнёры (для обратной совместимости) ─────────────────────────────────────

def partners_list_kb(partners: list) -> InlineKeyboardMarkup:
    buttons = []
    for p in partners:
        buttons.append([
            InlineKeyboardButton(
                text=f"👤 {p.get('name', '?')}",
                callback_data=f"partner:view:{p['id']}"
            )
        ])
    buttons.append([
        InlineKeyboardButton(text="➕ Добавить", callback_data="partner:add")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def no_partners_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить человека", callback_data="partner:add")]
    ])


def partner_actions_kb(partner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💑 Совместимость", callback_data=f"partner:compat:{partner_id}"),
            InlineKeyboardButton(text="🔮 Карта", callback_data=f"partner:natal:{partner_id}"),
        ],
        [
            InlineKeyboardButton(text="📅 Прогноз", callback_data=f"partner:forecast:{partner_id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"partner:delete:{partner_id}"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="partner:list")],
    ])


def confirm_delete_kb(partner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"partner:confirm_delete:{partner_id}"),
            InlineKeyboardButton(text="❌ Нет", callback_data=f"partner:view:{partner_id}"),
        ]
    ])


# ── Таро ──────────────────────────────────────────────────────────────────────

def tarot_button() -> InlineKeyboardMarkup:
    """Кнопка «Расклад Таро» под сообщением бота."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🃏 Расклад Таро", callback_data="tarot:spread")]
    ])
