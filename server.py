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
import asyncio, json, hashlib, hmac as hmac_lib, datetime, os, random, string, smtplib, http
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
SMTP_USER  = os.environ.get("SMTP_USER",  "rakhmet.danial2000@gmail.com")
SMTP_PASS  = os.environ.get("SMTP_PASS",  "pfve xwbr zfoi bemy")
SMTP_FROM  = os.environ.get("SMTP_FROM",  SMTP_USER)
DB_URL     = os.environ.get("DATABASE_URL", "postgresql://salam_db_2v1p_user:gcABVaPlAzRxHqFudVQL1IUruYqjRzLU@dpg-d8hqlc5dt1ts73ejkl80-a/salam_db_2v1p")

db_pool   = None
clients   = {}   # {ws: {username, email, user_id}}
otp_store = {}   # {email: {otp, username, expires}}

# ── Вспомогательные функции ──────────────────────────────────
def compute_hmac(text: str) -> str:
    return hmac_lib.new(SECRET_KEY.encode(), text.encode(), hashlib.sha256).hexdigest()

def generate_otp() -> str:
    return "".join(random.choices(string.digits, k=6))

def send_otp_email(to_email: str, username: str, otp: str):
    if not SMTP_USER or not SMTP_PASS:
        print(f"⚠ [SMTP] Пропущены учетные данные. Код для {to_email} ({username}): {otp}")
        return True
    try:
        msg = MIMEText(f"Привет, {username}!\nТвой одноразовый код для входа в Salam: {otp}\nКод действует 5 минут.")
        msg["Subject"] = f"{otp} — Код авторизации Salam"
        msg["From"] = SMTP_FROM
        msg["To"] = to_email

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, [to_email], msg.as_string())
        return True
    except Exception as e:
        print(f"❌ Ошибка отправки email на {to_email}: {e}")
        return False

# ── База Данных ──────────────────────────────────────────────
async def init_db():
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=10)
        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    username VARCHAR(100) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
        print("✅ База данных PostgreSQL успешно инициализирована")
    except Exception as e:
        print(f"❌ Не удалось подключиться к БД: {e}")
        db_pool = None

async def get_or_create_user(email: str, username: str):
    if not db_pool:
        return random.randint(1000, 9999)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, username FROM users WHERE email = $1", email)
        if row:
            if row["username"] != username:
                await conn.execute("UPDATE users SET username = $2 WHERE id = $1", row["id"], username)
            return row["id"]
        else:
            user_id = await conn.fetchval(
                "INSERT INTO users (email, username) VALUES ($1, $2) RETURNING id", 
                email, username
            )
            return user_id

# ── Логика Веб-Сокетов ───────────────────────────────────────
def online_users():
    return list(set(c["username"] for c in clients.values()))

async def broadcast(msg_dict):
    if not clients: return
    payload = json.dumps(msg_dict)
    await asyncio.gather(*[ws.send(payload) for ws in clients], return_exceptions=True)

# Перехватчик HTTP-запросов (Health Check) для предотвращения падения на Render.com
async def health_check(connection, request):
    # Если Render проверяет доступность через HEAD или GET на корень сайта "/"
    if request.method == "HEAD" or request.path == "/":
        # Возвращаем стандартный ответ 200 OK, подтверждающий, что порт слушается
        return http.HTTPStatus.OK, [("Content-Type", "text/plain")], b"OK"
    return None # Если это WebSocket-handshake (GET с Upgrade), передаем управление дальше

async def handler(websocket):
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
            except:
                continue

            action = data.get("action")

            if action == "request_otp":
                email = (data.get("email") or "").strip().lower()
                username = (data.get("username") or "").strip()
                if not email or not username:
                    await websocket.send(json.dumps({"type":"error","text":"Заполни все поля"}))
                    continue
                
                otp = generate_otp()
                otp_store[email] = {
                    "otp": otp,
                    "username": username,
                    "expires": datetime.datetime.utcnow() + datetime.timedelta(minutes=5)
                }
                
                success = send_otp_email(email, username, otp)
                if success:
                    await websocket.send(json.dumps({"type":"otp_sent","text":"Код отправлен на ваш email"}))
                else:
                    await websocket.send(json.dumps({"type":"error","text":"Ошибка отправки письма. Проверьте настройки SMTP сервера."}))

            elif action == "verify_otp":
                email = (data.get("email") or "").strip().lower()
                code = (data.get("code") or "").strip()
                
                if email not in otp_store:
                    await websocket.send(json.dumps({"type":"error","text":"Сначала запроси код"}))
                    continue
                
                record = otp_store[email]
                if datetime.datetime.utcnow() > record["expires"]:
                    del otp_store[email]
                    await websocket.send(json.dumps({"type":"error","text":"Срок действия кода истек"}))
                    continue
                
                if record["otp"] != code:
                    await websocket.send(json.dumps({"type":"error","text":"Неверный код"}))
                    continue
                
                username = record["username"]
                del otp_store[email]

                user_id = await get_or_create_user(email, username)
                clients[websocket] = {"username": username, "email": email, "user_id": user_id}
                
                print(f"[+] {username} ({email}) joined")
                await websocket.send(json.dumps({"type":"auth_ok","username":username,"email":email}))
                await broadcast({"type":"system","text":f"{username} вошел в чат!","users":online_users()})

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
        global db_pool
        db_pool = None
    else:
        await init_db()

    port = int(os.environ.get("PORT", 8765))
    print(f"Salam server on :{port}")
    # Подключаем health_check через параметр process_request
    async with websockets.serve(handler, "0.0.0.0", port, process_request=health_check):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
