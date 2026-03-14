import os
import asyncio
import logging
import io
import base64
import qrcode
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from pyrogram import Client, errors, raw
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetDialogFiltersRequest, GetDialogsRequest
from telethon.tl.types import InputPeerEmpty
from telethon.tl.functions.users import GetFullUserRequest

# ────────────────────────────────────────────────
# НАСТРОЙКИ ПОД ВАШ ДОМЕН
# ────────────────────────────────────────────────
API_ID = 37381535 
API_HASH = "45b9d76188016001f1ef201f86fb05de"
BOT_TOKEN = '8692188581:AAGJJ5etN6Wrh9z6bxKK0niZ-xGHb8HEug0'

# Твой домен для кнопки в боте
BASE_URL = "http://waitsender.online.swtest.ru" 

SESSIONS_DIR = "sessions"
PHOTO_URL = "https://drive.google.com/uc?export=download&id=1F62quk7LcaM8gn0fLC82JtgAEPkN6ihP"

if not os.path.exists(SESSIONS_DIR):
    os.makedirs(SESSIONS_DIR)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
app = FastAPI()

# Разрешаем сайту делать запросы к бэкенду
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_methods=["*"],
    allow_headers=["*"],
)

active_auth_clients = {}
telethon_clients = {}
active_folders = {}
saved_data = {}

class SendStates(StatesGroup):
    choosing_folder = State()
    typing_text = State()
    typing_delay = State()
    sending = State()

# ────────────────────────────────────────────────
# СЛУЖЕБНЫЕ ФУНКЦИИ
# ────────────────────────────────────────────────

async def get_session_str(user_id: int):
    path = os.path.join(SESSIONS_DIR, f"{user_id}.txt")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return None

async def get_telethon_client(user_id: int) -> TelegramClient:
    if user_id in telethon_clients and telethon_clients[user_id].is_connected():
        return telethon_clients[user_id]
    ss = await get_session_str(user_id)
    if not ss: return None
    client = TelegramClient(StringSession(ss), API_ID, API_HASH)
    await client.connect()
    telethon_clients[user_id] = client
    return client

# ────────────────────────────────────────────────
# API ЭНДПОИНТЫ (ДЛЯ САЙТА)
# ────────────────────────────────────────────────

