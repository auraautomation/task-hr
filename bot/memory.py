"""
Har bir foydalanuvchi uchun suhbat tarixini SQLite'da saqlaydi.
Bot qayta ishga tushsa ham (Docker restart) xotira yo'qolmaydi,
chunki data/ papka volume sifatida saqlanadi (docker-compose.yml'ga qarang).
"""
from bot.db import get_conn

MAX_HISTORY = 20


def add_message(user_id: int, role: str, content: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?,?,?)",
            (user_id, role, content),
        )


def get_history(user_id: int, limit: int = MAX_HISTORY) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE user_id=? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    # eskisidan yangisiga qarab qaytaramiz
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def reset_history(user_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
