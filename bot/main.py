"""
HR Agent - Telegram bot kirish nuqtasi.

Ishga tushirish:
    python -m bot.main

Talab qilinadigan .env o'zgaruvchilari uchun .env.example'ga qarang.
"""
import asyncio
import inspect
import json
import logging
import os
import uuid

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

from bot.db import init_db
from bot.llm import LLMError, call_llm
from bot.memory import add_message, get_history, reset_history
from bot.rag import store as rag_store
from bot.tools import DISPATCH, REQUIRES_CONFIRMATION, TOOL_SCHEMAS, get_conn

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("hr_agent")

ADMIN_USERS = set(int(x) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip())
HR_USERS = set(int(x) for x in os.getenv("HR_USERS", "").split(",") if x.strip())
TELEGRAM_MAX_LEN = 4000
MAX_STEPS = 6  # cheksiz loop'dan himoya
REGISTRATION_OPEN = os.getenv("REGISTRATION_OPEN", "true").lower() == "true"

SYSTEM_PROMPT = """Sen kompaniyaning HR bo'limi uchun ichki yordamchi agentsan.

Vazifang:
- Xodimlarga ta'til balansi, ta'til so'rovi, ichki qoidalar bo'yicha yordam berish
- HR xodimlariga kutayotgan so'rovlarni ko'rsatish

Qoidalar (buzilmasin):
1. Hech qachon ma'lumot to'qima. Agar tool orqali ma'lumot topilmasa - "bilmayman, HR bilan bog'laning" de.
2. Xodimning maoshi haqida hech qachon aniq raqam aytma - bu maxfiy.
3. Xodim faqat o'z ma'lumotini so'rashi mumkin, HR xodimlari hammani ko'ra oladi - buni tool o'zi tekshiradi.
4. Ta'til balansi so'ralganda ("ta'til balansim qancha", "nechta ta'tilim qoldi") -> ALBATTA `get_leave_balance` tool'ini chaqir! `get_employee` chaqirma!
5. Ta'til so'rovi berilganda (masalan "20-25 sentabrga yillik ta'til so'ramoqchiman") -> ALBATTA `create_leave_request(date_from="2026-09-20", date_to="2026-09-25", leave_type="yillik")` tool'ini chaqir!
6. Qoidalar va reglamentlar so'ralganda -> `search_policy` tool'ini chaqir!
7. Javoblaring qisqa, aniq va do'stona bo'lsin. O'zbek tilida javob ber.
"""


def split_message(text: str) -> list[str]:
    if len(text) <= TELEGRAM_MAX_LEN:
        return [text]
    parts = []
    while text:
        parts.append(text[:TELEGRAM_MAX_LEN])
        text = text[TELEGRAM_MAX_LEN:]
    return parts


