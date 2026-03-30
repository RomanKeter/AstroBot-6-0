"""
Астрологический движок.
Используем только приблизительный расчёт (надёжно работает на любом хостинге).
Для точных расчётов можно подключить kerykeion или skyfield.
"""

import logging
from datetime import datetime
from typing import Dict, List

logger = logging.getLogger(__name__)

# Переводы
ZODIAC_SIGNS_RU = {
    'Aries': 'Овен', 'Taurus': 'Телец', 'Gemini': 'Близнецы',
    'Cancer': 'Рак', 'Leo': 'Лев', 'Virgo': 'Дева',
    'Libra': 'Весы', 'Scorpio': 'Скорпион', 'Sagittarius': 'Стрелец',
    'Capricorn': 'Козерог', 'Aquarius': 'Водолей', 'Pisces': 'Рыбы'
}

PLANETS_RU = {
    'Sun': 'Солнце', 'Moon': 'Луна', 'Mercury': 'Меркурий',
    'Venus': 'Венера', 'Mars': 'Марс', 'Jupiter': 'Юпитер',
    'Saturn': 'Сатурн', 'Uranus': 'Уран', 'Neptune': 'Нептун',
    'Pluto': 'Плутон'
}


def translate_sign(sign: str) -> str:
    return ZODIAC_SIGNS_RU.get(sign, sign)


def translate_planet(planet: str) -> str:
    return PLANETS_RU.get(planet, planet)


def get_sun_sign(day: int, month: int) -> str:
    """Знак зодиака по дате рождения."""
    # (месяц, день_начала, знак)
    zodiac = [
        (1, 20, 'Водолей'), (2, 19, 'Рыбы'), (3, 21, 'Овен'),
        (4, 20, 'Телец'), (5, 21, 'Близнецы'), (6, 21, 'Рак'),
        (7, 23, 'Лев'), (8, 23, 'Дева'), (9, 23, 'Весы'),
        (10, 23, 'Скорпион'), (11, 22, 'Стрелец'), (12, 22, 'Козерог'),
    ]

    for i, (m, d, sign) in enumerate(zodiac):
        if month == m and day >= d:
            return sign
        if month == m and day < d:
            # Возвращаем предыдущий знак
            return zodiac[i - 1][2]

    return 'Козерог'  # Декабрь 22 - Январь 19


def get_approx_moon_sign(day: int, month: int, year: int) -> str:
    """Очень приблизительный знак Луны (меняется каждые ~2.5 дня)."""
    signs = ['Овен', 'Телец', 'Близнецы', 'Рак', 'Лев', 'Дева',
             'Весы', 'Скорпион', 'Стрелец', 'Козерог', 'Водолей', 'Рыбы']
    # Простая формула на основе дня года
    day_of_year = datetime(year, month, day).timetuple().tm_yday
    # Луна проходит все знаки за ~27.3 дня
    index = int((day_of_year * 12 / 27.3) + year) % 12
    return signs[index]


def calculate_natal_chart(
    year: int, month: int, day: int,
    hour: int, minute: int,
    lat: float, lon: float,
    city: str = "Unknown"
) -> Dict:
    """Расчёт натальной карты (приблизительный, но надёжный)."""

    # Пробуем kerykeion (если установлен)
    try:
        from kerykeion import AstrologicalSubject
        subject = AstrologicalSubject(
            "User", year, month, day, hour, minute,
            city=city, lat=lat, lng=lon
        )

        planets = {}
        for p in ['sun', 'moon', 'mercury', 'venus', 'mars',
                   'jupiter', 'saturn', 'uranus', 'neptune', 'pluto']:
            obj = getattr(subject, p, None)
            if obj:
                planets[p.capitalize()] = {
                    'name': translate_planet(p.capitalize()),
                    'sign': translate_sign(obj['sign']),
                    'degree': round(obj['abs_pos'], 2),
                    'house': obj.get('house', ''),
                    'retrograde': obj.get('retrograde', False)
                }

        asc_sign = translate_sign(subject.first_house['sign']) if hasattr(subject, 'first_house') else None

        return {
            'planets': planets,
            'houses': {},
            'ascendant': asc_sign,
            'sun_sign': planets.get('Sun', {}).get('sign'),
            'moon_sign': planets.get('Moon', {}).get('sign'),
            'valid': True,
            'source': 'kerykeion'
        }

    except Exception as e:
        logger.info(f"Kerykeion unavailable ({e}), using approximate calculation")

    # Fallback: приблизительный расчёт
    sun_sign = get_sun_sign(day, month)
    moon_sign = get_approx_moon_sign(day, month, year)

    return {
        'planets': {
            'Sun': {'name': 'Солнце', 'sign': sun_sign, 'degree': 0, 'house': 1, 'retrograde': False},
            'Moon': {'name': 'Луна', 'sign': moon_sign, 'degree': 0, 'house': 4, 'retrograde': False},
        },
        'houses': {},
        'ascendant': None,
        'sun_sign': sun_sign,
        'moon_sign': moon_sign,
        'valid': True,
        'source': 'approximate',
        'warning': 'Приблизительный расчёт (без эфемерид)'
    }


def format_chart_text(chart: Dict, birth_info: Dict) -> str:
    """Форматировать карту для отображения."""
    lines = [
        f"☉ Солнце: {chart.get('sun_sign', '?')}",
        f"☽ Луна: {chart.get('moon_sign', 'неизвестно')}",
        f"↑ Асцендент: {chart.get('ascendant') or 'не рассчитан'}",
        "",
        f"📍 {birth_info.get('city', 'Неизвестно')}"
    ]

    if chart.get('source') == 'approximate':
        lines.append(f"⚠️ {chart.get('warning', '')}")

    if chart.get('planets') and len(chart['planets']) > 2:
        lines.extend(["", "Планеты:"])
        priority = ['Sun', 'Moon', 'Mercury', 'Venus', 'Mars',
                     'Jupiter', 'Saturn', 'Uranus', 'Neptune', 'Pluto']
        for key in priority:
            if key in chart['planets']:
                p = chart['planets'][key]
                retro = " ℞" if p.get('retrograde') else ""
                lines.append(f"  {p['name']}: {p['sign']}{retro}")

    return '\n'.join(lines)


def get_transits(chart: Dict, date: datetime = None) -> List[Dict]:
    """Заглушка для транзитов."""
    return []
