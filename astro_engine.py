"""
Астрологический движок.
Использует kerykeion для точных расчётов (если установлен),
иначе — улучшенный приблизительный расчёт.
"""

import logging
import math
from datetime import datetime
from typing import Dict, List

logger = logging.getLogger(__name__)

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

SIGNS_LIST = [
    'Овен', 'Телец', 'Близнецы', 'Рак', 'Лев', 'Дева',
    'Весы', 'Скорпион', 'Стрелец', 'Козерог', 'Водолей', 'Рыбы'
]


def translate_sign(sign: str) -> str:
    return ZODIAC_SIGNS_RU.get(sign, sign)


def translate_planet(planet: str) -> str:
    return PLANETS_RU.get(planet, planet)


def get_sun_sign(day: int, month: int) -> str:
    """Знак зодиака по дате рождения."""
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
            return zodiac[i - 1][2]
    return 'Козерог'


def _julian_day(year: int, month: int, day: int, hour: float = 12.0) -> float:
    """Вычислить Julian Day Number."""
    a = (14 - month) // 12
    y = year + 4800 - a
    m = month + 12 * a - 3
    jdn = (day + (153 * m + 2) // 5 + 365 * y
           + y // 4 - y // 100 + y // 400 - 32045)
    return jdn + (hour - 12) / 24.0


def get_approx_moon_sign(day: int, month: int, year: int, hour: int = 12) -> str:
    """
    Улучшенный расчёт знака Луны через Julian Day.
    Погрешность ±1 знак (~1-2 дня).

    Исправлено: убрано прибавление year, которое давало
    произвольные результаты. Используем астрономическое
    среднее движение Луны 13.176358°/день от эпохи J2000.
    """
    # Опорная точка J2000.0: лунная долгота ≈ 218.316°
    J2000_MOON_LON = 218.316
    J2000_JD = 2451545.0

    jd = _julian_day(year, month, day, float(hour))
    days_since_j2000 = jd - J2000_JD

    # Среднее движение Луны
    moon_lon = (J2000_MOON_LON + 13.176358 * days_since_j2000) % 360

    sign_index = int(moon_lon / 30) % 12
    return SIGNS_LIST[sign_index]


def get_approx_ascendant(hour: int, minute: int, lat: float, lon: float,
                         day: int, month: int, year: int) -> str:
    """
    Приблизительный расчёт асцендента через Local Sidereal Time.
    """
    jd = _julian_day(year, month, day, hour + minute / 60.0)
    d = jd - 2451545.0

    # Greenwich Sidereal Time
    gst = (280.46061837 + 360.98564736629 * d) % 360
    lst = (gst + lon) % 360

    lat_rad = math.radians(lat)
    lst_rad = math.radians(lst)
    eps = math.radians(23.4393)  # наклон эклиптики

    asc_lon = math.degrees(math.atan2(
        math.cos(lst_rad),
        -(math.sin(lst_rad) * math.cos(eps)
          + math.tan(lat_rad) * math.sin(eps))
    )) % 360

    sign_index = int(asc_lon / 30) % 12
    return SIGNS_LIST[sign_index]


def calculate_natal_chart(
    year: int, month: int, day: int,
    hour: int, minute: int,
    lat: float, lon: float,
    city: str = "Unknown",
    timezone: str = "UTC"
) -> Dict:
    """Расчёт натальной карты: сначала kerykeion, потом улучшенный fallback."""

    # Пробуем kerykeion
    try:
        from kerykeion import AstrologicalSubject
        subject = AstrologicalSubject(
            "User", year, month, day, hour, minute,
            lat=lat, lng=lon, tz_str=timezone
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

        asc_sign = None
        if hasattr(subject, 'first_house'):
            asc_sign = translate_sign(subject.first_house['sign'])

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
        logger.info(f"Kerykeion недоступен ({e}), использую улучшенный расчёт")

    # Улучшенный приблизительный расчёт
    sun_sign  = get_sun_sign(day, month)
    moon_sign = get_approx_moon_sign(day, month, year, hour)
    asc_sign  = get_approx_ascendant(hour, minute, lat, lon, day, month, year)

    return {
        'planets': {
            'Sun':  {'name': 'Солнце', 'sign': sun_sign,  'degree': 0, 'house': 1, 'retrograde': False},
            'Moon': {'name': 'Луна',   'sign': moon_sign, 'degree': 0, 'house': 4, 'retrograde': False},
        },
        'houses': {},
        'ascendant': asc_sign,
        'sun_sign': sun_sign,
        'moon_sign': moon_sign,
        'valid': True,
        'source': 'approximate',
        'warning': 'Приблизительный расчёт. Установите kerykeion для точных данных.'
    }


def calculate_compatibility(chart1: Dict, chart2: Dict) -> Dict:
    """
    Расчёт совместимости двух натальных карт (базовая синастрия).
    """
    score = 0
    aspects = []

    sun1   = chart1.get('sun_sign')
    sun2   = chart2.get('sun_sign')
    moon1  = chart1.get('moon_sign')
    moon2  = chart2.get('moon_sign')

    fire  = {'Овен', 'Лев', 'Стрелец'}
    earth = {'Телец', 'Дева', 'Козерог'}
    air   = {'Близнецы', 'Весы', 'Водолей'}
    water = {'Рак', 'Скорпион', 'Рыбы'}

    compatible_el = {
        ('огонь', 'воздух'), ('воздух', 'огонь'),
        ('земля', 'вода'),   ('вода', 'земля')
    }

    def get_element(sign):
        if sign in fire:  return 'огонь'
        if sign in earth: return 'земля'
        if sign in air:   return 'воздух'
        if sign in water: return 'вода'
        return None

    # Солнце–Солнце
    el1, el2 = get_element(sun1), get_element(sun2)
    if sun1 == sun2:
        score += 20
        aspects.append("☉ Одинаковые солнечные знаки — глубокое взаимопонимание")
    elif el1 and el2:
        if el1 == el2:
            score += 15
            aspects.append(f"☉ Солнца одной стихии ({el1}) — схожие ценности")
        elif (el1, el2) in compatible_el:
            score += 10
            aspects.append(f"☉ Солнца в совместимых стихиях ({el1} + {el2})")
        else:
            score += 3
            aspects.append("☉ Разные стихии Солнца — нужна работа над пониманием")

    # Луна–Луна
    el_m1, el_m2 = get_element(moon1), get_element(moon2)
    if moon1 == moon2:
        score += 20
        aspects.append("☽ Одинаковые лунные знаки — эмоциональная близость")
    elif el_m1 and el_m2:
        if el_m1 == el_m2:
            score += 15
            aspects.append("☽ Луны одной стихии — общий эмоциональный язык")
        elif (el_m1, el_m2) in compatible_el:
            score += 10
            aspects.append(f"☽ Луны в гармонии ({el_m1} + {el_m2})")
        else:
            score += 3
            aspects.append("☽ Разные лунные стихии — учитесь слышать друг друга")

    # Солнце–Луна взаимодействие
    if sun1 == moon2 or sun2 == moon1:
        score += 15
        aspects.append("✨ Солнце одного партнёра совпадает с Луной другого — классическая совместимость")

    score = min(score, 100)

    if score >= 70:
        level, emoji = "Высокая", "💚"
    elif score >= 45:
        level, emoji = "Хорошая", "💛"
    elif score >= 25:
        level, emoji = "Средняя", "🧡"
    else:
        level, emoji = "Сложная", "❤️‍🔥"

    return {'score': score, 'level': level, 'emoji': emoji, 'aspects': aspects}


def format_chart_text(chart: Dict, birth_info: Dict) -> str:
    """Форматировать карту для отображения."""
    lines = [
        f"☉ Солнце: {chart.get('sun_sign', '?')}",
        f"☽ Луна: {chart.get('moon_sign', 'неизвестно')}",
        f"↑ Асцендент: {chart.get('ascendant') or 'не рассчитан'}",
        "",
        f"📍 {birth_info.get('city', 'Неизвестно')}",
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