async def run_agent(user_id: int, user_text: str, bot: Bot, chat_id: int) -> str | None:
    """
    Asosiy agent loop. Tasdiqlash talab qiladigan tool chaqirilsa,
    None qaytaradi va o'rniga inline tugma yuboradi (bajarish keyinchalik amalga oshadi).
    """
    add_message(user_id, "user", user_text)
    history = get_history(user_id)

    # Foydalanuvchi ma'lumotlarini bazadan aniqlaymiz
    user_context = ""
    with get_conn() as conn:
        emp = conn.execute(
            "SELECT employee_id, full_name, department, position FROM employees WHERE telegram_user_id=?",
            (user_id,),
        ).fetchone()

    if emp:
        user_context = (
            f"\n\n[Hozirgi foydalanuvchi ma'lumoti]:\n"
            f"- Telegram ID: {user_id}\n"
            f"- Xodim ID: {emp['employee_id']}\n"
            f"- Ism: {emp['full_name']}\n"
            f"- Bo'lim: {emp['department']}\n"
            f"- Lavozim: {emp['position']}\n"
            f"DIQQAT: Foydalanuvchi \"men\", \"mening ta'tilim\", \"men haqimda\" deb so'raganda, "
            f"uning employee_id={emp['employee_id']} ekanligi SIZGA ANIQLANISHI SHART! "
            f"Undan ISMINI YOKI ID'SINI QAYTA SO'RAMANG, darhol employee_id={emp['employee_id']} bilan mos tool'ni chaqiring!"
        )
    else:
        user_context = f"\n\n[Hozirgi foydalanuvchi]: Telegram ID: {user_id} (hali bazadagi xodimga bog'lanmagan)."

    messages = [{"role": "system", "content": SYSTEM_PROMPT + user_context}] + history

    for step in range(MAX_STEPS):
        try:
            result = await call_llm(messages, tools=TOOL_SCHEMAS)
        except LLMError as e:
            log.error(f"LLM butunlay javob bermadi: {e}")
            return "Kechirasiz, hozir AI xizmati javob bermayapti. Birozdan so'ng qayta urinib ko'ring."

        if not result["tool_calls"]:
            final_text = result["text"] or "Kechirasiz, javob shakllantira olmadim."
            add_message(user_id, "assistant", final_text)
            return final_text

        # Tool chaqiruvlarini bajaramiz
        messages.append({"role": "assistant", "content": result["text"] or ""})
        for tc in result["tool_calls"]:
            name, args = tc["name"], tc["arguments"]

            if name in REQUIRES_CONFIRMATION:
                await ask_confirmation(bot, chat_id, user_id, name, args)
                return None  # bajarish confirm_callback'da davom etadi

            fn = DISPATCH.get(name)
            if fn is None:
                tool_result = {"error": f"Noma'lum tool: {name}"}
            else:
                sig = inspect.signature(fn)
                if "requesting_user_id" in sig.parameters:
                    args = {**args, "requesting_user_id": user_id}
                try:
                    tool_result = fn(**args)
                except Exception as e:
                    tool_result = {"error": f"Tool xatosi: {e}"}

            log_tool_call(user_id, name, args, tool_result)
            messages.append(
                {"role": "tool", "content": json.dumps(tool_result, ensure_ascii=False)}
            )

    return "Kechirasiz, so'rovni bajarish uchun juda ko'p qadam kerak bo'ldi. Iltimos, savolni soddalashtirib qayta yozing."


def log_tool_call(user_id: int, name: str, args: dict, result: dict):
    log.info(f"[tool] user={user_id} {name}({args}) -> {result}")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO tool_logs (user_id, tool_name, arguments, result) VALUES (?,?,?,?)",
            (user_id, name, json.dumps(args, ensure_ascii=False), json.dumps(result, ensure_ascii=False)),
        )


# Tasdiqlash kutilayotgan amallar vaqtinchalik shu yerda saqlanadi (jarayon xotirasida)
PENDING_CONFIRMATIONS: dict[str, dict] = {}


async def ask_confirmation(bot: Bot, chat_id: int, user_id: int, tool_name: str, args: dict):
    token = str(uuid.uuid4())[:8]
    PENDING_CONFIRMATIONS[token] = {"tool_name": tool_name, "args": args, "user_id": user_id}
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Ha, yubor", callback_data=f"confirm:{token}"),
                InlineKeyboardButton(text="❌ Bekor qil", callback_data=f"cancel:{token}"),
            ]
        ]
    )
    text = (
        f"Quyidagi amalni tasdiqlaysizmi?\n\n<b>{tool_name}</b>\n"
        + "\n".join(f"• {k}: {v}" for k, v in args.items())
    )
    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")


