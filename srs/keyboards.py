"""
Клавиатуры бота.
"""

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔮 Моя карта"), KeyboardButton(text="📅 Прогноз")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True,
    )


def settings_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✏️ Изменить данные")],
            [KeyboardButton(text="📜 История"), KeyboardButton(text="🔙 Назад")],
        ],
        resize_keyboard=True,
    )


def cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
    )
