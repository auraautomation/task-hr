"""
HR agentining tool'lari. Har biri JSON-schema bilan ta'riflangan (TOOL_SCHEMAS)
va haqiqiy bazaga o'qish/yozish amalini bajaradi (faqat "ko'rsatish" emas).

Guardrail: get_employee / get_leave_balance chaqirilganda, agar so'rovchi HR emas
va o'zining employee_id'sidan boshqasini so'rasa - ruxsat berilmaydi.
Bu tekshiruv requesting_user_id orqali amalga oshiriladi (main.py'da uzatiladi).
"""
import os

from dotenv import load_dotenv

load_dotenv()  # bu modul boshqa joydan import qilinganda ham .env kafolatli yuklansin

from bot.db import get_conn
from bot.rag import store as rag_store

HR_USERS = set(int(x) for x in os.getenv("HR_USERS", "").split(",") if x.strip())


def _is_hr(telegram_user_id: int) -> bool:
    return telegram_user_id in HR_USERS


def _resolve_employee_id(telegram_user_id: int) -> int | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT employee_id FROM employees WHERE telegram_user_id=?", (telegram_user_id,)
        ).fetchone()
        return row["employee_id"] if row else None


def get_employee(query: str = "", requesting_user_id: int = 0) -> dict:
    own_id = _resolve_employee_id(requesting_user_id)
    search_term = (query or "").strip()
    if not search_term or search_term.lower() in ["me", "self", "men", "o'zim", "0"]:
        if own_id:
            search_term = str(own_id)
        else:
            return {"error": "Siz hali ro'yxatdan o'tmagansiz. Avval /register <xodim_id> qiling."}

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM employees WHERE full_name LIKE ? OR CAST(employee_id AS TEXT)=?",
            (f"%{search_term}%", search_term),
        ).fetchone()
        if not row:
            return {"error": "Xodim topilmadi"}

        emp_id = int(row["employee_id"])
        # Auto-link Telegram ID if not assigned yet
        if not row["telegram_user_id"] and requesting_user_id:
            conn.execute(
                "UPDATE employees SET telegram_user_id=? WHERE employee_id=?",
                (requesting_user_id, emp_id),
            )

        own_id = _resolve_employee_id(requesting_user_id) or emp_id
        if not _is_hr(requesting_user_id) and own_id != emp_id:
            return {"error": "Ruxsat yo'q: faqat o'z ma'lumotingizni so'rashingiz mumkin"}

    return {
        "employee_id": emp_id,
        "full_name": row["full_name"],
        "department": row["department"],
        "position": row["position"],
        "start_date": row["start_date"],
        # Maosh HECH QACHON qaytarilmaydi - guardrail
    }


def get_leave_balance(employee_id: int | str = 0, requesting_user_id: int = 0) -> dict:
    try:
        emp_id = int(float(employee_id)) if employee_id else 0
    except (ValueError, TypeError):
        emp_id = 0

    own_id = _resolve_employee_id(requesting_user_id)
    if emp_id == 0:
        if own_id:
            emp_id = own_id
        else:
            return {"error": "Siz hali ro'yxatdan o'tmagansiz. Avval /register <xodim_id> qiling."}

    if not own_id and emp_id:
        own_id = emp_id  # fallback to requested emp_id if auto-bound

    if not _is_hr(requesting_user_id) and own_id and own_id != emp_id:
        return {"error": "Ruxsat yo'q: faqat o'z ta'til balansingizni so'rashingiz mumkin"}

    with get_conn() as conn:
        row = conn.execute(
            "SELECT leave_balance, full_name FROM employees WHERE employee_id=?", (emp_id,)
        ).fetchone()
    if not row:
        return {"error": "Xodim topilmadi"}
    return {"employee_id": emp_id, "full_name": row["full_name"], "leave_balance": int(row["leave_balance"])}


