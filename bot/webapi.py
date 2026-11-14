"""Веб-сервер мини-приложения: отдаёт страницу и JSON-API ко всем разделам бота.

Каждый запрос к /api/ подписан данными Telegram (initData): по ним сервер
узнаёт пользователя и проверяет его разрешения так же, как бот в чате.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import secrets
import shutil
import time
import zipfile
from collections import Counter

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, User
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web
from PIL import Image, UnidentifiedImageError

from .access import DEFAULT_PERMS, PERMS, Access, dump_perms, parse_perms, resolve_access
from .config import BASE_DIR, Config
from .db import Database
from .importer import MAX_TICKETS_AT_ONCE, clean, normalize_phone
from . import ai, importer
from .events import Hub
from .imaging import (
    ALIGNS, clamp_box, clamp_name_box, decode_qr, extract_token, image_size, save_template,
)
from .service import (
    admin_label, current_template, export_file, groq_key, locate_box, mask, notify_admin,
    render_preview, sync_menu_button, ticket_dict, ticket_file, ticket_png,
)
from .ui import can_delete, client_block, export_txt, ticket_code

log = logging.getLogger(__name__)

PAGE = BASE_DIR / "webapp" / "index.html"
INIT_DATA_TTL = 24 * 3600
LIST_LIMIT = 500


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


class WebApi:
    def __init__(
        self, bot: Bot, db: Database, config: Config, bot_username: str,
        hub: Hub | None = None,
    ):
        self.bot, self.db, self.config, self.bot_username = bot, db, config, bot_username
        self.hub = hub or Hub()
        self.jobs: dict[str, dict] = {}
        self.background: set[asyncio.Task] = set()

    def build(self) -> web.Application:
        app = web.Application(middlewares=[self.middleware], client_max_size=25 * 1024 ** 2)
        app.router.add_get("/", self.page)
        post = {
            "me": self.me,
            "tickets/create": self.tickets_create,
            "tickets/list": self.tickets_list,
            "tickets/action": self.tickets_action,
            "tickets/export": self.tickets_export,
            "scan": self.scan,
            "scan/photo": self.scan_photo,
            "events/poll": self.events_poll,
            "tickets/image": self.ticket_image,
            "import/start": self.import_start,
            "import/status": self.import_status,
            "import/create": self.import_create,
            "stats": self.stats,
            "admins/list": self.admins_list,
            "admins/add": self.admins_add,
            "admins/toggle": self.admins_toggle,
            "admins/remove": self.admins_remove,
            "design/state": self.design_state,
            "design/upload": self.design_upload,
            "design/retry": self.design_retry,
            "design/save": self.design_save,
            "design/cancel": self.design_cancel,
            "design/default": self.design_default,
            "design/image": self.design_image,
            "reset": self.reset,
            "settings/get": self.settings_get,
            "settings/set": self.settings_set,
            "settings/check": self.settings_check,
        }
        for path, handler in post.items():
            app.router.add_post(f"/api/{path}", handler)
        return app

    # ---------- общее ----------

    @web.middleware
    async def middleware(self, request: web.Request, handler):
        if not request.path.startswith("/api/"):
            return await handler(request)
        try:
            request["access"], request["user"] = self.authorize(request)
            return await handler(request)
        except ApiError as e:
            return web.json_response({"error": e.message}, status=e.status)
        except TelegramAPIError as e:
            log.warning("Telegram: %s", e)
            return web.json_response(
                {"error": "Telegram не принял запрос. Откройте чат с ботом и нажмите /start."},
                status=502,
            )
        except Exception:
            log.exception("Ошибка в API %s", request.path)
            return web.json_response({"error": "Внутренняя ошибка сервера"}, status=500)

    def authorize(self, request: web.Request) -> tuple[Access, User]:
        try:
            data = safe_parse_webapp_init_data(
                self.config.bot_token, request.headers.get("X-Init-Data", "")
            )
        except ValueError:
            raise ApiError("Откройте приложение кнопкой Mini App в боте", 401)
        if not data.user or time.time() - data.auth_date.timestamp() > INIT_DATA_TTL:
            raise ApiError("Сессия устарела — закройте и откройте приложение заново", 401)
        user = User(
            id=data.user.id, is_bot=False, first_name=data.user.first_name,
            last_name=data.user.last_name, username=data.user.username,
        )
        access = resolve_access(user, self.db, self.config)
        if access is None:
            raise ApiError("Нет доступа", 403)
        return access, user

    @staticmethod
    def need(request: web.Request, perm: str) -> Access:
        access: Access = request["access"]
        if not access.can(perm):
            raise ApiError("Этот раздел вам недоступен", 403)
        return access

    @staticmethod
    async def body(request: web.Request) -> dict:
        try:
            data = await request.json()
        except ValueError:
            raise ApiError("Некорректный запрос")
        if not isinstance(data, dict):
            raise ApiError("Некорректный запрос")
        return data

    def ticket_json(self, t, access: Access) -> dict:
        return ticket_dict(t, access, self.config)

    async def page(self, request: web.Request) -> web.Response:
        return web.FileResponse(PAGE, headers={"Cache-Control": "no-cache"})

    async def me(self, request: web.Request) -> web.Response:
        access: Access = request["access"]
        return web.json_response({
            "name": request["user"].full_name,
            "is_main": access.is_main,
            "perms": [p for p in PERMS if access.can(p)],
            "scoped": access.tickets_owner is not None,
            "max_tickets": MAX_TICKETS_AT_ONCE,
        })

    # ---------- билеты ----------

    async def tickets_create(self, request: web.Request) -> web.Response:
        access = self.need(request, "new")
        data = await self.body(request)
        fields = {k: clean(str(data.get(k, ""))) for k in ("full_name", "group", "university")}
        if not fields["full_name"]:
            raise ApiError("Введите фамилию и имя")
        # Группа, университет и телефон необязательны; телефон, если указан, должен быть верным.
        raw_phone = str(data.get("phone", "")).strip()
        phone = normalize_phone(raw_phone) if raw_phone else ""
        if raw_phone and not phone:
            raise ApiError("Не похоже на номер телефона")
        try:
            count = int(data.get("count", 1))
        except (TypeError, ValueError):
            count = 0
        if not 1 <= count <= MAX_TICKETS_AT_ONCE:
            raise ApiError(f"Количество билетов — от 1 до {MAX_TICKETS_AT_ONCE}")

        same = self.db.find_by_phone(phone) if phone else []
        if same and not data.get("force"):
            # Приложение переспросит и повторит запрос с force
            return web.json_response({
                "duplicate": [
                    {"code": ticket_code(self.config, t["id"]), "full_name": t["full_name"]}
                    for t in same
                ]
            })
        user: User = request["user"]
        tickets = [
            self.db.create_ticket(
                fields["full_name"], fields["group"], fields["university"], phone,
                user.id, user.full_name,
            )
            for _ in range(count)
        ]
        sent = True
        try:
            for n, t in enumerate(tickets, 1):
                head = "✅ Билет выдан" if count == 1 else f"✅ Билет {n} из {count}"
                await self.bot.send_document(
                    user.id,
                    await ticket_file(t, self.db, self.config, self.bot_username),
                    caption=f"{head}\n\n" + client_block(t, self.config),
                )
        except TelegramAPIError as e:
            log.warning("Не удалось отправить билет в чат: %s", e)
            sent = False
        return web.json_response({
            "tickets": [self.ticket_json(t, access) for t in tickets], "sent": sent,
        })

    async def tickets_list(self, request: web.Request) -> web.Response:
        access: Access = request["access"]
        owner = access.tickets_owner
        query = str((await self.body(request)).get("query", "")).strip()
        if query:
            found = self.db.search(query, owner, limit=LIST_LIMIT)
            m = re.fullmatch(r"(?:[^\W\d]+-?)?0*(\d{1,9})", query)
            by_id = self.db.get_ticket(int(m.group(1))) if m else None
            if (
                by_id
                and owner in (None, by_id["created_by"])
                and all(t["id"] != by_id["id"] for t in found)
            ):
                found.insert(0, by_id)
            total = len(found)
        else:
            found = self.db.all_tickets(owner)
            total = len(found)
        return web.json_response({
            "tickets": [self.ticket_json(t, access) for t in found[:LIST_LIMIT]],
            "total": total,
            "scanned": sum(1 for t in found if t["scan_count"]),
        })

    def visible_ticket(self, request: web.Request, ticket_id) -> tuple:
        access: Access = request["access"]
        try:
            t = self.db.get_ticket(int(ticket_id))
        except (TypeError, ValueError):
            t = None
        if not t:
            raise ApiError("Билет не найден (удалён?)", 404)
        if access.tickets_owner not in (None, t["created_by"]):
            raise ApiError("Этот билет выдан другим администратором", 403)
        return t, access

    async def tickets_action(self, request: web.Request) -> web.Response:
        data = await self.body(request)
        t, access = self.visible_ticket(request, data.get("id"))
        action = data.get("action")
        if action == "send":
            await self.bot.send_document(
                request["user"].id,
                await ticket_file(t, self.db, self.config, self.bot_username),
                caption=client_block(t, self.config),
            )
        elif action == "delete":
            if not can_delete(t, access):
                raise ApiError(
                    "Удалить билет может главный администратор или админ, который его выдал", 403
                )
            self.db.delete_ticket(t["id"])
            return web.json_response({"deleted": True})
        elif action == "unscan":
            if not access.is_main:
                raise ApiError("Доступно только главному администратору", 403)
            self.db.reset_scan(t["id"])
        else:
            raise ApiError("Неизвестное действие")
        return web.json_response({"ticket": self.ticket_json(self.db.get_ticket(t["id"]), access)})

    async def tickets_export(self, request: web.Request) -> web.Response:
        access: Access = request["access"]
        await self.bot.send_document(
            request["user"].id,
            export_file(self.db, self.config, access.tickets_owner),
            caption="Список билетов",
        )
        return web.json_response({"sent": True})

    async def scan(self, request: web.Request) -> web.Response:
        access = self.need(request, "scan")
        raw = str((await self.body(request)).get("data", "")).strip()
        token = extract_token(raw)
        t = None
        if token:
            t = self.db.get_ticket_by_token(token)
        else:
            m = re.fullmatch(r"#?(?:[^\W\d]+-?)?0*(\d{1,9})", raw)
            if not m:
                return web.json_response({"status": "foreign"})
            t = self.db.get_ticket(int(m.group(1)))
        if not t:
            return web.json_response({"status": "not_found"})
        t, first = self.db.register_scan(t["id"], request["user"].full_name)
        return web.json_response({
            "status": "ok" if first else "repeat",
            "ticket": self.ticket_json(t, access),
        })

    async def scan_photo(self, request: web.Request) -> web.Response:
        """Скан по фото QR-кода (если камера Telegram недоступна)."""
        self.need(request, "scan")
        raw = await request.read()
        text = await asyncio.to_thread(decode_qr, raw) if raw else None
        if not text:
            return web.json_response({"status": "unreadable"})
        token = extract_token(text)
        if not token:
            return web.json_response({"status": "foreign"})
        t = self.db.get_ticket_by_token(token)
        if not t:
            return web.json_response({"status": "not_found"})
        t, first = self.db.register_scan(t["id"], request["user"].full_name)
        return web.json_response({
            "status": "ok" if first else "repeat",
            "ticket": self.ticket_json(t, request["access"]),
        })

    async def events_poll(self, request: web.Request) -> web.Response:
        """Длинный опрос: приложение держит его открытым и получает уведомления
        (например, результаты сканирования из чата)."""
        data = await self.body(request)
        after = data.get("after")
        try:
            after = None if after is None else int(after)
            wait = max(1.0, min(float(data.get("wait", 20)), 25.0))
        except (TypeError, ValueError):
            raise ApiError("Некорректный запрос")
        events, last = await self.hub.poll(request["user"].id, after, wait)
        return web.json_response({"events": events, "last": last})

    async def ticket_image(self, request: web.Request) -> web.Response:
        data = await self.body(request)
        t, _ = self.visible_ticket(request, data.get("id"))
        png = await ticket_png(t, self.db, self.config, self.bot_username)
        jpeg = await asyncio.to_thread(self.to_jpeg, png)
        return web.Response(body=jpeg, content_type="image/jpeg")

    # ---------- массовый залив ----------

    def new_job(self, user_id: int) -> dict:
        now = time.time()
        for key in [k for k, j in self.jobs.items() if now - j["created"] > 3600]:
            del self.jobs[key]
        job = {
            "id": secrets.token_urlsafe(8), "user": user_id, "created": now,
            "state": "running", "done": 0, "total": 0, "error": "", "result": None,
        }
        self.jobs[job["id"]] = job
        return job

    def own_job(self, request: web.Request, job_id) -> dict:
        job = self.jobs.get(str(job_id))
        if not job or job["user"] != request["user"].id:
            raise ApiError("Задача не найдена — загрузите файл заново", 404)
        return job

    async def import_start(self, request: web.Request) -> web.Response:
        self.need(request, "new")
        raw = await request.read()
        try:
            text = await asyncio.to_thread(importer.extract_text, raw)
        except importer.ImportError_ as e:
            raise ApiError(str(e))
        parts = importer.chunks(text)
        if not parts:
            raise ApiError("В файле нет текста")
        job = self.new_job(request["user"].id)
        job["total"] = len(parts)
        task = asyncio.get_running_loop().create_task(self.run_import(job, parts))
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return web.json_response({"job": job["id"], "total": len(parts)})

    async def run_import(self, job: dict, parts: list[str]) -> None:
        key = groq_key(self.db, self.config)
        raw_records, warnings, ai_parts, rule_parts = [], [], 0, 0
        try:
            for part in parts:
                records = None
                if key:
                    try:
                        records = await ai.extract_records(
                            part, key, self.config.groq_text_model,
                            explicit=self.db.get_setting("groq_text_model"),
                        )
                        ai_parts += 1
                    except ai.AIError as e:
                        log.warning("Groq (импорт): %s", e)
                        msg = f"ИИ не справился с частью списка ({e}) — она разобрана по простым правилам"
                        if msg not in warnings:
                            warnings.append(msg)
                if records is None:
                    records = importer.rules_parse(part)
                    rule_parts += 1
                raw_records.extend(records)
                job["done"] += 1
            if not key:
                warnings.append(
                    "Ключ Groq не задан, поэтому список разобран по простым правилам: "
                    "одна строка — один клиент в порядке «ФИ, группа, университет, телефон»."
                )
            mode = "ai" if not rule_parts else ("rules" if not ai_parts else "mixed")
            result = importer.finalize(raw_records, MAX_TICKETS_AT_ONCE, mode, warnings)
            job["result"] = {
                "records": self.annotate(result.records),
                "mode": result.mode,
                "warnings": result.warnings,
                "dropped_duplicates": result.dropped_duplicates,
                "truncated": result.truncated,
                "max_records": importer.MAX_RECORDS,
            }
            job["state"] = "done"
        except Exception:
            log.exception("Ошибка разбора файла")
            job["state"], job["error"] = "failed", "Не удалось разобрать файл"

    def annotate(self, records: list[dict]) -> list[dict]:
        """Добавляет к записям проблемы и признак «такой клиент уже есть в базе»."""
        out = []
        for rec in records:
            same = self.db.find_by_phone(rec["phone"]) if rec["phone"].startswith("+") else []
            already = any(t["full_name"].casefold() == rec["full_name"].casefold() for t in same)
            out.append({
                **rec,
                "issues": importer.issues_of(rec),
                "already": already,
                "phone_used": bool(same) and not already,
            })
        return out

    async def import_status(self, request: web.Request) -> web.Response:
        self.need(request, "new")
        job = self.own_job(request, (await self.body(request)).get("job"))
        data = {"state": job["state"], "done": job["done"], "total": job["total"], "error": job["error"]}
        if job["state"] == "done":
            data.update(job["result"])
        return web.json_response(data)

    async def import_create(self, request: web.Request) -> web.Response:
        access = self.need(request, "new")
        data = await self.body(request)
        raw_records = data.get("records")
        if not isinstance(raw_records, list) or not raw_records:
            raise ApiError("Нет клиентов для выдачи билетов")
        if len(raw_records) > importer.MAX_RECORDS:
            raise ApiError(f"За один раз — не больше {importer.MAX_RECORDS} клиентов")
        records = []
        for n, raw in enumerate(raw_records, 1):
            if not isinstance(raw, dict):
                raise ApiError("Некорректный запрос")
            rec = importer.normalize_record(raw, MAX_TICKETS_AT_ONCE)
            problems = importer.issues_of(rec)
            if problems:
                raise ApiError(f"Строка {n} ({rec['full_name'] or 'без имени'}): {', '.join(problems)}")
            records.append(rec)
        if sum(r["count"] for r in records) > importer.MAX_TICKETS_TOTAL:
            raise ApiError(f"За один раз — не больше {importer.MAX_TICKETS_TOTAL} билетов")

        user: User = request["user"]
        tickets = []
        for rec in records:
            for _ in range(rec["count"]):
                tickets.append(self.db.create_ticket(
                    rec["full_name"], rec["group"], rec["university"], rec["phone"],
                    user.id, user.full_name,
                ))
        task = asyncio.get_running_loop().create_task(self.deliver_import(user.id, tickets))
        self.background.add(task)  # ссылка, чтобы задачу не удалил сборщик мусора
        task.add_done_callback(self.background.discard)
        return web.json_response({
            "created": len(tickets),
            "clients": len(records),
            "first": ticket_code(self.config, tickets[0]["id"]),
            "last": ticket_code(self.config, tickets[-1]["id"]),
            "tickets": [self.ticket_json(t, access) for t in tickets[:50]],
        })

    async def deliver_import(self, user_id: int, tickets: list) -> None:
        """Рисует билеты и присылает админу в чат архивами (лимит Telegram — 50 МБ на файл)
        вместе со списком. Выполняется в фоне, чтобы запрос не висел минутами."""
        limit = 40 * 1024 * 1024
        parts: list[bytes] = []
        buf = io.BytesIO()
        zf, size = zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED), 0
        try:
            for t in tickets:
                png = await ticket_png(t, self.db, self.config, self.bot_username)
                if size + len(png) > limit and size:
                    zf.close()
                    parts.append(buf.getvalue())
                    buf = io.BytesIO()
                    zf, size = zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED), 0
                name = re.sub(r'[\\/:*?"<>|]+', "", t["full_name"]).strip()[:40]
                zf.writestr(f"{ticket_code(self.config, t['id'])} {name}.png", png)
                size += len(png)
            zf.close()
            parts.append(buf.getvalue())
            total = len(parts)
            for i, data in enumerate(parts, 1):
                await self.bot.send_document(
                    user_id, BufferedInputFile(data, filename=f"tickets_{i}_of_{total}.zip"),
                    caption=f"Билеты ({len(tickets)} шт.): архив {i} из {total}",
                )
            await self.bot.send_document(
                user_id,
                BufferedInputFile(export_txt(tickets, self.config), filename="issued.txt"),
                caption="Список выданных билетов",
            )
            self.hub.publish(user_id, {
                "type": "notice", "text": f"Билеты ({len(tickets)} шт.) отправлены вам в чат с ботом",
            })
        except Exception:
            log.exception("Не удалось отправить билеты массового залива")
            self.hub.publish(user_id, {
                "type": "notice", "error": True,
                "text": "Билеты созданы, но отправить файлы в чат не удалось. "
                        "Откройте их в списке и нажмите «Прислать в чат».",
            })

    async def stats(self, request: web.Request) -> web.Response:
        self.need(request, "stats")
        tickets = self.db.all_tickets()
        scanned = sum(1 for t in tickets if t["scan_count"])

        def top(key: str) -> list:
            return [
                {"name": name, "count": n}
                for name, n in Counter(t[key] for t in tickets if t[key]).most_common(10)
            ]

        return web.json_response({
            "total": len(tickets),
            "scanned": scanned,
            "waiting": len(tickets) - scanned,
            "repeats": sum(1 for t in tickets if t["scan_count"] > 1),
            "by_admin": top("created_by_name"),
            "by_university": top("university"),
        })

    # ---------- администраторы ----------

    def admin_json(self, a, access: Access) -> dict:
        return {
            "id": a["id"],
            "label": admin_label(a),
            "username": a["username"],
            "user_id": a["user_id"],
            "perms": [p for p in PERMS if p in parse_perms(a["perms"])],
            "is_self": a["user_id"] == access.user_id,
        }

    def admins_payload(self, access: Access) -> dict:
        return {
            "admins": [self.admin_json(a, access) for a in self.db.list_admins()],
            "main_ids": sorted(self.config.main_admin_ids),
            "all_perms": [{"key": k, "title": v, "grantable": access.can(k)} for k, v in PERMS.items()],
        }

    def editable_admin(self, request: web.Request, admin_id):
        access = self.need(request, "admins")
        try:
            a = self.db.get_admin(int(admin_id))
        except (TypeError, ValueError):
            a = None
        if not a:
            raise ApiError("Админ не найден", 404)
        if a["user_id"] == access.user_id:
            raise ApiError("Свои разрешения менять нельзя", 403)
        return a, access

    async def admins_list(self, request: web.Request) -> web.Response:
        return web.json_response(self.admins_payload(self.need(request, "admins")))

    async def admins_add(self, request: web.Request) -> web.Response:
        access = self.need(request, "admins")
        raw = str((await self.body(request)).get("value", "")).strip()
        user_id = username = None
        nick = re.fullmatch(r"(?:https?://)?(?:t\.me/|@)?([A-Za-z][A-Za-z0-9_]{3,31})", raw)
        if re.fullmatch(r"\d{1,15}", raw):
            user_id = int(raw)
        elif nick:
            username = nick.group(1).lower()
        else:
            raise ApiError("Не похоже на ник или ID")
        if (user_id and self.config.is_main(user_id)) or self.db.admin_exists(user_id, username):
            raise ApiError("Этот человек уже администратор")
        name = f"@{username}" if username else f"ID {user_id}"
        if user_id:
            try:  # получится, если человек уже открывал бота
                chat = await self.bot.get_chat(user_id)
                name = chat.full_name or name
                username = chat.username.lower() if chat.username else None
            except TelegramAPIError:
                pass
        perms = dump_perms({p for p in parse_perms(DEFAULT_PERMS) if access.can(p)})
        admin_id = self.db.add_admin(user_id, username, name, perms)
        await notify_admin(
            self.bot, self.db.get_admin(admin_id),
            "Вас назначили администратором билетов. Откройте приложение: кнопка «Mini App» слева от поля ввода.",
        )
        return web.json_response({**self.admins_payload(access), "added": admin_id})

    async def admins_toggle(self, request: web.Request) -> web.Response:
        data = await self.body(request)
        a, access = self.editable_admin(request, data.get("id"))
        key = data.get("perm")
        if key not in PERMS or not access.can(key):
            raise ApiError("Вы не можете выдать разрешение, которого нет у вас", 403)
        perms = set(parse_perms(a["perms"]))
        perms.symmetric_difference_update({key})
        self.db.set_admin_perms(a["id"], dump_perms(perms))
        return web.json_response(self.admins_payload(access))

    async def admins_remove(self, request: web.Request) -> web.Response:
        a, access = self.editable_admin(request, (await self.body(request)).get("id"))
        self.db.remove_admin(a["id"])
        if a["user_id"]:
            await sync_menu_button(self.bot, a["user_id"], admin=False)
        return web.json_response(self.admins_payload(access))

    # ---------- дизайн билета ----------

    def design_payload(self, **extra) -> dict:
        path, box, name = current_template(self.db, self.config)
        return {
            "custom": path == self.config.template_path, "box": list(box),
            "name": name, **extra,
        }

    async def design_state(self, request: web.Request) -> web.Response:
        self.need(request, "design")
        return web.json_response(self.design_payload())

    async def pending_payload(self, previous=None, hint=None, previous_name=None) -> dict:
        size = await asyncio.to_thread(image_size, self.config.pending_path)
        box, name, source, problem = await locate_box(
            self.config, self.db, size, previous, hint, previous_name
        )
        if box is None:  # ставим в центр — админ поправит на картинке
            side = min(size) // 3
            box = clamp_box(((size[0] - side) / 2, (size[1] - side) / 2, side), *size)
            source = "Место не найдено — укажите его на картинке"
        return {
            "box": list(box), "name": name, "source": source, "problem": problem,
            "width": size[0], "height": size[1],
        }

    async def design_upload(self, request: web.Request) -> web.Response:
        self.need(request, "design")
        raw = await request.read()
        if not raw:
            raise ApiError("Файл пустой")
        try:
            await asyncio.to_thread(save_template, raw, self.config.pending_path)
        except (UnidentifiedImageError, OSError):
            raise ApiError("Не удалось открыть картинку. Нужен PNG или JPG.")
        return web.json_response(await self.pending_payload())

    def pending_size(self) -> tuple[int, int]:
        if not self.config.pending_path.exists():
            raise ApiError("Черновик дизайна не найден, загрузите картинку заново", 404)
        return image_size(self.config.pending_path)

    def pending_box(self, data: dict):
        size = self.pending_size()
        try:
            raw = [float(v) for v in data.get("box", [])]
        except (TypeError, ValueError):
            raw = []
        if len(raw) != 3:
            raise ApiError("Не указано место под QR")
        return clamp_box(tuple(raw), *size)

    def pending_name(self, data: dict) -> dict | None:
        """Раскладка ФИ из запроса приложения: рамка, выравнивание, цвет; None — ФИ отключено."""
        entry = data.get("name")
        if not isinstance(entry, dict) or not entry.get("enabled"):
            return None
        try:
            box = clamp_name_box([float(v) for v in entry.get("box", [])], *self.pending_size())
        except (TypeError, ValueError):
            raise ApiError("Некорректное место под ФИ")
        color = entry.get("color")
        return {
            "enabled": True, "box": box,
            "align": entry.get("align") if entry.get("align") in ALIGNS else "left",
            "color": color if isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color) else None,
        }

    async def design_retry(self, request: web.Request) -> web.Response:
        self.need(request, "design")
        data = await self.body(request)
        previous = self.pending_box(data)
        hint = str(data.get("hint", "")).strip() or None
        return web.json_response(
            await self.pending_payload(previous, hint, self.pending_name(data))
        )

    async def design_save(self, request: web.Request) -> web.Response:
        self.need(request, "design")
        data = await self.body(request)
        box, name = self.pending_box(data), self.pending_name(data)
        shutil.move(self.config.pending_path, self.config.template_path)
        self.db.set_setting("template_box", list(box))
        self.db.set_setting("template_name", name or {"enabled": False})
        return web.json_response(self.design_payload())

    async def design_cancel(self, request: web.Request) -> web.Response:
        self.need(request, "design")
        self.config.pending_path.unlink(missing_ok=True)
        return web.json_response(self.design_payload())

    async def design_default(self, request: web.Request) -> web.Response:
        self.need(request, "design")
        self.db.delete_setting("template_box")
        self.db.delete_setting("template_name")
        self.config.template_path.unlink(missing_ok=True)
        return web.json_response(self.design_payload())

    async def design_image(self, request: web.Request) -> web.Response:
        """Предпросмотр: текущий дизайн или черновик с QR и примером ФИ в указанных местах."""
        self.need(request, "design")
        data = await self.body(request)
        if data.get("pending"):
            path, box, name = self.config.pending_path, self.pending_box(data), self.pending_name(data)
        else:
            path, box, name = current_template(self.db, self.config)
        jpeg = await asyncio.to_thread(self.preview_jpeg, path, box, name)
        return web.Response(body=jpeg, content_type="image/jpeg")

    def preview_jpeg(self, path, box, name=None) -> bytes:
        return self.to_jpeg(render_preview(path, box, self.bot_username, name))

    @staticmethod
    def to_jpeg(png: bytes) -> bytes:
        """Уменьшенный JPEG: картинки грузятся через туннель, PNG был бы тяжёлым."""
        with Image.open(io.BytesIO(png)) as src:
            img = Image.new("RGB", src.size, "#2b1612")
            img.paste(src, mask=src.getchannel("A") if "A" in src.getbands() else None)
        img.thumbnail((1400, 1400), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=86)
        return out.getvalue()

    # ---------- настройки (только главный администратор) ----------

    def settings_payload(self) -> dict:
        key = groq_key(self.db, self.config)
        return {
            "groq_set": bool(key),
            "groq_hint": mask(key) if key else "",
            "vision_model": self.db.get_setting("groq_vision_model") or "",
            "text_model": self.db.get_setting("groq_text_model") or "",
            "vision_auto": ai.remembered("vision") or "",
            "text_auto": ai.remembered("text") or "",
            "vision_default": self.config.groq_model,
            "text_default": self.config.groq_text_model,
        }

    @staticmethod
    def need_main(request: web.Request) -> None:
        if not request["access"].is_main:
            raise ApiError("Доступно только главному администратору", 403)

    async def settings_get(self, request: web.Request) -> web.Response:
        self.need_main(request)
        return web.json_response(self.settings_payload())

    async def settings_set(self, request: web.Request) -> web.Response:
        self.need_main(request)
        data = await self.body(request)
        if "groq_api_key" in data:
            key = str(data["groq_api_key"]).strip()
            if key and not re.fullmatch(r"[A-Za-z0-9_\-]{20,200}", key):
                raise ApiError("Не похоже на ключ Groq")
            if key:
                self.db.set_setting("groq_api_key", key)
            else:
                self.db.delete_setting("groq_api_key")
        for field, kind in (("vision_model", "vision"), ("text_model", "text")):
            if field in data:
                model = str(data[field]).strip()
                if model and not re.fullmatch(r"[A-Za-z0-9._/:\-]{2,120}", model):
                    raise ApiError("Название модели: латинские буквы, цифры и символы . _ / : -")
                if model:
                    self.db.set_setting(f"groq_{field}", model)
                else:
                    self.db.delete_setting(f"groq_{field}")
                ai.forget(kind)   # выбор вручную важнее найденного ранее
        return web.json_response(self.settings_payload())

    async def settings_check(self, request: web.Request) -> web.Response:
        """Кнопка «Проверить ИИ»: ключ, список моделей, ответ текстовой и картиночной модели."""
        self.need_main(request)
        key = groq_key(self.db, self.config)
        if not key:
            raise ApiError("Сначала задайте ключ Groq")
        result = await ai.check(
            key, self.config.groq_model, self.config.groq_text_model,
            self.db.get_setting("groq_vision_model"), self.db.get_setting("groq_text_model"),
        )
        result["models"] = result["models"][:40]
        return web.json_response(result)

    # ---------- сброс ----------

    async def reset(self, request: web.Request) -> web.Response:
        self.need(request, "reset")
        if self.db.all_tickets():
            # Сначала резервная копия: если она не отправится, ничего не удаляем.
            await self.bot.send_document(
                request["user"].id,
                export_file(self.db, self.config),
                caption="Резервная копия списка перед сбросом",
            )
        return web.json_response({"deleted": self.db.reset_tickets()})
