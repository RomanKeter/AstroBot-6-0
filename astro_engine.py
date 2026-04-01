"""
Астрологический движок — западная астрология.
Использует библиотеку kerykeion (Swiss Ephemeris) для точных расчётов.
Fallback: формулы Мееуса (если kerykeion/swisseph недоступны).

Расширено: аспекты между планетами, стихии, кресты, расширенная сводка.
"""

import logging
import math
from datetime import datetime
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# ── Переводы ──────────────────────────────────────────────────────────────────

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

HOUSE_MEANINGS = {
    1:  ("Дом личности",       "Внешность, первое впечатление, характер, физическое тело, начинания."),
    2:  ("Дом ресурсов",       "Деньги, имущество, самооценка, материальная безопасность, таланты."),
    3:  ("Дом коммуникаций",   "Речь, мышление, братья и сёстры, короткие поездки, обучение."),
    4:  ("Дом корней",         "Семья, дом, детство, родители, недвижимость, основа личности."),
    5:  ("Дом творчества",     "Творчество, романтика, дети, удовольствия, игра, самовыражение."),
    6:  ("Дом здоровья",       "Здоровье, ежедневный труд, режим, слуги, домашние животные."),
    7:  ("Дом партнёрств",     "Брак, деловые союзы, открытые противники, публичные отношения."),
    8:  ("Дом трансформации",  "Смерть и возрождение, чужие ресурсы, сексуальность, тайны, наследство."),
    9:  ("Дом философии",      "Дальние путешествия, высшее образование, религия, философия, закон."),
    10: ("Дом карьеры",        "Профессия, репутация, социальный статус, отношения с властью, призвание."),
    11: ("Дом дружбы",         "Друзья, группы, мечты, социальные идеалы, технологии, будущее."),
    12: ("Дом тайн",           "Подсознание, изоляция, скрытые враги, духовность, кармические уроки."),
}

SIGN_RULERS = {
    'Овен': 'Марс', 'Телец': 'Венера', 'Близнецы': 'Меркурий',
    'Рак': 'Луна', 'Лев': 'Солнце', 'Дева': 'Меркурий',
    'Весы': 'Венера', 'Скорпион': 'Плутон', 'Стрелец': 'Юпитер',
    'Козерог': 'Сатурн', 'Водолей': 'Уран', 'Рыбы': 'Нептун',
}

# ── Стихии и кресты ──────────────────────────────────────────────────────────

ELEMENTS = {
    'Овен': 'Огонь', 'Лев': 'Огонь', 'Стрелец': 'Огонь',
    'Телец': 'Земля', 'Дева': 'Земля', 'Козерог': 'Земля',
    'Близнецы': 'Воздух', 'Весы': 'Воздух', 'Водолей': 'Воздух',
    'Рак': 'Вода', 'Скорпион': 'Вода', 'Рыбы': 'Вода',
}

QUALITIES = {
    'Овен': 'Кардинальный', 'Рак': 'Кардинальный', 'Весы': 'Кардинальный', 'Козерог': 'Кардинальный',
    'Телец': 'Фиксированный', 'Лев': 'Фиксированный', 'Скорпион': 'Фиксированный', 'Водолей': 'Фиксированный',
    'Близнецы': 'Мутабельный', 'Дева': 'Мутабельный', 'Стрелец': 'Мутабельный', 'Рыбы': 'Мутабельный',
}

# ── Аспекты ──────────────────────────────────────────────────────────────────

ASPECTS = {
    'Соединение':    (0,   8),
    'Секстиль':      (60,  6),
    'Квадратура':     (90,  7),
    'Тригон':         (120, 8),
    'Оппозиция':      (180, 8),
    'Квинконс':       (150, 3),
    'Полусекстиль':   (30,  2),
}

ASPECT_NATURE = {
    'Соединение':  'нейтральный (зависит от планет)',
    'Секстиль':    'гармоничный',
    'Квадратура':   'напряжённый',
    'Тригон':       'гармоничный',
    'Оппозиция':    'напряжённый',
    'Квинконс':     'напряжённый (скрытый)',
    'Полусекстиль': 'слабо гармоничный',
}


def translate_sign(sign: str) -> str:
    return ZODIAC_SIGNS_RU.get(sign, sign)


