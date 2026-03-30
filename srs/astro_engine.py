"""
Астрологический движок.
Пробуем immanuel, если падает — используем flatlib как fallback.
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Переводы
ZODIAC_SIGNS_RU = {
    "Aries": "Овен", "Taurus": "Телец", "Gemini": "Близнецы",
    "Cancer": "Рак", "Leo": "Лев", "Virgo": "Дева",
    "Libra": "Весы", "Scorpio": "Скорпион", "Sagittarius": "Стрелец",
    "Capricorn": "Козерог", "Aquarius": "Водолей", "Pisces": "Рыбы",
}

PLANETS_RU = {
    "Sun": "Солнце", "Moon": "Луна", "Mercury": "Меркурий",
    "Venus": "Венера", "Mars": "Марс", "Jupiter": "Юпитер",
    "Saturn": "Сатурн", "Uranus": "Уран", "Neptune": "Нептун",
    "Pluto": "Плутон",
}

PLANET_ORDER = ["Sun", "Moon", "Mercury", "Venus", "Mars",
                "Jupiter", "Saturn", "Uranus", "Neptune", "Pluto"]


def translate_sign(sign: str) -> str:
    return ZODIAC_SIGNS_RU.get(sign, sign)


def translate_planet(planet: str) -> str:
    return PLANETS_RU.get(planet, planet)


def get_approx_sign(day: int, month: int) -> str:
    """Примерный знак зодиака по дате рождения."""
    # (месяц, день) — начало каждого знака
    boundaries = [
        (1, 20, "Водолей"), (2, 19, "Рыбы"), (3, 21, "Овен"),
        (4, 20, "Телец"), (5, 21, "Близнецы"), (6, 21, "Рак"),
        (7, 23, "Лев"), (8, 23, "Дева"), (9, 23, "Весы"),
        (10, 23, "Скорпион"), (11, 22, "Стрелец"), (12, 22, "Козерог"),
    ]
    sign = "Козерог"  # Дефолт (22 дек – 19 янв)
    for m, d, s in boundaries:
        if (month, day) >= (m, d):
            sign = s
    return sign


def calculate_with_immanuel(
    year: int, month: int, day: int,
    hour: int, minute: int,
    lat: float, lon: float,
) -> Optional[Dict]:
    """Расчёт через immanuel."""
    try:
        from immanuel import charts
        from immanuel.classes import serialize

        natal = charts.Natal(
            date_time=datetime(year, month, day, hour, minute),
            latitude=lat,
            longitude=lon,
        )
        serialized = serialize.to_dict(natal)

        planets = {}
        for name, data in serialized.get("objects", {}).items():
            if name in ("Mean_Lilith", "True_Node"):
                continue
            planets[name] = {
                "name": translate_planet(name),
                "sign": translate_sign(data.get("sign", "")),
                "degree": round(data.get("longitude", 0), 2),
                "house": data.get("house"),
                "retrograde": data.get("retrograde", False),
            }

        houses = {}
        for num, data in serialized.get("houses", {}).items():
            houses[int(num)] = {
                "sign": translate_sign(data.get("sign", "")),
                "degree": round(data.get("longitude", 0), 2),
            }

        return {
            "planets": planets,
            "houses": houses,
            "ascendant": houses.get(1, {}).get("sign"),
            "sun_sign": planets.get("Sun", {}).get("sign"),
            "moon_sign": planets.get("Moon", {}).get("sign"),
            "valid": True,
            "source": "immanuel",
        }

    except Exception as e:
        logger.warning(f"Immanuel failed: {e}")
        return None


def calculate_with_flatlib(
    year: int, month: int, day: int,
    hour: int, minute: int,
    lat: float, lon: float,
) -> Optional[Dict]:
    """Fallback на flatlib."""
    try:
        from flatlib import const
        from flatlib.chart import Chart
        from flatlib.datetime import Datetime as FlatDateTime
        from flatlib.geopos import GeoPos

        date = FlatDateTime(f"{year}/{month}/{day}", f"{hour}:{minute}", "+00:00")
        pos = GeoPos(lat, lon)
        chart = Chart(date, pos)

        planets = {}
        planet_map = [
            (const.SUN, "Sun"), (const.MOON, "Moon"),
            (const.MERCURY, "Mercury"), (const.VENUS, "Venus"),
            (const.MARS, "Mars"), (const.JUPITER, "Jupiter"),
            (const.SATURN, "Saturn"),
        ]
        for planet_const, name in planet_map:
            obj = chart.get(planet_const)
            planets[name] = {
                "name": translate_planet(name),
                "sign": translate_sign(obj.sign),
                "degree": round(obj.lon, 2),
                "house": getattr(obj, "house", None),
                "retrograde": getattr(obj, "retrograde", False),
            }

        asc = chart.get(const.ASC)

        houses = {}
        for i in range(1, 13):
            try:
                h = chart.get(const.HOUSES[i - 1])
                houses[i] = {"sign": translate_sign(h.sign), "degree": round(h.lon, 2)}
            except Exception:
                houses[i] = {"sign": "?", "degree": 0}

        return {
            "planets": planets,
            "houses": houses,
            "ascendant": translate_sign(asc.sign) if asc else None,
            "sun_sign": planets["Sun"]["sign"],
            "moon_sign": planets["Moon"]["sign"],
            "valid": True,
            "source": "flatlib",
        }

    except Exception as e:
        logger.warning(f"Flatlib failed: {e}")
        return None


def calculate_natal_chart(
    year: int, month: int, day: int,
    hour: int, minute: int,
    lat: float, lon: float,
    city: str = "Unknown",
) -> Dict:
    """Расчёт с fallback цепочкой: immanuel → flatlib → примерный."""

    result = calculate_with_immanuel(year, month, day, hour, minute, lat, lon)
    if result:
        logger.info(f"Chart via immanuel for {city}")
        return result

    result = calculate_with_flatlib(year, month, day, hour, minute, lat, lon)
    if result:
        logger.info(f"Chart via flatlib for {city}")
        return result

    # Fallback — примерные данные
    logger.warning(f"All engines failed for {city}, using approximate")
    approx_sign = get_approx_sign(day, month)
    return {
        "planets": {
            "Sun": {
                "name": "Солнце", "sign": approx_sign,
                "degree": 0, "house": 1, "retrograde": False,
            }
        },
        "houses": {i: {"sign": "?", "degree": 0} for i in range(1, 13)},
        "ascendant": None,
        "sun_sign": approx_sign,
        "moon_sign": None,
        "valid": True,
        "source": "approximate",
        "warning": "Точное время/место не учтены — данные приблизительные",
    }


def format_chart_text(chart: Dict, birth_info: Dict) -> str:
    """Форматировать карту в текст."""
    lines = [
        f"☉ Солнце: {chart.get('sun_sign', '?')}",
        f"☽ Луна: {chart.get('moon_sign') or 'неизвестно'}",
        f"↑ Асцендент: {chart.get('ascendant') or 'неизвестно'}",
        "",
        f"📍 {birth_info.get('city', 'Неизвестно')}",
    ]

    if chart.get("source") == "approximate":
        lines.append(f"⚠️ {chart.get('warning', '')}")

    planets = chart.get("planets", {})
    if len(planets) > 1:
        lines.extend(["", "Планеты:"])
        for key in PLANET_ORDER:
            if key in planets:
                p = planets[key]
                retro = " ℞" if p.get("retrograde") else ""
                lines.append(f"  {p['name']}: {p['sign']}{retro}")

    return "\n".join(lines)


def get_transits(chart: Dict, date: datetime | None = None) -> List[Dict]:
    """Заглушка для транзитов — пока без расчёта."""
    return []
