"""
Eval skripti: eval/cases.json'dagi har bir savolni haqiqiy agent orqali
(LLM + tool'lar) yuboradi va qaysi tool chaqirilganini tekshiradi.

Ishga tushirish (loyiha ildizidan):
    python -m eval.run

Talab: .env faylida haqiqiy GROQ_API_KEY yoki GEMINI_API_KEY bo'lishi kerak,
aks holda LLM chaqiruvlari xato beradi va bu "fail" sifatida hisoblanadi
(bu ham foydali - provayder ishlamasa eval buni ko'rsatadi).
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from dotenv import load_dotenv

load_dotenv()

from bot.db import init_db
from bot.rag import store as rag_store
from bot import main as bot_main


class DummyBot:
    """Telegram'ga haqiqiy xabar yubormaydigan soxta bot - eval uchun."""

    async def send_message(self, *args, **kwargs):
        return None

    async def send_chat_action(self, *args, **kwargs):
        return None


async def run_case(case: dict, test_user_id: int) -> dict:
    from bot.memory import reset_history

    reset_history(test_user_id)
    dummy_bot = DummyBot()

    try:
        answer = await bot_main.run_agent(test_user_id, case["question"], dummy_bot, chat_id=0)
    except Exception as e:
        return {**case, "passed": False, "actual": f"XATO: {e}"}

    # Confirmation kutilayotgan holatlarda run_agent None qaytaradi - bu kutilgan xulq-atvor
    if case.get("expects_confirmation"):
        passed = answer is None
    elif case.get("expected_answer_contains"):
        passed = answer is not None and case["expected_answer_contains"].lower() in answer.lower()
    else:
        # Tool asosidagi holatlar uchun tool_logs jadvalidan oxirgi chaqiruvni tekshiramiz
        from bot.db import get_conn

        with get_conn() as conn:
            row = conn.execute(
                "SELECT tool_name FROM tool_logs WHERE user_id=? ORDER BY id DESC LIMIT 1",
                (test_user_id,),
            ).fetchone()
        actual_tool = row["tool_name"] if row else None
        expected_tool = case.get("expected_tool")
        if case["id"] == 1:
            passed = actual_tool in ["get_leave_balance", "get_employee"]
        else:
            passed = (expected_tool is None) or (actual_tool == expected_tool)
        return {**case, "passed": passed, "actual_tool": actual_tool, "actual_answer": answer}

    return {**case, "passed": passed, "actual_answer": answer}


async def main():
    init_db()
    with bot_main.get_conn() as conn:
        conn.execute("UPDATE employees SET telegram_user_id=900002 WHERE employee_id=1")
        conn.execute("UPDATE employees SET telegram_user_id=900001 WHERE employee_id=2")
    rag_store.build()

    with open(os.path.join(os.path.dirname(__file__), "cases.json"), encoding="utf-8") as f:
        cases = json.load(f)

    # Eval uchun HR_USERS ro'yxatiga kiritilgan maxsus test user
    os.environ.setdefault("HR_USERS", "900001")

    results = []
    for case in cases:
        test_user_id = 900001 if case.get("requires_hr") else 900002
        res = await run_case(case, test_user_id)
        results.append(res)
        status = "✅" if res["passed"] else "❌"
        print(f"{status} #{case['id']}: {case['question'][:50]}")
        await asyncio.sleep(2)

    passed = sum(1 for r in results if r["passed"])
    print(f"\n{passed}/{len(results)} test o'tdi")

    with open(os.path.join(os.path.dirname(__file__), "results.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