def translate_planet(planet: str) -> str:
    return PLANETS_RU.get(planet, planet)


# ── Расчёт аспектов ──────────────────────────────────────────────────────────

def calculate_aspects(planets: Dict) -> List[Dict]:
    """Рассчитать аспекты между всеми парами натальных планет."""
    aspect_list = []
    planet_keys = list(planets.keys())

    for i in range(len(planet_keys)):
        for j in range(i + 1, len(planet_keys)):
            p1_key = planet_keys[i]
            p2_key = planet_keys[j]
            p1 = planets[p1_key]
            p2 = planets[p2_key]

            deg1 = p1.get('degree', 0)
            deg2 = p2.get('degree', 0)

            diff = abs(deg1 - deg2)
            if diff > 180:
                diff = 360 - diff

            for aspect_name, (exact_angle, orb) in ASPECTS.items():
                if abs(diff - exact_angle) <= orb:
                    aspect_list.append({
                        'planet1': p1.get('name', p1_key),
                        'planet2': p2.get('name', p2_key),
                        'aspect': aspect_name,
                        'nature': ASPECT_NATURE[aspect_name],
                        'orb': round(abs(diff - exact_angle), 1),
                        'exact_angle': exact_angle,
                    })
                    break  # одна пара — один аспект (самый точный)

    return aspect_list


def calculate_transit_aspects(natal_planets: Dict, transit_planets: Dict) -> List[Dict]:
    """Рассчитать аспекты транзитных планет к натальным."""
    aspect_list = []

    for t_key, t_planet in transit_planets.items():
        for n_key, n_planet in natal_planets.items():
            t_deg = t_planet.get('degree', 0)
            n_deg = n_planet.get('degree', 0)

            diff = abs(t_deg - n_deg)
            if diff > 180:
                diff = 360 - diff

            for aspect_name, (exact_angle, orb) in ASPECTS.items():
                # Для транзитов используем более узкие орбисы
                transit_orb = orb * 0.7
                if abs(diff - exact_angle) <= transit_orb:
                    aspect_list.append({
                        'transit_planet': t_planet.get('name', t_key),
                        'natal_planet': n_planet.get('name', n_key),
                        'aspect': aspect_name,
                        'nature': ASPECT_NATURE[aspect_name],
                        'orb': round(abs(diff - exact_angle), 1),
                    })
                    break

    return aspect_list


def analyze_element_balance(chart: Dict) -> Dict:
    """Анализ баланса стихий и крестов в натальной карте."""
    elements = {'Огонь': 0, 'Земля': 0, 'Воздух': 0, 'Вода': 0}
    qualities = {'Кардинальный': 0, 'Фиксированный': 0, 'Мутабельный': 0}

    # Веса планет (личные планеты весят больше)
    weights = {
        'Sun': 3, 'Moon': 3, 'Mercury': 2, 'Venus': 2, 'Mars': 2,
        'Jupiter': 1, 'Saturn': 1, 'Uranus': 1, 'Neptune': 1, 'Pluto': 1,
    }

    planets = chart.get('planets', {})
    for key, p in planets.items():
        sign = p.get('sign', '')
        w = weights.get(key, 1)
        if sign in ELEMENTS:
            elements[ELEMENTS[sign]] += w
        if sign in QUALITIES:
            qualities[QUALITIES[sign]] += w

    # Добавляем Асцендент
    asc = chart.get('ascendant', '')
    if asc in ELEMENTS:
        elements[ELEMENTS[asc]] += 2
    if asc in QUALITIES:
        qualities[QUALITIES[asc]] += 2

    dominant_element = max(elements, key=elements.get)
    weak_element = min(elements, key=elements.get)
    dominant_quality = max(qualities, key=qualities.get)

    return {
        'elements': elements,
        'qualities': qualities,
        'dominant_element': dominant_element,
        'weak_element': weak_element,
        'dominant_quality': dominant_quality,
    }


# ── Вспомогательные ───────────────────────────────────────────────────────────

def _lon_to_sign(lon: float) -> Tuple[str, float]:
    """Долгота → (знак, градус_в_знаке)."""
    idx = int(lon / 30) % 12
    return SIGNS_LIST[idx], round(lon % 30, 1)


