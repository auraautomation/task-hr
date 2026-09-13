"""
SQLite bazasini yaratish va boshlang'ich ma'lumot (employees.csv) bilan to'ldirish.
Bot birinchi marta ishga tushganda init_db() chaqiriladi - jadval bo'lmasa yaratadi,
employees jadvali bo'sh bo'lsa CSV'dan yuklaydi. Qayta ishga tushganda hech narsani
qayta yozmaydi - shu sababli xotira/ma'lumot yo'qolmaydi.
"""
import csv
import os
import sqlite3
from contextlib import contextmanager

from dotenv import load_dotenv

load_dotenv()  # bu modul boshqa joydan import qilinganda ham .env kafolatli yuklansin

DB_PATH = os.getenv("DB_PATH", "data/hr_agent.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    employee_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    department TEXT,
    position TEXT,
    start_date TEXT,
    leave_balance INTEGER DEFAULT 0,
    phone TEXT,
    telegram_user_id INTEGER
);

CREATE TABLE IF NOT EXISTS leave_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    leave_type TEXT NOT NULL,
    status TEXT DEFAULT 'pending',       -- pending / approved / rejected
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (employee_id) REFERENCES employees(employee_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,                  -- user / assistant / tool
    content TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tool_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    tool_name TEXT,
    arguments TEXT,
    result TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(csv_path: str = "data/employees.csv"):
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        count = conn.execute("SELECT COUNT(*) c FROM employees").fetchone()["c"]
        if count == 0 and os.path.exists(csv_path):
            with open(csv_path, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = [
                    (
                        int(r["employee_id"]),
                        r["full_name"],
                        r["department"],
                        r["position"],
                        r["start_date"],
                        int(r["leave_balance"]),
                        r["phone"],
                    )
                    for r in reader
                ]
            conn.executemany(
                "INSERT INTO employees (employee_id, full_name, department, position, "
                "start_date, leave_balance, phone) VALUES (?,?,?,?,?,?,?)",
                rows,
            )
            print(f"[db] {len(rows)} ta xodim yuklandi")
        allowed = [int(x) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()]
        if allowed:
            conn.execute(
                "UPDATE employees SET telegram_user_id=? WHERE employee_id=1 AND telegram_user_id IS NULL",
                (allowed[0],),
            )


if __name__ == "__main__":
    init_db()
    print("[db] baza tayyor:", DB_PATH)
