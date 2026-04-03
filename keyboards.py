"""
Клавиатуры для Астро-Бота.
Обновлено: Партнёры → Астропрофили с кнопкой «Я».
"""

from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔮 Моя карта"), KeyboardButton(text="📅 Прогноз")],
            [KeyboardButton(text="🌟 Астропрофиль"), KeyboardButton(text="📍 Мой регион")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True,
    )


def settings_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✏️ Изменить данные")],
            [KeyboardButton(text="🔙 Назад")],
        ],
        resize_keyboard=True,
    )


def cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
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
        [InlineKeyboardButton(text="Выбрать дату", callback_data="forecast:custom")],
    ])


def astroprofile_list_kb(user_name: str, partners: list) -> InlineKeyboardMarkup:
    """
    Список астропрофилей.
    Первая кнопка — «Я (имя)», затем все добавленные люди, внизу «Добавить».
    """
    buttons = [
        [InlineKeyboardButton(text=f"👤 Я ({user_name})", callback_data="profile:self")]
    ]
    for p in partners:
        chart = p.get('chart', {})
        sun = chart.get('sun_sign', '')
        label = f"🌟 {p['name']}"
        if sun:
            label += f" · ☉{sun}"
        buttons.append([
            InlineKeyboardButton(text=label, callback_data=f"profile:view:{p['id']}")
        ])
    buttons.append([
        InlineKeyboardButton(text="➕ Добавить профиль", callback_data="profile:add")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def no_profiles_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить профиль", callback_data="profile:add")],
    ])


def profile_actions_kb(profile_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔮 Натальная карта", callback_data=f"profile:natal:{profile_id}"),
            InlineKeyboardButton(text="📅 Прогноз", callback_data=f"profile:forecast:{profile_id}"),
        ],
        [
            InlineKeyboardButton(text="💑 Совместимость", callback_data=f"profile:compat:{profile_id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"profile:delete:{profile_id}"),
        ],
        [InlineKeyboardButton(text="◀️ Назад к списку", callback_data="profile:list")],
    ])


def confirm_delete_kb(profile_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"profile:confirm_delete:{profile_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="profile:list"),
        ],
    ])