def _planet_in_house(planet_lon: float, cusps: Dict[int, float]) -> int:
    """Номер дома для данной долготы."""
    for h in range(12, 0, -1):
        cusp      = cusps[h]
        next_cusp = cusps[h % 12 + 1]
        if next_cusp > cusp:
            if cusp <= planet_lon < next_cusp:
                return h
        else:
            if planet_lon >= cusp or planet_lon < next_cusp:
                return h
    return 1


# ── Meeus fallback ────────────────────────────────────────────────────────────

def _julian_day(year: int, month: int, day: int, hour: float = 12.0) -> float:
    a = (14 - month) // 12
    y = year + 4800 - a
    m = month + 12 * a - 3
    jdn = (day + (153 * m + 2) // 5 + 365 * y
           + y // 4 - y // 100 + y // 400 - 32045)
    return jdn + (hour - 12) / 24.0


def _moon_longitude_meeus(year: int, month: int, day: int, hour: float = 12.0) -> float:
    jd = _julian_day(year, month, day, hour)
    T = (jd - 2451545.0) / 36525.0
    Lp = (218.3164477 + 481267.88123421 * T
          - 0.0015786 * T**2 + T**3 / 538841 - T**4 / 65194000) % 360
    D  = (297.8501921 + 445267.1114034 * T
          - 0.0018819 * T**2 + T**3 / 545868 - T**4 / 113065000) % 360
    M  = (357.5291092 + 35999.0502909 * T
          - 0.0001536 * T**2 + T**3 / 24490000) % 360
    Mp = (134.9633964 + 477198.8675055 * T
          + 0.0087414 * T**2 + T**3 / 69699 - T**4 / 14712000) % 360
    F  = (93.2720950  + 483202.0175233 * T
          - 0.0036539 * T**2 - T**3 / 3526000 + T**4 / 863310000) % 360
    D_r, M_r, Mp_r, F_r = map(math.radians, [D, M, Mp, F])
    sigma_l = (
          6288774 * math.sin(Mp_r) + 1274027 * math.sin(2*D_r - Mp_r)
        +  658314 * math.sin(2*D_r) + 213618 * math.sin(2*Mp_r)
        -  185116 * math.sin(M_r) - 114332 * math.sin(2*F_r)
        +   58793 * math.sin(2*D_r - 2*Mp_r) + 57066 * math.sin(2*D_r - M_r - Mp_r)
        +   53322 * math.sin(2*D_r + Mp_r) + 45758 * math.sin(2*D_r - M_r)
        -   40923 * math.sin(M_r - Mp_r) - 34720 * math.sin(D_r)
        -   30383 * math.sin(M_r + Mp_r) + 15327 * math.sin(2*D_r - 2*F_r)
        -   12528 * math.sin(Mp_r + 2*F_r) + 10980 * math.sin(Mp_r - 2*F_r)
        +   10675 * math.sin(4*D_r - Mp_r) + 10034 * math.sin(3*Mp_r)
        +    8548 * math.sin(4*D_r - 2*Mp_r) - 7888 * math.sin(2*D_r + M_r - Mp_r)
        -    6766 * math.sin(2*D_r + M_r) - 5163 * math.sin(D_r - Mp_r)
        +    4987 * math.sin(D_r + M_r) + 4036 * math.sin(2*D_r - M_r + Mp_r)
        +    3994 * math.sin(2*D_r + 2*Mp_r) + 3861 * math.sin(4*D_r)
        +    3665 * math.sin(2*D_r - 3*Mp_r)
    )
    return (Lp + sigma_l / 1000000.0) % 360


def _sun_longitude(year: int, month: int, day: int, hour: float = 12.0) -> float:
    jd = _julian_day(year, month, day, hour)
    T = (jd - 2451545.0) / 36525.0
    L0 = (280.46646 + 36000.76983 * T + 0.0003032 * T**2) % 360
    M  = math.radians((357.52911 + 35999.05029 * T - 0.0001537 * T**2) % 360)
    C  = ((1.914602 - 0.004817 * T - 0.000014 * T**2) * math.sin(M)
          + (0.019993 - 0.000101 * T) * math.sin(2 * M)
          + 0.000289 * math.sin(3 * M))
    return (L0 + C) % 360


def _planet_mean_longitude(year: int, month: int, day: int, hour: float = 12.0) -> Dict[str, float]:
    jd = _julian_day(year, month, day, hour)
    d  = jd - 2451545.0
    T  = d / 36525.0
    return {
        'Mercury': (252.251 + 4.09233445 * d + 0.00030350 * T**2) % 360,
        'Venus':   (181.979 + 1.60213034 * d + 0.00031014 * T**2) % 360,
        'Mars':    (355.433 + 0.52402068 * d - 0.00027279 * T**2) % 360,
        'Jupiter': (34.351  + 0.08308529 * d + 0.00022059 * T**2) % 360,
        'Saturn':  (50.077  + 0.03344414 * d - 0.00055753 * T**2) % 360,
        'Uranus':  (314.055 + 0.01176904 * d + 0.00030390 * T**2) % 360,
        'Neptune': (304.348 + 0.00598103 * d - 0.00026600 * T**2) % 360,
        'Pluto':   (238.929 + 0.00397100 * d) % 360,
    }


def _ascendant_longitude(year: int, month: int, day: int,
                          hour: int, minute: int,
                          lat: float, lon: float) -> float:
    jd  = _julian_day(year, month, day, hour + minute / 60.0)
    d   = jd - 2451545.0
    gst = (280.46061837 + 360.98564736629 * d) % 360
    lst = (gst + lon) % 360
    lat_r = math.radians(lat)
    lst_r = math.radians(lst)
    eps_r = math.radians(23.4393)
    return math.degrees(math.atan2(
        math.cos(lst_r),
        -(math.sin(lst_r) * math.cos(eps_r) + math.tan(lat_r) * math.sin(eps_r))
    )) % 360


def _meeus_fallback(year, month, day, hour, minute, lat, lon, city, timezone) -> Dict:
    """Расчёт через формулы Мееуса (менее точный fallback)."""
    h_float = hour + minute / 60.0
    sun_lon  = _sun_longitude(year, month, day, h_float)
    moon_lon = _moon_longitude_meeus(year, month, day, h_float)
    asc_lon  = _ascendant_longitude(year, month, day, hour, minute, lat, lon)
    other    = _planet_mean_longitude(year, month, day, h_float)

    all_lons = {'Sun': sun_lon, 'Moon': moon_lon, **other}
    cusps    = {i: (asc_lon + (i - 1) * 30) % 360 for i in range(1, 13)}

    planets = {}
    for name_en, p_lon in all_lons.items():
        sign, deg = _lon_to_sign(p_lon)
        planets[name_en] = {
            'name':       translate_planet(name_en),
            'sign':       sign,
            'degree':     round(p_lon, 2),
            'sign_degree': deg,
            'house':      _planet_in_house(p_lon, cusps),
            'retrograde': False,
        }

    asc_sign, asc_deg = _lon_to_sign(asc_lon)
    houses = {}
    for i in range(1, 13):
        sign, deg = _lon_to_sign(cusps[i])
        houses[i] = {
            'sign':    sign,
            'degree':  deg,
            'name':    HOUSE_MEANINGS[i][0],
            'meaning': HOUSE_MEANINGS[i][1],
            'ruler':   SIGN_RULERS.get(sign, '?'),
        }

    # Рассчитываем аспекты
    aspects = calculate_aspects(planets)

    return {
        'planets':    planets,
        'houses':     houses,
        'aspects':    aspects,
        'ascendant':  asc_sign,
        'asc_degree': asc_deg,
        'sun_sign':   planets['Sun']['sign'],
        'moon_sign':  planets['Moon']['sign'],
        'valid':      True,
        'source':     'meeus',
        'warning':    'Расчёт по формулам Мееуса. Установите kerykeion для максимальной точности.',
    }


# ── Kerykeion (точный расчёт) ────────────────────────────────────────────────

def _kerykeion_calc(year, month, day, hour, minute, lat, lon, city, timezone) -> Dict:
    from kerykeion import AstrologicalSubject

    subject = AstrologicalSubject(
        "User", year, month, day, hour, minute,
        lat=lat, lng=lon, tz_str=timezone, city=city
    )

    PLANET_ATTRS = {
        'Sun': 'sun', 'Moon': 'moon', 'Mercury': 'mercury',
        'Venus': 'venus', 'Mars': 'mars', 'Jupiter': 'jupiter',
        'Saturn': 'saturn', 'Uranus': 'uranus', 'Neptune': 'neptune',
        'Pluto': 'pluto',
    }

    planets = {}
    for eng_name, attr_name in PLANET_ATTRS.items():
        obj = getattr(subject, attr_name, None)
        if obj is None:
            continue
        sign_en = getattr(obj, 'sign', None)
        abs_pos = getattr(obj, 'abs_pos', 0.0)
        position = getattr(obj, 'position', abs_pos % 30)
        house_num = getattr(obj, 'house', '')
        retro = getattr(obj, 'retrograde', False)

        planets[eng_name] = {
            'name':        translate_planet(eng_name),
            'sign':        translate_sign(sign_en) if sign_en else '?',
            'degree':      round(abs_pos, 2),
            'sign_degree': round(position, 1),
            'house':       house_num,
            'retrograde':  retro,
        }

    asc_sign = '?'
    asc_degree = 0.0
    first_house = getattr(subject, 'first_house', None)
    if first_house:
        asc_sign_en = getattr(first_house, 'sign', None)
        asc_sign = translate_sign(asc_sign_en) if asc_sign_en else '?'
        asc_degree = round(getattr(first_house, 'position', 0.0), 1)

    HOUSE_ATTRS = [
        'first_house', 'second_house', 'third_house', 'fourth_house',
        'fifth_house', 'sixth_house', 'seventh_house', 'eighth_house',
        'ninth_house', 'tenth_house', 'eleventh_house', 'twelfth_house'
    ]

    houses = {}
    for i, attr_name in enumerate(HOUSE_ATTRS, 1):
        house_obj = getattr(subject, attr_name, None)
        if house_obj:
            h_sign_en = getattr(house_obj, 'sign', None)
            h_sign = translate_sign(h_sign_en) if h_sign_en else '?'
            h_pos = round(getattr(house_obj, 'position', 0.0), 1)
        else:
            h_sign = '?'
            h_pos = 0.0

        houses[i] = {
            'sign':    h_sign,
            'degree':  h_pos,
            'name':    HOUSE_MEANINGS[i][0],
            'meaning': HOUSE_MEANINGS[i][1],
            'ruler':   SIGN_RULERS.get(h_sign, '?'),
        }

    # Рассчитываем аспекты
    aspects = calculate_aspects(planets)

    return {
        'planets':    planets,
        'houses':     houses,
        'aspects':    aspects,
        'ascendant':  asc_sign,
        'asc_degree': asc_degree,
        'sun_sign':   planets.get('Sun', {}).get('sign', '?'),
        'moon_sign':  planets.get('Moon', {}).get('sign', '?'),
        'valid':      True,
        'source':     'kerykeion',
    }


# ── Основная функция ─────────────────────────────────────────────────────────

def get_sun_sign(day: int, month: int) -> str:
    dates = [
        (120, 'Козерог'), (219, 'Водолей'), (320, 'Рыбы'),
        (420, 'Овен'), (521, 'Телец'), (621, 'Близнецы'),
        (722, 'Рак'), (823, 'Лев'), (923, 'Дева'),
        (1023, 'Весы'), (1122, 'Скорпион'), (1222, 'Стрелец'),
    ]
    md = month * 100 + day
    for limit, sign in dates:
        if md <= limit:
            return sign
    return 'Козерог'


def calculate_natal_chart(
    year: int, month: int, day: int,
    hour: int, minute: int,
    lat: float, lon: float,
    city: str = "Unknown",
    timezone: str = "UTC"
) -> Dict:
    try:
        result = _kerykeion_calc(year, month, day, hour, minute, lat, lon, city, timezone)
        logger.info("Натальная карта рассчитана через kerykeion (Swiss Ephemeris)")
        return result
    except Exception as e:
        logger.warning(f"Kerykeion недоступен ({e}), переключаюсь на формулы Мееуса")

    return _meeus_fallback(year, month, day, hour, minute, lat, lon, city, timezone)


# ── Текущие транзиты ──────────────────────────────────────────────────────────

def get_current_transits(date: datetime = None) -> Dict:
    if date is None:
        date = datetime.utcnow()

    try:
        from kerykeion import AstrologicalSubject
        subject = AstrologicalSubject(
            "Transit", date.year, date.month, date.day,
            date.hour, date.minute,
            lat=0.0, lng=0.0, tz_str="UTC"
        )
        PLANET_ATTRS = {
            'Sun': 'sun', 'Moon': 'moon', 'Mercury': 'mercury',
            'Venus': 'venus', 'Mars': 'mars', 'Jupiter': 'jupiter',
            'Saturn': 'saturn', 'Uranus': 'uranus', 'Neptune': 'neptune',
            'Pluto': 'pluto',
        }
        transits = {}
        for eng_name, attr_name in PLANET_ATTRS.items():
            obj = getattr(subject, attr_name, None)
            if obj:
                sign_en = getattr(obj, 'sign', None)
                retro = getattr(obj, 'retrograde', False)
                transits[eng_name] = {
                    'name': translate_planet(eng_name),
                    'sign': translate_sign(sign_en) if sign_en else '?',
                    'degree': round(getattr(obj, 'abs_pos', 0.0), 2),
                    'retrograde': retro,
                }
        return transits
    except Exception:
        h_float = date.hour + date.minute / 60.0
        sun_lon = _sun_longitude(date.year, date.month, date.day, h_float)
        moon_lon = _moon_longitude_meeus(date.year, date.month, date.day, h_float)
        other = _planet_mean_longitude(date.year, date.month, date.day, h_float)
        all_lons = {'Sun': sun_lon, 'Moon': moon_lon, **other}
        transits = {}
        for name_en, p_lon in all_lons.items():
            sign, deg = _lon_to_sign(p_lon)
            transits[name_en] = {
                'name': translate_planet(name_en),
                'sign': sign,
                'degree': round(p_lon, 2),
                'retrograde': False,
            }
        return transits


def format_transits_text(transits: Dict) -> str:
    lines = []
    for key in ['Sun', 'Moon', 'Mercury', 'Venus', 'Mars',
                'Jupiter', 'Saturn', 'Uranus', 'Neptune', 'Pluto']:
        t = transits.get(key)
        if t:
            retro = " (ретроградный)" if t.get('retrograde') else ""
            lines.append(f"{t['name']}: {t['sign']}{retro}")
    return "\n".join(lines)


def get_transits(chart: Dict, date: datetime = None) -> List[Dict]:
    """Заглушка для совместимости."""
    return []


# ── Форматирование ────────────────────────────────────────────────────────────

def format_chart_text(chart: Dict, birth_info: Dict = None) -> str:
    if birth_info is None:
        birth_info = {}
    lines = [
        f"☉ Солнце:     {chart.get('sun_sign', '?')}",
        f"☽ Луна:       {chart.get('moon_sign', '?')}",
        f"↑ Асцендент:  {chart.get('ascendant') or '?'}",
        "",
        f"📍 {birth_info.get('city', '?')}",
    ]
    if chart.get('source') == 'meeus':
        lines.append(f"⚠️ {chart.get('warning', '')}")
    return '\n'.join(lines)


def format_full_chart_text(chart: Dict) -> str:
    lines = ["🪐 <b>Планеты:</b>"]
    for key in ['Sun', 'Moon', 'Mercury', 'Venus', 'Mars',
                'Jupiter', 'Saturn', 'Uranus', 'Neptune', 'Pluto']:
        p = chart.get('planets', {}).get(key)
        if not p:
            continue
        retro = " ℞" if p.get('retrograde') else ""
        house = f" · Дом {p['house']}" if p.get('house') else ""
        deg   = f" {p.get('sign_degree', '')}°" if p.get('sign_degree') is not None else ""
        lines.append(f"  {p['name']}: <b>{p['sign']}</b>{deg}{retro}{house}")

    lines += ["", "🏠 <b>12 домов:</b>"]
    for i in range(1, 13):
        h = chart.get('houses', {}).get(i) or chart.get('houses', {}).get(str(i))
        if not h:
            continue
        ruler = f" · Упр: {h['ruler']}" if h.get('ruler') else ""
        lines.append(f"\n  <b>Дом {i} — {h['name']}</b>")
        lines.append(f"  Знак: {h['sign']}{ruler}")
        lines.append(f"  {h['meaning']}")

    # Аспекты
    aspects = chart.get('aspects', [])
    if aspects:
        lines += ["", "🔗 <b>Аспекты:</b>"]
        for a in aspects:
            nature_icon = "✅" if "гармоничный" in a['nature'] else "⚡" if "напряжённый" in a['nature'] else "🔵"
            lines.append(f"  {nature_icon} {a['planet1']} {a['aspect']} {a['planet2']} (орбис {a['orb']}°)")

    if chart.get('source') == 'meeus':
        lines += ["", f"⚠️ {chart.get('warning', '')}"]
    elif chart.get('source') == 'kerykeion':
        lines += ["", "✅ Рассчитано через Swiss Ephemeris (высокая точность)"]
    return '\n'.join(lines)


# ── Промпты для AI ────────────────────────────────────────────────────────────

def build_natal_summary(chart: Dict) -> str:
    """Расширенная сводка натальной карты для AI-промпта."""
    lines = [f"Асцендент: {chart.get('ascendant', '?')}"]
    for key in ['Sun', 'Moon', 'Mercury', 'Venus', 'Mars',
                'Jupiter', 'Saturn', 'Uranus', 'Neptune', 'Pluto']:
        p = chart.get('planets', {}).get(key)
        if p:
            retro = " (ретро)" if p.get('retrograde') else ""
            lines.append(f"{p['name']}: {p['sign']}, Дом {p.get('house', '?')}{retro}")

    # Аспекты
    aspects = chart.get('aspects', [])
    if aspects:
        lines.append("\nАспекты:")
        for a in aspects:
            lines.append(f"  {a['planet1']} {a['aspect']} {a['planet2']} ({a['nature']}, орбис {a['orb']}°)")

    # Баланс стихий
    balance = analyze_element_balance(chart)
    lines.append(f"\nДоминирующая стихия: {balance['dominant_element']}")
    lines.append(f"Слабая стихия: {balance['weak_element']}")
    lines.append(f"Доминирующий крест: {balance['dominant_quality']}")
    el = balance['elements']
    lines.append(f"Баланс стихий: Огонь={el['Огонь']}, Земля={el['Земля']}, Воздух={el['Воздух']}, Вода={el['Вода']}")

    return "\n".join(lines)


def build_natal_summary_short(chart: Dict) -> str:
    """Краткая сводка для inline-ответов."""
    lines = []
    for key in ['Sun', 'Moon', 'Mercury', 'Venus', 'Mars']:
        p = chart.get('planets', {}).get(key)
        if p:
            retro = " ℞" if p.get('retrograde') else ""
            lines.append(f"{p['name']}: {p['sign']}{retro}")
    lines.append(f"Асцендент: {chart.get('ascendant', '?')}")
    return ", ".join(lines)


def build_houses_prompt(chart: Dict, user_name: str = "пользователь") -> str:
    lines = [
        "Ты профессиональный астролог, специализируешься на западной астрологии.",
        f"Дай подробную интерпретацию натальной карты.",
        "",
        build_natal_summary(chart),
        "",
        "Опиши каждый из 12 домов по плану:",
        "- Тема дома",
        "- Знак на куспиде и что это значит",
        "- Планеты в доме (если есть) и их влияние",
        "- Конкретный практический вывод для человека",
        "",
        "Формат ответа: Дом 1, Дом 2, ... Дом 12.",
        "Стиль: психологически глубокий, конкретный, без общих фраз.",
        "Объём: подробно, не менее 2–3 предложений на каждый дом.",
    ]
    return '\n'.join(lines)


def build_partner_prompt(chart: Dict, name: str) -> str:
    lines = [
        f"Ты астролог. Дай краткий психологический портрет человека по натальной карте.",
        f"Имя: {name}",
        build_natal_summary(chart),
        "",
        "Структура:",
        "1. Личность и характер (Солнце + Асцендент)",
        "2. Эмоциональный мир (Луна)",
        "3. Как общается и думает (Меркурий)",
        "4. Любовь и ценности (Венера)",
        "5. Энергия и воля (Марс)",
        "6. Главный совет",
        "Стиль: конкретный, без воды.",
    ]
    return '\n'.join(lines)


# ── Совместимость ─────────────────────────────────────────────────────────────

def calculate_compatibility(chart1: Dict, chart2: Dict) -> Dict:
    """Расширенная синастрия: стихии + аспекты между картами."""
    score   = 0
    aspects = []

    sun1, sun2   = chart1.get('sun_sign'),  chart2.get('sun_sign')
    moon1, moon2 = chart1.get('moon_sign'), chart2.get('moon_sign')
    asc1, asc2   = chart1.get('ascendant'), chart2.get('ascendant')

    fire  = {'Овен', 'Лев', 'Стрелец'}
    earth = {'Телец', 'Дева', 'Козерог'}
    air   = {'Близнецы', 'Весы', 'Водолей'}
    water = {'Рак', 'Скорпион', 'Рыбы'}
    compat_el = {('огонь','воздух'),('воздух','огонь'),('земля','вода'),('вода','земля')}

    def el(s):
        if s in fire:  return 'огонь'
        if s in earth: return 'земля'
        if s in air:   return 'воздух'
        if s in water: return 'вода'
        return None

    def check_pair(s1, s2, label):
        nonlocal score
        e1, e2 = el(s1), el(s2)
        if s1 == s2:
            score += 20; aspects.append(f"{label} Один знак — глубокая близость")
        elif e1 and e2:
            if e1 == e2:
                score += 15; aspects.append(f"{label} Одна стихия ({e1}) — схожее восприятие")
            elif (e1, e2) in compat_el:
                score += 10; aspects.append(f"{label} Совместимые стихии ({e1} + {e2})")
            else:
                score += 3;  aspects.append(f"{label} Разные стихии — нужно взаимное терпение")

    check_pair(sun1,  sun2,  "☉ Солнце–Солнце:")
    check_pair(moon1, moon2, "☽ Луна–Луна:")
    if asc1 and asc2:
        check_pair(asc1, asc2, "↑ Асц–Асц:")

    if sun1 == moon2 or sun2 == moon1:
        score += 15; aspects.append("✨ Солнце одного совпадает с Луной другого — классическая синастрия")
    if moon1 == asc2 or moon2 == asc1:
        score += 10; aspects.append("🌙 Луна одного совпадает с Асцендентом другого — эмоциональная поддержка")

    # Межкартные аспекты (если есть данные планет)
    p1 = chart1.get('planets', {})
    p2 = chart2.get('planets', {})
    if p1 and p2:
        cross_aspects = calculate_transit_aspects(p1, p2)
        harmonious = sum(1 for a in cross_aspects if 'гармоничный' in a['nature'])
        tense = sum(1 for a in cross_aspects if 'напряжённый' in a['nature'])
        score += harmonious * 3
        score -= tense * 1
        if cross_aspects:
            aspects.append(f"🔗 Межкартных аспектов: {len(cross_aspects)} (гармоничных: {harmonious}, напряжённых: {tense})")

    score = max(0, min(score, 100))
    if score >= 70:   level, emoji = "Высокая",  "💚"
    elif score >= 45: level, emoji = "Хорошая",  "💛"
    elif score >= 25: level, emoji = "Средняя",  "🧡"
    else:             level, emoji = "Сложная",  "❤️‍🔥"

    return {'score': score, 'level': level, 'emoji': emoji, 'aspects': aspects}


def build_compatibility_prompt(chart1: Dict, chart2: Dict,
                                name1: str, name2: str, compat: Dict) -> str:
    lines = [
        "Ты астролог. Дай развёрнутый анализ совместимости пары (западная астрология, синастрия).",
        "",
        f"{name1}:",
        build_natal_summary(chart1),
        "",
        f"{name2}:",
        build_natal_summary(chart2),
        "",
        f"Балл совместимости: {compat['score']}% ({compat['level']})",
        "",
        "Структура ответа:",
        "1. Общая динамика союза",
        "2. Эмоциональная совместимость (Луны)",
        "3. Сексуальное притяжение и энергия",
        "4. Возможные трудности и как их преодолеть",
        "5. Практический совет для пары",
        "",
        "Стиль: честный, психологически глубокий, без лишних общих фраз.",
    ]
    return '\n'.join(lines)