async def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN .env faylida topilmadi")

    init_db()
    rag_store.build()

    bot = Bot(token=token)
    dp = Dispatcher()

    def is_allowed(user_id: int) -> bool:
        """Admin yoki bazada telegram_user_id bog'langan har qanday xodimga ruxsat."""
        if user_id in ADMIN_USERS or user_id in HR_USERS:
            return True
        with get_conn() as conn:
            row = conn.execute(
                "SELECT employee_id FROM employees WHERE telegram_user_id=?", (user_id,)
            ).fetchone()
        return row is not None

    @dp.message(Command("start"))
    async def start_handler(message: Message):
        uid = message.from_user.id
        if is_allowed(uid):
            await message.answer(
                "Salom! Men HR bo'limining yordamchi agentiman.\n\n"
                "Men bunday narsalarda yordam bera olaman:\n"
                "• \"Mening ta'til balansim qancha?\"\n"
                "• \"15-20 sentabrga yillik ta'til so'ramoqchiman\"\n"
                "• \"Masofadan ishlash qoidasi qanday?\"\n"
                "• (HR uchun) \"Kutayotgan so'rovlarni ko'rsat\"\n\n"
                "Buyruqlar: /help, /register, /myid, /reset"
            )
        else:
            await message.answer(
                "Salom! Men HR bo'limining yordamchi agentiman.\n\n"
                "Botdan foydalanish uchun avval ro'yxatdan o'ting:\n"
                "/register <xodim_id> — Masalan: /register 15\n\n"
                "Xodim ID'ingizni HR bo'limidan so'rang."
            )

    @dp.message(Command("help"))
    async def help_handler(message: Message):
        if not is_allowed(message.from_user.id):
            await message.answer(
                "Botdan foydalanish uchun avval ro'yxatdan o'ting:\n"
                "/register <xodim_id> — Masalan: /register 15"
            )
            return
        is_hr = message.from_user.id in HR_USERS or message.from_user.id in ADMIN_USERS
        text = (
            "📖 <b>Buyruqlar ro'yxati:</b>\n\n"
            "/start — Botni tanishtirish\n"
            "/register <xodim_id> — Ro'yxatdan o'tish\n"
            "/myid — O'z ma'lumotlaringiz\n"
            "/reset — Suhbat xotirasini tozalash\n"
            "/help — Shu xabar\n"
        )
        if is_hr:
            text += (
                "\n<b>🔐 HR/Admin buyruqlari:</b>\n"
                "/admin_list — Barcha xodimlar ro'yxati\n"
                "/admin_bind <xodim_id> <tg_id> — Xodimni bog'lash\n"
            )
        text += "\n💬 Oddiy tilda savolingizni yozing, men tushunaman."
        await message.answer(text, parse_mode="HTML")

    @dp.message(Command("reset"))
    async def reset_handler(message: Message):
        if not is_allowed(message.from_user.id):
            await message.answer("Avval /register bilan ro'yxatdan o'ting.")
            return
        reset_history(message.from_user.id)
        await message.answer("Suhbat xotirasi tozalandi.")

    @dp.message(Command("register"))
    async def register_handler(message: Message):
        """Xodim o'zini ro'yxatdan o'tkazadi: /register <employee_id>"""
        uid = message.from_user.id

        # Allaqachon ro'yxatdan o'tganmi?
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT employee_id, full_name FROM employees WHERE telegram_user_id=?", (uid,)
            ).fetchone()
        if existing:
            await message.answer(
                f"Siz allaqachon ro'yxatdan o'tgansiz!\n"
                f"Xodim ID: {existing['employee_id']}\n"
                f"Ism: {existing['full_name']}"
            )
            return

        if not REGISTRATION_OPEN and uid not in ADMIN_USERS:
            await message.answer("Ro'yxatdan o'tish hozirda yopiq. HR bilan bog'laning.")
            return

        parts = message.text.split()
        if len(parts) < 2:
            await message.answer(
                "Foydalanish: /register <xodim_id>\n"
                "Masalan: /register 15\n\n"
                "Xodim ID'ingizni HR bo'limidan so'rang."
            )
            return

        try:
            emp_id = int(parts[1])
        except ValueError:
            await message.answer("Xodim ID raqam bo'lishi kerak. Masalan: /register 15")
            return

        with get_conn() as conn:
            emp = conn.execute(
                "SELECT employee_id, full_name, telegram_user_id FROM employees WHERE employee_id=?",
                (emp_id,)
            ).fetchone()

        if not emp:
            await message.answer(f"#{emp_id} raqamli xodim bazada topilmadi. ID'ni tekshiring.")
            return

        if emp["telegram_user_id"] and emp["telegram_user_id"] != uid:
            await message.answer(
                "Bu xodim ID'si boshqa Telegram akkauntga bog'langan.\n"
                "Agar xato bo'lsa, HR bilan bog'laning."
            )
            return

        # Telegram ID'ni bog'laymiz
        with get_conn() as conn:
            conn.execute(
                "UPDATE employees SET telegram_user_id=? WHERE employee_id=?",
                (uid, emp_id),
            )

        await message.answer(
            f"✅ Muvaffaqiyatli ro'yxatdan o'tdingiz!\n\n"
            f"👤 Ism: {emp['full_name']}\n"
            f"🆔 Xodim ID: {emp_id}\n"
            f"📱 Telegram ID: {uid}\n\n"
            "Endi menga savol berishingiz mumkin!"
        )
        log.info(f"[register] user_id={uid} -> employee_id={emp_id} ({emp['full_name']})")

    @dp.message(Command("myid"))
    async def myid_handler(message: Message):
        """Foydalanuvchiga o'z Telegram ID va xodim ID'sini ko'rsatadi."""
        uid = message.from_user.id
        with get_conn() as conn:
            row = conn.execute(
                "SELECT employee_id, full_name, department, position FROM employees WHERE telegram_user_id=?",
                (uid,)
            ).fetchone()

        if row:
            await message.answer(
                f"📋 Sizning ma'lumotlaringiz:\n\n"
                f"🆔 Telegram ID: {uid}\n"
                f"👤 Ism: {row['full_name']}\n"
                f"🏢 Bo'lim: {row['department']}\n"
                f"💼 Lavozim: {row['position']}\n"
                f"📊 Xodim ID: {row['employee_id']}"
            )
        else:
            await message.answer(
                f"🆔 Sizning Telegram ID: {uid}\n\n"
                "Siz hali ro'yxatdan o'tmagansiz.\n"
                "Ro'yxatdan o'tish: /register <xodim_id>"
            )

    @dp.message(Command("admin_bind"))
    async def admin_bind_handler(message: Message):
        """HR/Admin uchun: Xodimni Telegram ID'ga bog'lash.\n/admin_bind <employee_id> <telegram_user_id>"""
        uid = message.from_user.id
        if uid not in HR_USERS and uid not in ADMIN_USERS:
            await message.answer("Bu buyruq faqat HR/Admin uchun.")
            return

        parts = message.text.split()
        if len(parts) < 3:
            await message.answer(
                "Foydalanish: /admin_bind <xodim_id> <telegram_user_id>\n"
                "Masalan: /admin_bind 15 123456789"
            )
            return

        try:
            emp_id = int(parts[1])
            tg_id = int(parts[2])
        except ValueError:
            await message.answer("Xodim ID va Telegram ID raqam bo'lishi kerak.")
            return

        with get_conn() as conn:
            emp = conn.execute(
                "SELECT full_name FROM employees WHERE employee_id=?", (emp_id,)
            ).fetchone()
            if not emp:
                await message.answer(f"#{emp_id} raqamli xodim topilmadi.")
                return
            conn.execute(
                "UPDATE employees SET telegram_user_id=? WHERE employee_id=?",
                (tg_id, emp_id),
            )

        await message.answer(
            f"✅ Bog'landi: {emp['full_name']} (#{emp_id}) -> Telegram ID: {tg_id}"
        )
        log.info(f"[admin_bind] {uid} linked emp={emp_id} to tg={tg_id}")

    @dp.message(Command("admin_list"))
    async def admin_list_handler(message: Message):
        """HR/Admin uchun: Barcha xodimlar ro'yxati va ularning Telegram bog'lanish holati."""
        uid = message.from_user.id
        if uid not in HR_USERS and uid not in ADMIN_USERS:
            await message.answer("Bu buyruq faqat HR/Admin uchun.")
            return

        with get_conn() as conn:
            rows = conn.execute(
                "SELECT employee_id, full_name, department, telegram_user_id FROM employees ORDER BY employee_id"
            ).fetchall()

        lines = ["📋 <b>Xodimlar ro'yxati:</b>\n"]
        registered = 0
        for r in rows:
            status = "✅" if r["telegram_user_id"] else "⬜"
            if r["telegram_user_id"]:
                registered += 1
            lines.append(f"{status} #{r['employee_id']} {r['full_name']} ({r['department']})")

        lines.append(f"\n📊 Jami: {len(rows)} xodim | Ro'yxatdan o'tgan: {registered}")
        text = "\n".join(lines)

        for part in split_message(text):
            await message.answer(part, parse_mode="HTML")

    @dp.message(F.text)
    async def text_handler(message: Message):
        user_id = message.from_user.id
        if not is_allowed(user_id):
            await message.answer(
                "Siz hali ro'yxatdan o'tmagansiz.\n"
                "Avval /register <xodim_id> buyrug'ini yuboring.\n"
                "Masalan: /register 15"
            )
            log.warning(f"Ro'yxatdan o'tmagan foydalanuvchi: user_id={user_id}")
            return

        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        try:
            reply = await run_agent(user_id, message.text, bot, message.chat.id)
        except Exception as e:
            log.exception("Kutilmagan xato")
            reply = "Kechirasiz, kutilmagan xatolik yuz berdi. Administratorga xabar berildi."
        if reply:
            for part in split_message(reply):
                await message.answer(part)

    @dp.callback_query(F.data.startswith("confirm:"))
    async def confirm_handler(callback: CallbackQuery):
        token = callback.data.split(":", 1)[1]
        pending = PENDING_CONFIRMATIONS.pop(token, None)
        if not pending:
            await callback.answer("Bu so'rov muddati o'tgan.")
            return

        user_id = pending["user_id"]
        tool_name = pending["tool_name"]
        args = pending["args"]

        fn = DISPATCH[tool_name]
        sig = inspect.signature(fn)
        if "requesting_user_id" in sig.parameters:
            args = {**args, "requesting_user_id": user_id}

        try:
            result = fn(**args)
        except Exception as e:
            result = {"error": str(e)}

        log_tool_call(user_id, tool_name, args, result)

        if "error" in result:
            await callback.message.edit_text(f"❌ Xatolik: {result['error']}")
        else:
            add_message(user_id, "assistant", f"Ta'til so'rovi muvaffaqiyatli yaratildi. Request ID: {result.get('request_id')}")
            await callback.message.edit_text(
                "✅ <b>Ta'til so'rovi muvaffaqiyatli yaratildi va HR bo'limiga yuborildi!</b>\n\n"
                f"So'rov ID: #{result.get('request_id', '-')}\n"
                f"Holati: Kutilmoqda (pending)",
                parse_mode="HTML",
            )
            req_id = result.get("request_id")
            emp_id = int(float(pending["args"].get("employee_id", 0)))
            hr_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"hr_approve:{req_id}"),
                        InlineKeyboardButton(text="❌ Rad etish", callback_data=f"hr_reject:{req_id}"),
                    ]
                ]
            )
            # HR foydalanuvchilariga avtomatik notification
            for hr_user_id in HR_USERS:
                try:
                    await bot.send_message(
                        hr_user_id,
                        f"🔔 <b>Yangi ta'til so'rovi kelib tushdi!</b>\n\n"
                        f"• Xodim ID: {emp_id}\n"
                        f"• Sana: {pending['args'].get('date_from')} — {pending['args'].get('date_to')}\n"
                        f"• Turi: {pending['args'].get('leave_type')}\n"
                        f"• So'rov ID: #{req_id}",
                        reply_markup=hr_kb,
                        parse_mode="HTML",
                    )
                except Exception as ex:
                    log.warning(f"HR {hr_user_id} ga bildirishnoma yuborib bo'lmadi: {ex}")

        await callback.answer()

    @dp.callback_query(F.data.startswith("hr_approve:"))
    async def hr_approve_handler(callback: CallbackQuery):
        if HR_USERS and callback.from_user.id not in HR_USERS:
            await callback.answer("Ruxsat yo'q: Siz HR emassiz.", show_alert=True)
            return
        req_id = int(callback.data.split(":", 1)[1])
        with get_conn() as conn:
            conn.execute("UPDATE leave_requests SET status='approved' WHERE id=?", (req_id,))
            req = conn.execute("SELECT employee_id FROM leave_requests WHERE id=?", (req_id,)).fetchone()
            emp = conn.execute("SELECT telegram_user_id FROM employees WHERE employee_id=?", (req["employee_id"],)).fetchone() if req else None

        await callback.message.edit_text(
            f"✅ <b>Ta'til so'rovi #{req_id} HR tomonidan TASDIQLANDI!</b>",
            parse_mode="HTML"
        )
        await callback.answer("Ta'til so'rovi tasdiqlandi!")

        if emp and emp["telegram_user_id"]:
            try:
                await bot.send_message(
                    emp["telegram_user_id"],
                    f"🎉 <b>Xushxabar! #{req_id}-sonli ta'til so'rovingiz HR tomonidan TASDIQLANDI.</b>",
                    parse_mode="HTML"
                )
            except Exception:
                pass

    @dp.callback_query(F.data.startswith("hr_reject:"))
    async def hr_reject_handler(callback: CallbackQuery):
        if HR_USERS and callback.from_user.id not in HR_USERS:
            await callback.answer("Ruxsat yo'q: Siz HR emassiz.", show_alert=True)
            return
        req_id = int(callback.data.split(":", 1)[1])
        with get_conn() as conn:
            conn.execute("UPDATE leave_requests SET status='rejected' WHERE id=?", (req_id,))
            req = conn.execute("SELECT employee_id FROM leave_requests WHERE id=?", (req_id,)).fetchone()
            emp = conn.execute("SELECT telegram_user_id FROM employees WHERE employee_id=?", (req["employee_id"],)).fetchone() if req else None

        await callback.message.edit_text(
            f"❌ <b>Ta'til so'rovi #{req_id} HR tomonidan RAD ETILDI.</b>",
            parse_mode="HTML"
        )
        await callback.answer("Ta'til so'rovi rad etildi.")

        if emp and emp["telegram_user_id"]:
            try:
                await bot.send_message(
                    emp["telegram_user_id"],
                    f"❌ <b>#{req_id}-sonli ta'til so'rovingiz HR tomonidan RAD ETILDI.</b>",
                    parse_mode="HTML"
                )
            except Exception:
                pass

    @dp.callback_query(F.data.startswith("cancel:"))
    async def cancel_handler(callback: CallbackQuery):
        token = callback.data.split(":", 1)[1]
        PENDING_CONFIRMATIONS.pop(token, None)
        await callback.message.edit_text("❌ Bekor qilindi.")
        await callback.answer()

    log.info("HR agent ishga tushdi (polling)")
    log.info(f"Admin foydalanuvchilar: {ADMIN_USERS}")
    log.info(f"HR foydalanuvchilar: {HR_USERS}")
    log.info(f"Ro'yxatdan o'tish: {'ochiq' if REGISTRATION_OPEN else 'yopiq'}")

    # Render Web Service uchun port va healthcheck tinglovchisi
    try:
        from aiohttp import web
        app = web.Application()
        app.router.add_get("/", lambda r: web.Response(text="HR Agent Bot is running!"))
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.getenv("PORT", "8000"))
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        log.info(f"Render healthcheck veb-serveri {port}-portda ishga tushdi")
    except Exception as ex:
        log.warning(f"Veb-serverni ishga tushirib bo'lmadi: {ex}")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
