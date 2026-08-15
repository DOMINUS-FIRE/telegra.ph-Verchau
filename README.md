# Camera Link Telegram Bot

Telegram-бот создаёт одноразовые HTTPS-ссылки с тремя вариантами оформления страницы: TikTok, YouTube и Telegraph. Ссылки всегда ведут на ваш собственный `PUBLIC_BASE_URL`; реальные домены TikTok/YouTube/Telegraph использовать нельзя, потому что они не маршрутизируют запросы на ваш сервер.

Получатель открывает страницу, видит явное уведомление о передаче фото/IP/примерной IP-геолокации, самостоятельно разрешает камеру, делает фото, проверяет снимок и отдельно нажимает «Отправить фото и данные».

## Что приходит в Telegram
- фото;
- IP-адрес запроса;
- примерные город, регион, страна и ISP по IP через `ipwho.is`.

IP-геолокация не является GPS и может ошибаться при VPN, мобильном интернете, CGNAT и корпоративных сетях.

## Что исправлено
- ссылки теперь реально открывают ваш сервер: `PUBLIC_BASE_URL/tiktok/<token>`, `/youtube/<token>`, `/telegraph/<token>`;
- добавлена миграция старой SQLite-базы, если в ней ещё нет поля `service`;
- удалён ошибочный `from app import PAGE`;
- токен безопасно вставляется в JavaScript через JSON;
- ссылка блокируется атомарно во время отправки, чтобы два запроса не отправили два фото;
- если Telegram временно не принял фото, ссылка снова становится доступна;
- добавлена проверка HTTPS/getUserMedia и более понятные ошибки камеры.

## Локальный запуск
1. Python 3.10+.
2. Создайте Telegram-бота через BotFather.
3. `pip install -r requirements.txt`
4. Задайте `BOT_TOKEN` и `PUBLIC_BASE_URL`.
5. `python app.py`

Для камеры на телефоне нужен HTTPS. `localhost` является исключением для локальной разработки.

## Render
Build command:

`pip install -r requirements.txt`

Start command:

`python app.py`

Environment variables:
- `BOT_TOKEN`
- `PUBLIC_BASE_URL` — например `https://camera-link-bot.onrender.com`
- `DB_PATH` — опционально

На эфемерном хостинге SQLite может исчезнуть после redeploy/restart. Для постоянного хранения используйте persistent disk либо внешнюю БД.
