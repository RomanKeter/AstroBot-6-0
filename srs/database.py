"""
База данных с поддержкой координат и полной карты.
"""

import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AstroDatabase:
    def __init__(self, db_path: str = "astro_bot.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
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
                    user_id INTEGER NOT NULL,
                    reading_type TEXT NOT NULL,
                    content TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            """)

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
        timezone: str = "Europe/Minsk",
    ) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO users
                    (user_id, username, first_name, birth_date, birth_time,
                     city, lat, lon, timezone, chart_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id, username, first_name, birth_date, birth_time,
                        city, lat, lon, timezone,
                        json.dumps(chart, ensure_ascii=False),
                    ),
                )
            return True
        except Exception as e:
            logger.error(f"save_user error: {e}")
            return False

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()

            if not row:
                return None

            result = dict(row)
            chart_json = result.pop("chart_json", None)
            if chart_json:
                result["chart"] = json.loads(chart_json)
            else:
                result["chart"] = None
            return result

    def save_reading(self, user_id: int, reading_type: str, content: str) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO readings (user_id, reading_type, content) VALUES (?, ?, ?)",
                    (user_id, reading_type, content),
                )
            return True
        except Exception as e:
            logger.error(f"save_reading error: {e}")
            return False

    def get_readings(
        self, user_id: int, limit: int = 5
    ) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM readings WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
