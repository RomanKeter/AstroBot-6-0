"""
Клавиатуры бота.
"""

from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove


def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔮 Моя карта"), KeyboardButton(text="📅 Прогноз")],
            [KeyboardButton(text="💑 Совместимость"), KeyboardButton(text="📍 Мой регион")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True
    )


def settings_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✏️ Изменить данные")],
            [KeyboardButton(text="📜 История"), KeyboardButton(text="🔙 Назад")],
        ],
        resize_keyboard=True
    )


def cancel_menu():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True
    )


def remove():
    return ReplyKeyboardRemove()
