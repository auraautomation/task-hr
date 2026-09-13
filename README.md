# HR Agent — Telegram bot

Kompaniya HR bo'limi uchun AI agent. Xodimlarga ta'til balansi, ta'til so'rovi,
ichki qoidalar bo'yicha yordam beradi; HR xodimlariga kutayotgan so'rovlarni ko'rsatadi.

## Arxitektura

```
Telegram xabar
      │
      ▼
 aiogram handler (auth tekshiruvi: ALLOWED_USERS)
      │
      ▼
 run_agent() — agent loop (max 6 qadam)
      │
      ├─► LLM chaqiruvi (bot/llm.py)
      │     Asosiy: Groq (llama-3.3-70b) → xato bo'lsa Gemini'ga fallback
      │     Har urinishda exponential backoff (429 / timeout uchun)
      │
      ├─► Tool chaqirilsa (bot/tools.py):
      │     get_employee / get_leave_balance / create_leave_request /
      │     list_pending_requests / search_policy
      │     → SQLite'ga o'qiydi/yozadi, natija LLM'ga qaytariladi
      │
      ├─► create_leave_request kabi xavfli amal — avval Telegram inline
      │     tugma bilan tasdiqlash so'raladi (bot/main.py: ask_confirmation)
      │
      └─► Yakuniy javob foydalanuvchiga (4096 belgidan uzun bo'lsa bo'lib yuboriladi)

Xotira: SQLite (data/hr_agent.db), har user uchun oxirgi 20 xabar — Docker
restart'dan keyin ham saqlanadi (volume orqali).

RAG: data/docs/*.md hujjatlar bo'laklarga bo'linadi, Gemini embedding
(models/text-embedding-004) bilan vektorlashtiriladi va cosine similarity
bo'yicha top-3 bo'lak topiladi. GEMINI_API_KEY bo'lmasa — oddiy bag-of-words
fallback ishlaydi (sifat pastroq, lekin tizim ishlashda davom etadi).
```

## Tool'lar

| Tool | Nima qiladi | Ruxsat |
|---|---|---|
| `get_employee(query)` | Ism/ID bo'yicha xodim kartasi (maosh qaytarilmaydi) | Xodim — faqat o'zini, HR — hammani |
| `get_leave_balance(employee_id)` | Qolgan ta'til kunlari | Xodim — faqat o'zini, HR — hammani |
| `create_leave_request(...)` | Ta'til so'rovi yaratadi | Har kim, lekin **inline tugma bilan tasdiqlash talab qilinadi** |
| `list_pending_requests()` | Kutayotgan so'rovlar ro'yxati | Faqat HR (`HR_USERS`) |
| `search_policy(query)` | RAG orqali ichki hujjatlardan javob | Hammaga ochiq |

## O'rnatish (lokal)

```bash
git clone <repo-url> hr_agent && cd hr_agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env   # TELEGRAM_BOT_TOKEN, GROQ_API_KEY, GEMINI_API_KEY, ALLOWED_USERS, HR_USERS

python -m bot.db          # bazani yaratish va employees.csv'ni yuklash
python -m bot.main        # botni ishga tushirish (polling)
```

Telegram'da botga `/start` yozib tekshiring.

## Serverga joylash (Docker)

```bash
git clone <repo-url> && cd hr_agent
cp .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f hr_bot     # loglarni kuzatish
```

`restart: unless-stopped` — server qayta yuklansa bot o'zi ko'tariladi.
`data/` papka volume sifatida ulangan — konteyner o'chirilsa ham SQLite va
ma'lumotlar yo'qolmaydi.

## Eval

```bash
python -m eval.run
```

`eval/cases.json`da 15 ta test holat bor: to'g'ri tool chaqirilyaptimi,
guardrail'lar ishlayaptimi (maosh berilmasligi, ruxsatsiz ma'lumotga
kirmaslik), RAG to'g'ri javob topayaptimi. Natijalar `eval/results.json`ga
yoziladi.

## Qabul qilingan qarorlar

- **Framework'siz yozildi** — Groq/Gemini SDK + o'z agent loop'i. Kichik,
  bitta bo'limlik agent uchun LangChain/LangGraph ortiqcha murakkablik
  qo'shadi deb hisoblandim.
- **SQLite** — bitta bo'lim, past yuklama uchun PostgreSQL shart emas.
  Kelajakda ko'p bo'lim/xodim bo'lsa PostgreSQL'ga o'tish oson (schema deyarli bir xil).
- **RAG in-memory** — hujjatlar soni kichik (5 ta), har `docker compose up`da
  qayta indekslanadi. pgvector/ChromaDB hozircha ortiqcha.
- **Maosh hech qachon qaytarilmaydi** — `get_employee` tool darajasida bu
  maydon umuman qaytarilmaydi (LLM'ga ham berilmaydi), shunday qilib LLM
  "noto'g'ri eslab qolib" oshkor qilib qo'yish xavfi yo'qoladi.

## Nima qilinmadi (va nega)

- **Scheduler (masalan kunlik brief)** — HR uchun texnik topshiriqda shart
  emas edi (sotuv/moliya bo'limlarida bor). Vaqt yetganda `APScheduler`
  bilan qo'shish mumkin.
- **Webhook (faqat polling)** — muddat siqiq bo'lgani uchun polling bilan
  cheklandim; production uchun webhook + nginx + certbot tavsiya etiladi
  (hujjatning 5.5-bo'limida sxema bor).
- **Gemini function-calling integratsiyasi to'liq bajarildi** — `bot/llm.py` faylida Gemini uchun Function Declarations va tool calling qo'llab-quvvatlovi yaratildi. Groq va Gemini har ikkala provayder ham tool chaqira oladi.
- **HR Avtomatik Bildirishnomalari (Notification)** — Ta'til so'rovi tasdiqlangach, `HR_USERS` ro'yxatidagi barcha HR foydalanuvchilariga Telegram bot orqali real-vaqtda xabar boradi.
- **Guardraillar kuchaytirildi** — `create_leave_request` da oddiy xodim faqat o'ziga ta'til so'rashi mumkinligi kafolatlandi (`requesting_user_id`).
- **Ko'p tilli qo'llab-quvvatlash** — faqat o'zbek tilida javob beradi.

## .env o'zgaruvchilari

`.env.example` fayliga qarang. **`.env` hech qachon git'ga commit qilinmaydi**
(`.gitignore`da bor).
