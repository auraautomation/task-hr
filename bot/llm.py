"""
Ikki LLM provayder bilan ishlaydigan qatlam: Groq (asosiy, tez va bepul) va
Gemini (fallback). LLM_PROVIDER env orqali qaysi biri asosiy ekanligi tanlanadi.
Birinchi provayder xato bersa (429, timeout, connection error) ikkinchisiga o'tadi.
Ikkalasi ham ishlamasa - foydalanuvchiga tushunarli xabar qaytariladi (traceback yo'q).
"""
import asyncio
import json
import os

from dotenv import load_dotenv

load_dotenv()  # bu modul boshqa joydan import qilinganda ham .env kafolatli yuklansin

from groq import Groq
import google.generativeai as genai

def _clean_env(key: str, default: str) -> str:
    v = os.getenv(key, default)
    return v.split("#")[0].strip() if v else default

GROQ_MODEL = _clean_env("GROQ_MODEL", "llama-3.1-8b-instant")
GEMINI_MODEL = _clean_env("GEMINI_MODEL", "gemini-3.5-flash-lite")
PRIMARY = _clean_env("LLM_PROVIDER", "gemini")

_groq_client = Groq(api_key=os.getenv("GROQ_API_KEY")) if os.getenv("GROQ_API_KEY") else None
if os.getenv("GEMINI_API_KEY"):
    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))


class LLMError(Exception):
    """Ikkala provayder ham ishlamaganda ko'tariladi."""


async def _call_groq(messages: list[dict], tools: list[dict] | None):
    def _sync():
        return _groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            tools=tools,
            tool_choice="auto" if tools else None,
            temperature=0.3,
        )

    resp = await asyncio.to_thread(_sync)
    msg = resp.choices[0].message
    tool_calls = []
    if msg.tool_calls:
        for tc in msg.tool_calls:
            tool_calls.append(
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments or "{}"),
                }
            )
    return {"text": msg.content, "tool_calls": tool_calls}


GEMINI_MODELS = [
    _clean_env("GEMINI_MODEL", "gemini-3.5-flash-lite"),
    "gemini-2.5-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
]

async def _call_gemini(messages: list[dict], tools: list[dict] | None):
    last_ex = None
    for model_name in GEMINI_MODELS:
        def _sync(target_model=model_name):
            gemini_tools = None
            if tools:
                declarations = []
                for t in tools:
                    fn = t.get("function", {})
                    declarations.append({
                        "name": fn.get("name"),
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {})
                    })
                gemini_tools = [{"function_declarations": declarations}]

            model = genai.GenerativeModel(target_model, tools=gemini_tools)
            
            prompt_parts = []
            for m in messages:
                role = m.get("role")
                content = m.get("content")
                if not content:
                    continue
                if role == "system":
                    prompt_parts.append(f"System: {content}")
                elif role == "user":
                    prompt_parts.append(f"User: {content}")
                elif role == "assistant":
                    prompt_parts.append(f"Assistant: {content}")
                elif role == "tool":
                    prompt_parts.append(f"Tool Result: {content}")
            
            prompt = "\n\n".join(prompt_parts)
            return model.generate_content(prompt)

        try:
            resp = await asyncio.to_thread(_sync)
            text_content = ""
            tool_calls = []
            
            try:
                if resp.candidates and resp.candidates[0].content.parts:
                    for i, part in enumerate(resp.candidates[0].content.parts):
                        if hasattr(part, "text") and part.text:
                            text_content += part.text
                        if hasattr(part, "function_call") and part.function_call and part.function_call.name:
                            fc = part.function_call
                            args = dict(fc.args) if fc.args else {}
                            tool_calls.append({
                                "id": f"call_{i}",
                                "name": fc.name,
                                "arguments": args
                            })
            except Exception as e:
                print(f"[gemini] response parse error: {e}")
                text_content = getattr(resp, "text", "")

            return {"text": text_content, "tool_calls": tool_calls}

        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "Quota" in err_str or "ResourceExhausted" in err_str:
                print(f"[gemini] {model_name} 429 rate-limit ga tushdi, alternatiiv modelga o'tilmoqda...")
                last_ex = e
                continue
            else:
                raise e

    if last_ex:
        raise last_ex
    raise Exception("Barcha Gemini modellarida xatolik yuz berdi")


async def call_llm(messages: list[dict], tools: list[dict] | None = None, max_retries: int = 5):
    """
    Asosiy provayderni sinaydi, xato bo'lsa backoff bilan qayta uradi,
    baribir ishlamasa ikkinchi provayderga o'tadi.
    """
    providers = [PRIMARY]
    secondary = "groq" if PRIMARY == "gemini" else "gemini"
    if secondary == "gemini" and os.getenv("GEMINI_API_KEY"):
        providers.append("gemini")
    elif secondary == "groq" and _groq_client:
        providers.append("groq")

    last_err = None

    for provider in providers:
        fn = _call_groq if provider == "groq" else _call_gemini
        if provider == "groq" and _groq_client is None:
            continue
        if provider == "gemini" and not os.getenv("GEMINI_API_KEY"):
            continue

        for attempt in range(max_retries):
            try:
                return await fn(messages, tools)
            except Exception as e:  # 429, timeout, connection error va h.k.
                last_err = e
                err_str = str(e)
                wait = 15 if ("429" in err_str or "Quota" in err_str or "ResourceExhausted" in err_str) else (2 ** attempt)
                print(f"[llm] {provider} xato (urinish {attempt+1}/{max_retries}): {e}. {wait}s kutish...")
                await asyncio.sleep(wait)
        print(f"[llm] {provider} ishlamadi, keyingi provayderga o'tildi")

    raise LLMError(f"Hech qanday LLM provayder javob bermadi: {last_err}")
