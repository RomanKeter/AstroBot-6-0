"""
База данных с поддержкой координат, timezone и партнёров.
Добавлен нечёткий поиск партнёров по имени (склонения русских имён).
"""

import sqlite3
import json
import logging
import re
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


def _normalize_name(name: str) -> str:
    """
    Нормализация русского имени: убираем типичные окончания склонений.
    Лена/Лены/Лене/Лену/Леной/Лен → Лен
    Саша/Саши/Саше/Сашу/Сашей/Саш → Саш
    """
    name = name.strip().lower()
    # Убираем типичные русские окончания (от длинных к коротким)
    suffixes = ['ой', 'ей', 'ою', 'ею', 'ам', 'ям', 'ами', 'ями', 'ах', 'ях',
                'ом', 'ем', 'ём', 'ов', 'ев', 'ёв',
                'ы', 'и', 'у', 'ю', 'е', 'ё', 'а', 'я', 'о']
    # Минимальная длина основы — 3 буквы
    for suf in sorted(suffixes, key=len, reverse=True):
        if name.endswith(suf) and len(name) - len(suf) >= 3:
            return name[:-len(suf)]
    return name


def fuzzy_match_name(query: str, stored_name: str) -> bool:
    """
    Нечёткое сравнение имён с учётом русских склонений.
    Проверяет совпадение основ (стемов).
    """
    q_norm = _normalize_name(query)
    s_norm = _normalize_name(stored_name)

    # Точное совпадение нормализованных форм
    if q_norm == s_norm:
        return True

    # Одна основа начинается с другой (для коротких имён)
    if len(q_norm) >= 3 and len(s_norm) >= 3:
        if q_norm.startswith(s_norm) or s_norm.startswith(q_norm):
            return True

    # Точное совпадение оригиналов (case-insensitive)
    if query.strip().lower() == stored_name.strip().lower():
        return True

    return False


class AstroDatabase:
    def __init__(self, db_path: str = "astro_bot.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id     INTEGER PRIMARY KEY,
                        username    TEXT,
                        first_name  TEXT,
                        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        birth_date  TEXT,
                        birth_time  TEXT,
                        city        TEXT,
                        lat         REAL,
                        lon         REAL,
                        timezone    TEXT DEFAULT 'UTC',
                        current_city     TEXT,
                        current_lat      REAL,
                        current_lon      REAL,
                        current_timezone TEXT,
                        chart_json  TEXT
                    )
                """)
                for col, definition in [
                    ("current_city", "TEXT"),
                    ("current_lat", "REAL"),
                    ("current_lon", "REAL"),
                    ("current_timezone", "TEXT"),
                ]:
                    try:
                        conn.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
                    except sqlite3.OperationalError:
                        pass

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS readings (
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id      INTEGER,
                        reading_type TEXT,
                        content      TEXT,
                        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS partners (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id     INTEGER NOT NULL,
                        name        TEXT,
                        birth_date  TEXT,
                        birth_time  TEXT,
                        city        TEXT,
                        lat         REAL,
                        lon         REAL,
                        timezone    TEXT DEFAULT 'UTC',
                        chart_json  TEXT,
                        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
        except Exception as e:
            logger.error(f"DB init error: {e}")
            raise

    # ─── Пользователи ─────────────────────────────────────────────────────────

    def save_user(self, user_id, username, first_name, birth_date, birth_time,
                  city, lat, lon, chart, timezone='UTC'):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO users
                    (user_id, username, first_name, birth_date, birth_time,
                     city, lat, lon, timezone, chart_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    user_id, username, first_name, birth_date, birth_time,
                    city, lat, lon, timezone,
                    json.dumps(chart, ensure_ascii=False)
                ))
            return True
        except Exception as e:
            logger.error(f"Save user error: {e}")
            return False

    def update_current_location(self, user_id, current_city, current_lat,
                                current_lon, current_timezone):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    UPDATE users
                    SET current_city=?, current_lat=?, current_lon=?, current_timezone=?
                    WHERE user_id=?
                """, (current_city, current_lat, current_lon, current_timezone, user_id))
            return True
        except Exception as e:
            logger.error(f"Update location error: {e}")
            return False

    def get_user(self, user_id):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT * FROM users WHERE user_id = ?", (user_id,)
                )
                row = cursor.fetchone()
                if row:
                    result = dict(row)
                    if result.get('chart_json'):
                        result['chart'] = json.loads(result['chart_json'])
                        result['natal_chart'] = result['chart']
                    return result
                return None
        except Exception as e:
            logger.error(f"Get user error: {e}")
            return None

    # ─── Партнёры ──────────────────────────────────────────────────────────────

    def save_partner(self, user_id, name, birth_date, birth_time,
                     city, lat, lon, chart, timezone='UTC'):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""
                    INSERT INTO partners
                    (user_id, name, birth_date, birth_time, city, lat, lon, timezone, chart_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    user_id, name, birth_date, birth_time,
                    city, lat, lon, timezone,
                    json.dumps(chart, ensure_ascii=False)
                ))
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Save partner error: {e}")
            return -1

    def get_partners(self, user_id):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT * FROM partners WHERE user_id = ? ORDER BY created_at DESC",
                    (user_id,)
                )
                results = []
                for row in cursor.fetchall():
                    r = dict(row)
                    if r.get('chart_json'):
                        r['chart'] = json.loads(r['chart_json'])
                    results.append(r)
                return results
        except Exception as e:
            logger.error(f"Get partners error: {e}")
            return []

    def find_partner_by_name(self, user_id: int, name_query: str) -> Optional[Dict]:
        """
        Найти партнёра по имени с учётом склонений русского языка.
        Возвращает первого подходящего партнёра или None.
        """
        partners = self.get_partners(user_id)

        # Сначала пробуем точное совпадение (case-insensitive)
        for p in partners:
            if p.get('name', '').strip().lower() == name_query.strip().lower():
                return p

        # Затем нечёткий поиск
        for p in partners:
            if fuzzy_match_name(name_query, p.get('name', '')):
                return p

        return None

    def find_partners_in_text(self, user_id: int, text: str) -> List[Dict]:
        """
        Найти всех упомянутых партнёров в тексте.
        Возвращает список найденных партнёров.
        """
        partners = self.get_partners(user_id)
        found = []
        words = re.findall(r'[а-яА-ЯёЁa-zA-Z]+', text)

        for word in words:
            if len(word) < 3:
                continue
            for p in partners:
                if p in found:
                    continue
                if fuzzy_match_name(word, p.get('name', '')):
                    found.append(p)

        return found

    def delete_partner(self, partner_id, user_id):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "DELETE FROM partners WHERE id=? AND user_id=?",
                    (partner_id, user_id)
                )
            return True
        except Exception as e:
            logger.error(f"Delete partner error: {e}")
            return False

    # ─── История ───────────────────────────────────────────────────────────────

    def save_reading(self, user_id, reading_type, content):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO readings (user_id, reading_type, content)
                    VALUES (?, ?, ?)
                """, (user_id, reading_type, content))
            return True
        except Exception as e:
            logger.error(f"Save reading error: {e}")
            return False

    def get_readings(self, user_id, limit=5):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT * FROM readings WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                    (user_id, limit)
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Get readings error: {e}")
            return []
