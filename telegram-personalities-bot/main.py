# main.py (updated)
import os
import sqlite3
import json
import logging
import asyncio
from typing import Optional, Dict, Any, List

import httpx
import openai
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException

# -------------- Настройка логов --------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("telebot-webhook")

# -------------- Переменные окружения --------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
DATABASE_PATH = os.getenv("DATABASE_PATH", "data.db")
BASE_URL = os.getenv("BASE_URL")  # публичный URL приложения (нужно для webhook)
# Админы (список Telegram user_id через запятую)
ADMIN_IDS_ENV = os.getenv("ADMIN_IDS")  # e.g. "761662415,123456789"
DEFAULT_ADMIN = 761662415  # твоё значение из профиля, используется если ADMIN_IDS пустая

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    logger.error("TELEGRAM_TOKEN и OPENAI_API_KEY должны быть заданы.")
    raise SystemExit("Нехватка обязательных переменных окружения")

openai.api_key = OPENAI_API_KEY
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

app = FastAPI()

# -------------- Встроенные персоны (будут загружены в БД при инициализации) --------------
BUILTIN_PERSONALITIES = {
    "einstein": {
        "title": "Альберт Эйнштейн",
        "system": (
            "Ты — Альберт Эйнштейн. Ты говоришь простым, дружелюбным, любопытным тоном. "
            "Твоя манера — немного шутливая, используешь простые аналогии. Эксперт в физике и логике. "
            "Объясняй просто, задавай уточняющий вопрос."
        )
    },
    "aristotle": {
        "title": "Аристотель",
        "system": (
            "Ты — Аристотель, древнегреческий философ. Стиль — мудрый, структурированный. "
            "Эксперт в этике, политике, риторике. Строй ответы в виде тезисов, делай вывод, задай вопрос."
        )
    },
    "temur": {
        "title": "Амир Темур",
        "system": (
            "Ты — Амир Темур (Тамерлан), великий полководец и государственный деятель. "
            "Стиль — уверенный, краткий, стратегический. Дай чёткие рекомендации и план действий."
        )
    }
}

# runtime personalities (will be loaded from DB and merged with builtins)
PERSONALITIES: Dict[str, Dict[str, str]] = {}

# -------------- SQLite: простое хранилище --------------
def init_db():
    conn = sqlite3.connect(DATABASE_PATH)
    cur = conn.cursor()
    # table for storing active personality per chat
    cur.execute("""
        CREATE TABLE IF NOT EXISTS active_personality (
            chat_id INTEGER PRIMARY KEY,
            personality TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # table for storing custom personalities (and builtin ones if not present)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS personalities (
            key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            system TEXT NOT NULL,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # table for creation flow states (temporary)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS creation_state (
            chat_id INTEGER PRIMARY KEY,
            step INTEGER NOT NULL,
            temp_data TEXT
        )
    """)
    conn.commit()
    conn.close()
    logger.info("DB initialized at %s", DATABASE_PATH)

def load_personalities_from_db():
    """Load personalities from DB into runtime PERSONALITIES (merge with builtins)."""
    global PERSONALITIES
    PERSONALITIES = dict(BUILTIN_PERSONALITIES)  # start with builtin
    conn = sqlite3.connect(DATABASE_PATH)
    cur = conn.cursor()
    cur.execute("SELECT key, title, system FROM personalities")
    rows = cur.fetchall()
    for key, title, system in rows:
        PERSONALITIES[key] = {"title": title, "system": system}
    conn.close()
    logger.info("Loaded personalities: %s", list(PERSONALITIES.keys()))

def ensure_builtins_in_db():
    """Insert builtins into DB if not already present (so they are manageable)."""
    conn = sqlite3.connect(DATABASE_PATH)
    cur = conn.cursor()
    for key, data in BUILTIN_PERSONALITIES.items():
        cur.execute("SELECT 1 FROM personalities WHERE key = ?", (key,))
        if not cur.fetchone():
            cur.execute("INSERT INTO personalities(key, title, system, created_by) VALUES(?,?,?,?)",
                        (key, data["title"], data["system"], None))
    conn.commit()
    conn.close()