def create_leave_request(employee_id: int | str = 0, date_from: str = "", date_to: str = "", leave_type: str = "", requesting_user_id: int = 0) -> dict:
    """Bu tool chaqirilishidan oldin Telegram'da inline tugma bilan tasdiqlanadi (main.py)."""
    try:
        emp_id = int(float(employee_id)) if employee_id else 0
    except (ValueError, TypeError):
        emp_id = 0

    own_id = _resolve_employee_id(requesting_user_id) if requesting_user_id else None
    if emp_id == 0:
        if own_id:
            emp_id = own_id
        else:
            return {"error": "Siz hali ro'yxatdan o'tmagansiz. Avval /register <xodim_id> qiling."}

    if requesting_user_id and not _is_hr(requesting_user_id) and own_id and own_id != emp_id:
        return {"error": "Ruxsat yo'q: Siz faqat o'zingiz uchun ta'til so'rovi yarata olasiz"}

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO leave_requests (employee_id, date_from, date_to, leave_type, status) "
            "VALUES (?,?,?,?, 'pending')",
            (emp_id, date_from, date_to, leave_type),
        )
        request_id = cur.lastrowid
    return {"request_id": request_id, "employee_id": emp_id, "status": "pending", "message": "So'rov HR'ga yuborildi"}


def list_pending_requests(requesting_user_id: int) -> dict:
    if not _is_hr(requesting_user_id):
        return {"error": "Ruxsat yo'q: bu faqat HR uchun"}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT lr.id, e.full_name, lr.date_from, lr.date_to, lr.leave_type "
            "FROM leave_requests lr JOIN employees e ON e.employee_id = lr.employee_id "
            "WHERE lr.status='pending' ORDER BY lr.created_at"
        ).fetchall()
    return {"pending": [dict(r) for r in rows]}


def search_policy(query: str) -> dict:
    results = rag_store.search(query, top_k=3)
    if not results:
        return {"found": False, "message": "Hujjatlarda javob topilmadi"}
    return {"found": True, "chunks": results}


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_employee",
            "description": "Ism yoki ID bo'yicha xodim kartasini (lavozim, bo'lim, ish boshlagan sana) qaytaradi. Ta'til balansi so'ralganda ishlatilmaydi!",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Ism, employee_id yoki bo'sh"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_leave_balance",
            "description": "Xodimning qolgan ta'til kunlarini (ta'til balansini) qaytaradi. Ta'til balansi haqida har qanday savol berilganda FAQAT SHU TOOL chaqirilishi SHART!",
            "parameters": {
                "type": "object",
                "properties": {"employee_id": {"type": "integer", "description": "Xodim ID raqami (o'zi uchun 0 yoki ko'rsatilmaydi)"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_leave_request",
            "description": "Yangi ta'til so'rovi yaratadi. Foydalanuvchi ta'til so'ramoqchi bo'lganda (sana va turini aytsa) DARHOL shu tool chaqiriladi. employee_id ko'rsatilmasa 0 qo'yiladi.",
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_id": {"type": "integer", "description": "Xodim ID raqami (o'zi uchun 0 yoki ko'rsatilmaydi)"},
                    "date_from": {"type": "string", "description": "YYYY-MM-DD formatida ta'til boshlanish sanasi (masalan 2026-09-20)"},
                    "date_to": {"type": "string", "description": "YYYY-MM-DD formatida ta'til tugash sanasi (masalan 2026-09-25)"},
                    "leave_type": {"type": "string", "enum": ["yillik", "kasallik", "ta'tilsiz"], "description": "Ta'til turi"},
                },
                "required": ["date_from", "date_to", "leave_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_pending_requests",
            "description": "HR uchun: barcha kutayotgan ta'til so'rovlari ro'yxati",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_policy",
            "description": "Ichki HR qoidalari va reglamentlaridan savolga javob qidiradi (RAG)",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
]

DISPATCH = {
    "get_employee": get_employee,
    "get_leave_balance": get_leave_balance,
    "create_leave_request": create_leave_request,
    "list_pending_requests": list_pending_requests,
    "search_policy": search_policy,
}

# Tasdiqlash talab qiladigan tool'lar - main.py bularni to'g'ridan-to'g'ri
# chaqirmasdan, avval inline tugma orqali foydalanuvchidan "Ha" javobini kutadi.
REQUIRES_CONFIRMATION = {"create_leave_request"}
