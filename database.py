"""
База данных с поддержкой координат и полной карты.
"""

import sqlite3
import json
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


class AstroDatabase:
    def __init__(self, db_path: str = "astro_bot.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        username TEXT,
                        first_name TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        birth_date TEXT,
                        birth_time TEXT,
                        city TEXT,
                        lat REAL,
                        lon REAL,
                        timezone TEXT DEFAULT 'Europe/Minsk',
                        chart_json TEXT
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS readings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        reading_type TEXT,
                        content TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
        except Exception as e:
            logger.error(f"DB init error: {e}")
            raise

    def save_user(
        self,
        user_id: int,
        username: Optional[str],
        first_name: Optional[str],
        birth_date: str,
        birth_time: Optional[str],
        city: str,
        lat: float,
        lon: float,
        chart: Dict,
        timezone: str = 'Europe/Minsk'
    ) -> bool:
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

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT * FROM users WHERE user_id = ?",
                    (user_id,)
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

    def save_reading(self, user_id: int, reading_type: str, content: str) -> bool:
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

    def get_readings(self, user_id: int, limit: int = 5) -> List[Dict]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT * FROM readings WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                    (user_id, limit)
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Get readings error: {e}")
            return []