def set_personality(chat_id: int, pid: str):
    conn = sqlite3.connect(DATABASE_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO active_personality(chat_id, personality)
        VALUES(?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET personality=excluded.personality, updated_at=CURRENT_TIMESTAMP
    """, (chat_id, pid))
    conn.commit()
    conn.close()
    logger.info("Set personality for %s -> %s", chat_id, pid)

def get_personality(chat_id: int) -> Optional[str]:
    conn = sqlite3.connect(DATABASE_PATH)
    cur = conn.cursor()
    cur.execute("SELECT personality FROM active_personality WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def create_personality_in_db(key: str, title: str, system: str, created_by: Optional[int] = None):
    conn = sqlite3.connect(DATABASE_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO personalities(key, title, system, created_by) VALUES(?,?,?,?)",
                (key, title, system, created_by))
    conn.commit()
    conn.close()
    logger.info("Created personality %s by %s", key, created_by)

def delete_personality_from_db(key: str):
    conn = sqlite3.connect(DATABASE_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM personalities WHERE key = ?", (key,))
    conn.commit()
    conn.close()
    logger.info("Deleted personality %s", key)

def get_all_personalities_from_db() -> List[Dict[str, Any]]:
    conn = sqlite3.connect(DATABASE_PATH)
    cur = conn.cursor()
    cur.execute("SELECT key, title, created_by, created_at FROM personalities ORDER BY created_at DESC")
    rows = cur.fetchall()
    conn.close()
    return [{"key": r[0], "title": r[1], "created_by": r[2], "created_at": r[3]} for r in rows]

# Creation flow state (for /newpersona)
def set_creation_state(chat_id: int, step: int, temp_data: Optional[dict]):
    conn = sqlite3.connect(DATABASE_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO creation_state(chat_id, step, temp_data)
        VALUES(?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET step=excluded.step, temp_data=excluded.temp_data
    """, (chat_id, step, json.dumps(temp_data) if temp_data else None))
    conn.commit()
    conn.close()

def get_creation_state(chat_id: int) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(DATABASE_PATH)
    cur = conn.cursor()
    cur.execute("SELECT step, temp_data FROM creation_state WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    step, temp_json = row
    temp = json.loads(temp_json) if temp_json else {}
    return {"step": step, "temp": temp}

def clear_creation_state(chat_id: int):
    conn = sqlite3.connect(DATABASE_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM creation_state WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()

# -------------- Утилиты Telegram API --------------
async def telegram_send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{TELEGRAM_API}/sendMessage", data=payload, timeout=30)
    if resp.status_code != 200:
        logger.error("sendMessage failed: %s", resp.text)
    return resp.json()

async def telegram_answer_callback(callback_query_id: str, text: Optional[str] = None, show_alert: bool = False):
    payload = {"callback_query_id": callback_query_id, "show_alert": show_alert}
    if text:
        payload["text"] = text
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{TELEGRAM_API}/answerCallbackQuery", data=payload, timeout=10)
    if resp.status_code != 200:
        logger.error("answerCallbackQuery failed: %s", resp.text)
    return resp.json()

# -------------- Inline keyboard с личностями --------------
def personalities_keyboard():
    keyboard = {
        "inline_keyboard": [
            [{"text": f"🧪 {PERSONALITIES[k]['title'] if k in PERSONALITIES else k}", "callback_data": f"set:{k}"}]
            for k in PERSONALITIES.keys()
        ]
    }
    return keyboard

# -------------- Admin utilities --------------
def parse_admin_ids() -> List[int]:
    if ADMIN_IDS_ENV:
        try:
            return [int(x.strip()) for x in ADMIN_IDS_ENV.split(",") if x.strip()]
        except Exception:
            logger.warning("ADMIN_IDS env invalid, fallback to default admin.")
    return [DEFAULT_ADMIN]

def is_admin(user_id: int) -> bool:
    return user_id in parse_admin_ids()

# -------------- OpenAI вызов (в фоновом потоке) --------------
def call_openai_chat(system_prompt: str, user_text: str) -> str:
    try:
        response = openai.ChatCompletion.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text}
            ],
            max_tokens=700,
            temperature=0.7,
        )
        text = response.choices[0].message["content"].strip()
        return text
    except Exception as e:
        logger.exception("OpenAI error")
        return f"Ошибка при обращении к OpenAI: {e}"

