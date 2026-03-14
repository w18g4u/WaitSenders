import os
import asyncio
import qrcode
import io
import base64
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pyrogram import Client, errors, raw
import uvicorn

# --- ТВОИ НАСТРОЙКИ ---
API_ID = 37381535 
API_HASH = "45b9d76188016001f1ef201f86fb05de"
SESSIONS_DIR = r"E:\Project\SenderWait\sessions"

if not os.path.exists(SESSIONS_DIR):
    os.makedirs(SESSIONS_DIR)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

active_clients = {}

def format_phone(phone: str) -> str:
    return "+" + "".join(filter(str.isdigit, phone))

# --- ВХОД ПО НОМЕРУ ---
@app.post("/auth/send_code")
async def send_code(phone: str):
    clean_phone = format_phone(phone)
    client = Client(name=f"temp_{clean_phone}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
    try:
        await client.connect()
        code_data = await client.send_code(clean_phone)
        active_clients[clean_phone] = {"client": client, "hash": code_data.phone_code_hash}
        print(f">>> Код отправлен на {clean_phone}")
        return {"status": "success"}
    except Exception as e:
        print(f"!!! Ошибка отправки: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/auth/verify_code")
async def verify_code(phone: str, code: str, password: str = None):
    clean_phone = format_phone(phone)
    data = active_clients.get(clean_phone)
    if not data:
        raise HTTPException(status_code=404, detail="Сессия не найдена.")
    
    client = data["client"]
    try:
        try:
            await client.sign_in(clean_phone, data["hash"], code)
        except errors.SessionPasswordNeeded:
            if password:
                await client.check_password(password)
            else:
                return {"status": "password_required"}

        string_session = await client.export_session_string()
        file_path = os.path.join(SESSIONS_DIR, f"{clean_phone.replace('+', '')}.txt")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(string_session)
        
        await client.disconnect()
        del active_clients[clean_phone]
        print(f"✅ Сессия сохранена: {file_path}")
        return {"status": "success"}
    except Exception as e:
        print(f"!!! Ошибка входа: {e}")
        raise HTTPException(status_code=400, detail=str(e))

# --- ВХОД ПО QR-КОДУ (RAW МЕТОДЫ) ---
@app.post("/auth/generate_qr")
async def generate_qr():
    session_id = str(os.urandom(4).hex())
    client = Client(name=f"qr_{session_id}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
    try:
        await client.connect()
        print(">>> Запрос QR-кода (через RAW API)...")
        
        # Прямой вызов функции Telegram API
        qr_data = await client.invoke(
            raw.functions.auth.ExportLoginToken(
                api_id=API_ID,
                api_hash=API_HASH,
                except_ids=[]
            )
        )
        
        # Создаем URL для сканера Telegram
        token_b64 = base64.urlsafe_b64encode(qr_data.token).decode("utf-8").rstrip("=")
        url = f"tg://login?token={token_b64}"
        
        # Рисуем QR
        img = qrcode.make(url)
        buffered = io.BytesIO()
        img.save(buffered, format="PNG")
        img_base64 = base64.b64encode(buffered.getvalue()).decode()

        active_clients[session_id] = {
            "client": client, 
            "token": qr_data.token,
            "expires": qr_data.expires
        }
        
        print(f"✅ QR успешно создан (ID: {session_id})")
        return {"status": "success", "qr_image": img_base64, "session_id": session_id}
        
    except Exception as e:
        print(f"!!! Ошибка генерации QR: {e}")
        if client.is_connected:
            await client.disconnect()
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/auth/check_qr")
async def check_qr(session_id: str):
    data = active_clients.get(session_id)
    if not data:
        return {"status": "expired"}
    
    client = data["client"]
    try:
        # Проверяем статус токена через ImportLoginToken
        auth_res = await client.invoke(
            raw.functions.auth.ImportLoginToken(token=data["token"])
        )
        
        # Если Telegram вернул Success — мы внутри
        if isinstance(auth_res, raw.types.auth.LoginTokenSuccess):
            user = await client.get_me()
            string_session = await client.export_session_string()
            
            file_path = os.path.join(SESSIONS_DIR, f"{user.id}.txt")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(string_session)
                
            await client.disconnect()
            del active_clients[session_id]
            print(f"✅ Вход через QR выполнен: {user.first_name}")
            return {"status": "success"}
        
        return {"status": "waiting"}
        
    except errors.SessionPasswordNeeded:
        return {"status": "password_required"}
    except Exception:
        return {"status": "waiting"}

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)