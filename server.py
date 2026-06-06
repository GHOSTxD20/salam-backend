#!/usr/bin/env python3
"""
Salam — WebSocket server with email OTP auth + PostgreSQL
Env vars required:
  DATABASE_URL   — postgres://...
  SECRET_KEY     — hmac secret
  SMTP_HOST      — e.g. smtp.gmail.com
  SMTP_PORT      — 587
  SMTP_USER      — your@gmail.com
  SMTP_PASS      — app password
  SMTP_FROM      — your@gmail.com
"""
import asyncio, json, hashlib, hmac as hmac_lib, datetime, os, random, string, smtplib
from email.mime.text import MIMEText

try:
    import websockets
    import asyncpg
except ImportError:
    import subprocess, sys
    subprocess.run([sys.executable,"-m","pip","install","websockets","asyncpg","--break-system-packages","-q"])
    import websockets, asyncpg

SECRET_KEY = os.environ.get("SECRET_KEY", "change-me")
SMTP_HOST  = os.environ.get("SMTP_HOST",  "smtp.gmail.com")
SMTP_PORT  = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER  = os.environ.get("SMTP_USER",  "")
SMTP_PASS  = os.environ.get("SMTP_PASS",  "")
SMTP_FROM  = os.environ.get("SMTP_FROM",  SMTP_USER)
DB_URL     = os.environ.get("DATABASE_URL", "")

db_pool   = None
clients   = {}   # {ws: {username, email, user_id}}
otp_store = {}   # {email: {code, expires, username}}

# ── DB ────────────────────────────────────────────────────────
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=5)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id         SERIAL PRIMARY KEY,
                email      TEXT UNIQUE NOT NULL,
                username   TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    print("[db] tables ready")

async def get_or_create_user(email: str, username: str) -> dict:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE email=$1", email)
        if row:
            return dict(row)
        row = await conn.fetchrow(
            "INSERT INTO users(email,username) VALUES($1,$2) RETURNING *",
            email, username
        )
        return dict(row)

# ── OTP ───────────────────────────────────────────────────────
def gen_otp() -> str:
    return "".join(random.choices(string.digits, k=6))

def send_otp_email(to: str, code: str, username: str):
    body = f"""Привет, {username}!

Твой код для входа в Salam:

  ┌─────────────┐
  │   {code}   │
  └─────────────┘

Код действует 10 минут.
Если ты не запрашивал вход — просто игнорируй это письмо.

— Salam Messenger
"""
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"[Salam] Код подтверждения: {code}"
    msg["From"]    = SMTP_FROM
    msg["To"]      = to
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_FROM, [to], msg.as_string())

# ── HMAC ─────────────────────────────────────────────────────
def compute_hmac(text: str) -> str:
    return hmac_lib.new(SECRET_KEY.encode(), text.encode(), hashlib.sha256).hexdigest()

# ── Broadcast ────────────────────────────────────────────────
async def broadcast(data: dict, exclude=None):
    msg = json.dumps(data, ensure_ascii=False)
    dead = []
    for ws in list(clients):
        if ws == exclude:
            continue
        try:
            await ws.send(msg)
        except:
            dead.append(ws)
    for ws in dead:
        clients.pop(ws, None)

def online_users():
    return [v["username"] for v in clients.values()]

# ── Handler ──────────────────────────────────────────────────
async def handler(websocket):
    try:
        async for raw in websocket:
            try:
                data = json.loads(raw)
            except:
                continue
            action = data.get("action")

            # ── 1. Запрос OTP ──────────────────────────────
            if action == "request_otp":
                email    = (data.get("email") or "").strip().lower()
                username = (data.get("username") or "").strip()[:24]
                if not email or "@" not in email or not username:
                    await websocket.send(json.dumps({"type":"error","text":"Неверный email или имя"}))
                    continue
                code    = gen_otp()
                expires = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)
                otp_store[email] = {"code": code, "expires": expires, "username": username}
                try:
                    await asyncio.to_thread(send_otp_email, email, code, username)
                    await websocket.send(json.dumps({"type":"otp_sent","email":email}))
                    print(f"[otp] sent to {email}")
                except Exception as e:
                    print(f"[smtp error] {e}")
                    await websocket.send(json.dumps({"type":"error","text":f"Ошибка отправки письма: {e}"}))

            # ── 2. Проверка OTP ────────────────────────────
            elif action == "verify_otp":
                email = (data.get("email") or "").strip().lower()
                code  = (data.get("code")  or "").strip()
                entry = otp_store.get(email)
                if not entry:
                    await websocket.send(json.dumps({"type":"error","text":"Сначала запроси код"}))
                    continue
                if datetime.datetime.utcnow() > entry["expires"]:
                    otp_store.pop(email, None)
                    await websocket.send(json.dumps({"type":"error","text":"Код истёк, запроси новый"}))
                    continue
                if code != entry["code"]:
                    await websocket.send(json.dumps({"type":"error","text":"Неверный код"}))
                    continue
                otp_store.pop(email, None)
                user = await get_or_create_user(email, entry["username"])
                clients[websocket] = {"username": user["username"], "email": email, "user_id": user["id"]}
                await websocket.send(json.dumps({
                    "type": "auth_ok",
                    "username": user["username"],
                    "users": online_users()
                }))
                await broadcast({
                    "type": "system",
                    "text": f"{user['username']} вошёл в чат.",
                    "users": online_users()
                }, exclude=websocket)
                print(f"[auth] {user['username']} ({email})")

            # ── 3. Сообщение ───────────────────────────────
            elif action == "message":
                if websocket not in clients:
                    await websocket.send(json.dumps({"type":"error","text":"Сначала войди"}))
                    continue
                text = (data.get("text") or "").strip()[:2000]
                font = data.get("font", "Default")
                if not text:
                    continue
                sig = compute_hmac(text)
                ts  = datetime.datetime.utcnow().strftime("%H:%M")
                sender = clients[websocket]["username"]
                # Сервер передаёт хэш. Клиент отобразит его.
                entry = {"type":"message","sender":sender,"text":text,"hmac":sig,"time":ts,"font":font}
                await broadcast(entry)

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if websocket in clients:
            name = clients.pop(websocket)["username"]
            print(f"[-] {name} left")
            await broadcast({"type":"system","text":f"{name} покинул чат.","users":online_users()})

# ── Main ─────────────────────────────────────────────────────
async def main():
    if not DB_URL:
        print("⚠  DATABASE_URL не задан — пользователи не сохраняются (тестовый режим)")
        # fallback без БД
        global db_pool
        db_pool = None
    else:
        await init_db()

    port = int(os.environ.get("PORT", 8765))
    print(f"Salam server on :{port}")
    async with websockets.serve(handler, "0.0.0.0", port):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