# -------------- Обработка логики (в фоне) --------------
async def process_message_bg(update: dict):
    """
    Обработка входящего update в фоне: команды, сообщения, callback_query.
    Здесь также реализован пошаговый flow для создания новой личности.
    """
    # Callback (нажатие кнопки)
    if "callback_query" in update:
        cb = update["callback_query"]
        data = cb.get("data", "")
        chat = cb["message"]["chat"]
        chat_id = chat["id"]
        callback_id = cb["id"]

        if data.startswith("set:"):
            _, pid = data.split(":", 1)
            if pid in PERSONALITIES:
                set_personality(chat_id, pid)
                await telegram_answer_callback(callback_id, text=f"Выбран: {PERSONALITIES[pid]['title']}")
                await telegram_send_message(chat_id, f"Личность установлена: <b>{PERSONALITIES[pid]['title']}</b>\nНапиши любое сообщение — я отвечу от её имени.")
            else:
                await telegram_answer_callback(callback_id, text="Неизвестная личность", show_alert=True)
        return

    # Message (текст или командa)
    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "")
        from_user = msg.get("from", {})
        user_id = from_user.get("id")

        # --- Админ команды для управления персоналями ---
        if text.startswith("/newpersona"):
            if not is_admin(user_id):
                await telegram_send_message(chat_id, "У вас нет прав для создания персон.")
                return
            # start creation flow: ask for key
            clear_creation_state(chat_id)
            set_creation_state(chat_id, step=1, temp_data={})
            await telegram_send_message(chat_id,
                "Создание новой личности — шаг 1/3.\n\n"
                "Отправь уникальный ключ (id) для личности (латиницей, без пробелов), например: <code>scientist_x</code>.")
            return

        if text.startswith("/listpersonas"):
            # anyone can list
            rows = get_all_personalities_from_db()
            if not rows:
                await telegram_send_message(chat_id, "Персон пока нет.")
                return
            lines = [f"<b>{r['key']}</b> — {r['title']}" for r in rows]
            await telegram_send_message(chat_id, "Список персон:\n" + "\n".join(lines))
            return

        if text.startswith("/deletepersona"):
            if not is_admin(user_id):
                await telegram_send_message(chat_id, "У вас нет прав для удаления персон.")
                return
            parts = text.split()
            if len(parts) < 2:
                await telegram_send_message(chat_id, "Использование: /deletepersona <key>")
                return
            key = parts[1].strip()
            # prevent deleting builtins unless admin explicitly wants - we allow admins to delete
            delete_personality_from_db(key)
            load_personalities_from_db()
            await telegram_send_message(chat_id, f"Личность <b>{key}</b> удалена (если она существовала).")
            return

        # --- Creation flow handling (если есть state) ---
        state = get_creation_state(chat_id)
        if state:
            step = state["step"]
            temp = state["temp"] or {}
            # step 1: receive key
            if step == 1:
                key = text.strip()
                # validate key: only latin letters, numbers, underscore, hyphen
                import re
                if not re.fullmatch(r"[A-Za-z0-9_-]{2,40}", key):
                    await telegram_send_message(chat_id,
                        "Неверный ключ. Используйте только латиницу, цифры, подчеркивание или дефис (2-40 символов). Попробуйте ещё раз.")
                    return
                # check uniqueness in DB
                conn = sqlite3.connect(DATABASE_PATH)
                cur = conn.cursor()
                cur.execute("SELECT 1 FROM personalities WHERE key = ?", (key,))
                exists = cur.fetchone()
                conn.close()
                if exists:
                    await telegram_send_message(chat_id, "Ключ уже занят. Выберите другой.")
                    return
                temp["key"] = key
                set_creation_state(chat_id, step=2, temp_data=temp)
                await telegram_send_message(chat_id, "Шаг 2/3. Отправь название личности (короткая фраза, например: 'Учитель Python').")
                return
            # step 2: receive title
            if step == 2:
                title = text.strip()
                if len(title) < 2 or len(title) > 120:
                    await telegram_send_message(chat_id, "Название слишком короткое или слишком длинное. Попробуйте ещё раз.")
                    return
                temp["title"] = title
                set_creation_state(chat_id, step=3, temp_data=temp)
                await telegram_send_message(chat_id,
                    "Шаг 3/3. Отправь системный prompt (инструкцию для личности). Например:\n"
                    "<code>Ты — опытный учитель по Python. Отвечай понятно, с примерами и шагами.</code>\n\n"
                    "Когда готов — отправь сообщение с полным текстом инструкции.")
                return
            # step 3: receive system prompt and finalize
            if step == 3:
                system_prompt = text.strip()
                if len(system_prompt) < 10:
                    await telegram_send_message(chat_id, "Инструкция слишком короткая. Опиши поведение личности подробнее.")
                    return
                key = temp.get("key")
                title = temp.get("title")
                try:
                    create_personality_in_db(key=key, title=title, system=system_prompt, created_by=user_id)
                except sqlite3.IntegrityError:
                    await telegram_send_message(chat_id, "Ошибка: личность с таким ключом уже существует.")
                    clear_creation_state(chat_id)
                    return
                # reload runtime personalities
                load_personalities_from_db()
                clear_creation_state(chat_id)
                await telegram_send_message(chat_id,
                    f"Готово! Личность <b>{title}</b> с ключом <code>{key}</code> создана.\n"
                    f"Чтобы использовать: нажми /switch и выбери её (или /listpersonas чтобы увидеть список).")
                return

        # --- Обычные команды доступные всем ---
        if text.startswith("/start"):
            await telegram_send_message(chat_id,
                "Привет! Выбери личность для общения:",
                reply_markup=personalities_keyboard()
            )
            return
        if text.startswith("/switch"):
            await telegram_send_message(chat_id,
                "Выбирай личность:",
                reply_markup=personalities_keyboard()
            )
            return
        if text.startswith("/whoami"):
            pid = get_personality(chat_id)
            if pid and pid in PERSONALITIES:
                await telegram_send_message(chat_id, f"Текущая личность: <b>{PERSONALITIES[pid]['title']}</b>")
            else:
                await telegram_send_message(chat_id, "Пока не выбрана личность. Нажми /switch или /start для выбора.")
            return

        # Обычный текст — проксируем к OpenAI
        pid = get_personality(chat_id)
        if not pid:
            await telegram_send_message(chat_id, "Личность не выбрана. Нажми /switch и выбери одну из личностей.")
            return

        if pid not in PERSONALITIES:
            await telegram_send_message(chat_id, "Выбранная личность больше не доступна. Нажми /switch и выбери другую.")
            return

        system_prompt = PERSONALITIES[pid]["system"]
        user_text = text

        # Вызов OpenAI в executor
        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(None, call_openai_chat, system_prompt, user_text)
        await telegram_send_message(chat_id, reply)
        return

# -------------- FastAPI endpoints --------------
@app.on_event("startup")
async def startup_event():
    init_db()
    ensure_builtins_in_db()
    load_personalities_from_db()
    logger.info("App started. If BASE_URL provided, remember to set Telegram webhook to BASE_URL + /webhook")

@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        update = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    # Обрабатываем в фоне, чтобы быстро вернуть 200 Telegram
    background_tasks.add_task(process_message_bg, update)
    return {"ok": True}

# -------------- Утилита: установка webhook (локально вызови, если нужно) --------------
@app.get("/set_webhook")
async def set_webhook():
    if not BASE_URL:
        raise HTTPException(status_code=400, detail="BASE_URL env var not set")
    webhook_url = BASE_URL.rstrip("/") + "/webhook"
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{TELEGRAM_API}/setWebhook", data={"url": webhook_url}, timeout=30)
    return resp.json()

@app.get("/health")
async def health():
    return {"status": "ok"}