@app.post("/auth/send_code")
async def api_send_code(phone: str):
    clean_phone = "+" + "".join(filter(str.isdigit, phone))
    client = Client(name=f"temp_{clean_phone}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
    try:
        await client.connect()
        code_data = await client.send_code(clean_phone)
        active_auth_clients[clean_phone] = {"client": client, "hash": code_data.phone_code_hash}
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/auth/verify_code")
async def api_verify_code(phone: str, code: str, password: str = None):
    clean_phone = "+" + "".join(filter(str.isdigit, phone))
    data = active_auth_clients.get(clean_phone)
    if not data: raise HTTPException(status_code=404, detail="Сессия не найдена")
    client = data["client"]
    try:
        try:
            await client.sign_in(clean_phone, data["hash"], code)
        except errors.SessionPasswordNeeded:
            if password: await client.check_password(password)
            else: return {"status": "password_required"}

        user = await client.get_me()
        ss = await client.export_session_string()
        with open(os.path.join(SESSIONS_DIR, f"{user.id}.txt"), "w", encoding="utf-8") as f:
            f.write(ss)
        await client.disconnect()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/auth/generate_qr")
async def api_generate_qr():
    sid = str(os.urandom(4).hex())
    client = Client(name=f"qr_{sid}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
    try:
        await client.connect()
        qr_data = await client.invoke(raw.functions.auth.ExportLoginToken(api_id=API_ID, api_hash=API_HASH, except_ids=[]))
        token_b64 = base64.urlsafe_b64encode(qr_data.token).decode("utf-8").rstrip("=")
        img = qrcode.make(f"tg://login?token={token_b64}")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        active_auth_clients[sid] = {"client": client, "token": qr_data.token}
        return {"status": "success", "qr_image": base64.b64encode(buf.getvalue()).decode(), "session_id": sid}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/auth/check_qr")
async def api_check_qr(session_id: str):
    data = active_auth_clients.get(session_id)
    if not data: return {"status": "expired"}
    try:
        res = await data["client"].invoke(raw.functions.auth.ImportLoginToken(token=data["token"]))
        if isinstance(res, raw.types.auth.LoginTokenSuccess):
            user = await data["client"].get_me()
            ss = await data["client"].export_session_string()
            with open(os.path.join(SESSIONS_DIR, f"{user.id}.txt"), "w", encoding="utf-8") as f:
                f.write(ss)
            await data["client"].disconnect()
            del active_auth_clients[session_id]
            return {"status": "success"}
        return {"status": "waiting"}
    except: return {"status": "waiting"}

# ────────────────────────────────────────────────
# ЛОГИКА БОТА
# ────────────────────────────────────────────────

@dp.message(F.text == "/start")
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    ss = await get_session_str(user_id)
    if not ss:
        builder = InlineKeyboardBuilder().button(text="🌐 Открыть WaitSender", url=BASE_URL)
        return await message.answer("🔐 <b>Вход не выполнен</b>\nАвторизуйтесь на сайте:", reply_markup=builder.as_markup())
    
    builder = InlineKeyboardBuilder().button(text="🚀 Панель управления", callback_data="main_menu")
    await bot.send_photo(message.chat.id, PHOTO_URL, caption="🔥 <b>WaitSender активен</b>", reply_markup=builder.as_markup())

@dp.callback_query(F.data == "main_menu")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    builder = InlineKeyboardBuilder()
    builder.button(text="▶️ Рассылка по папкам", callback_data="folders")
    builder.button(text="👤 Мой аккаунт", callback_data="cabinet")
    builder.adjust(1)
    await callback.message.edit_caption(caption="🏠 <b>Главное меню</b>", reply_markup=builder.as_markup())

@dp.callback_query(F.data == "folders")
async def show_folders(callback: CallbackQuery, state: FSMContext):
    client = await get_telethon_client(callback.from_user.id)
    if not client: return await callback.answer("Ошибка сессии")
    resp = await client(GetDialogFiltersRequest())
    builder = InlineKeyboardBuilder()
    f_map = {}
    for i, f in enumerate(resp.filters):
        if hasattr(f, 'title'):
            name = f.title.text if hasattr(f.title, 'text') else str(f.title)
            f_map[f"f_{i}"] = name
            status = "🟢" if name in active_folders and not active_folders[name]['stop_flag'] else "🔴"
            builder.button(text=f"{status} {name}", callback_data=f"f_{i}")
    builder.button(text="⬅️ Назад", callback_data="main_menu")
    builder.adjust(1)
    await state.update_data(f_map=f_map, raw_filters=resp.filters)
    await callback.message.edit_caption(caption="📁 <b>Выберите папку:</b>", reply_markup=builder.as_markup())
    await state.set_state(SendStates.choosing_folder)

@dp.callback_query(SendStates.choosing_folder)
async def folder_select(callback: CallbackQuery, state: FSMContext):
    if callback.data == "main_menu": return await back_to_main(callback, state)
    data = await state.get_data()
    fname = data['f_map'].get(callback.data)
    if not fname: return
    await state.update_data(cur_folder=fname)
    await send_f_settings(callback.message, state, fname)

async def send_f_settings(message: Message, state: FSMContext, fname: str):
    s = saved_data.get(fname, {})
    running = fname in active_folders and not active_folders[fname]['stop_flag']
    caption = f"📂 Папка: <b>{fname}</b>\nСтатус: {'🚀 Идет' if running else '⏹ Стоп'}\n\n💬 Текст: {s.get('text', '—')}\n⏱ Задержка: {s.get('delay', '—')}с"
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Текст", callback_data="set_text")
    builder.button(text="⏱ Задержка", callback_data="set_delay")
    builder.button(text="⏹ Стоп" if running else "▶️ Старт", callback_data="toggle_run")
    builder.button(text="⬅️ Назад", callback_data="folders")
    builder.adjust(2)
    await message.edit_caption(caption=caption, reply_markup=builder.as_markup())
    await state.set_state(SendStates.sending)

@dp.callback_query(F.data == "toggle_run")
async def toggle_run(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    fname = data['cur_folder']
    s = saved_data.get(fname, {})
    if fname in active_folders and not active_folders[fname]['stop_flag']:
        active_folders[fname]['stop_flag'] = True
    else:
        if not s.get('text') or not s.get('delay'): return await callback.answer("Заполните настройки!")
        active_folders[fname] = {'stop_flag': False, 'text': s['text'], 'delay': s['delay']}
        asyncio.create_task(run_broadcast(fname, callback.from_user.id, data['raw_filters']))
    await send_f_settings(callback.message, state, fname)

async def run_broadcast(fname, uid, filters):
    info = active_folders[fname]
    client = await get_telethon_client(uid)
    f_obj = next((f for f in filters if hasattr(f, 'title') and (f.title.text if hasattr(f.title, 'text') else str(f.title)) == fname), None)
    dialogs = await client(GetDialogsRequest(offset_date=None, offset_id=0, offset_peer=InputPeerEmpty(), limit=200, hash=0))
    t_ids = {p.channel_id if hasattr(p, 'channel_id') else p.chat_id for p in f_obj.include_peers if hasattr(p, 'channel_id') or hasattr(p, 'chat_id')}
    targets = [d for d in dialogs.chats if d.id in t_ids]
    async def worker(t):
        while not info['stop_flag']:
            try: await client.send_message(t, info['text'])
            except: pass
            await asyncio.sleep(info['delay'])
    await asyncio.gather(*(worker(t) for t in targets))

@dp.callback_query(F.data == "set_text")
async def set_text_cb(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Пришлите текст сообщения:")
    await state.set_state(SendStates.typing_text)

@dp.message(SendStates.typing_text)
async def get_text_msg(message: Message, state: FSMContext):
    data = await state.get_data()
    saved_data.setdefault(data['cur_folder'], {})['text'] = message.text
    await message.answer("✅ Текст сохранен. Нажмите /start для меню.")

@dp.callback_query(F.data == "set_delay")
async def set_delay_cb(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите задержку (сек):")
    await state.set_state(SendStates.typing_delay)

@dp.message(SendStates.typing_delay)
async def get_delay_msg(message: Message, state: FSMContext):
    if not message.text.isdigit(): return
    data = await state.get_data()
    saved_data.setdefault(data['cur_folder'], {})['delay'] = int(message.text)
    await message.answer("✅ Задержка сохранена.")

async def main():
    # Запускаем сервер на 8000 порту
    config = uvicorn.Config(app, host="0.0.0.0", port=8000)
    server = uvicorn.Server(config)
    await asyncio.gather(server.serve(), dp.start_polling(bot))

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
