"""Веб-сервер мини-приложения: отдаёт страницу и JSON-API ко всем разделам бота.

Каждый запрос к /api/ подписан данными Telegram (initData): по ним сервер
узнаёт пользователя и проверяет его разрешения так же, как бот в чате.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import secrets
import shutil
import time
import zipfile
from collections import Counter
from datetime import datetime
from html import escape
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, User
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web
from PIL import Image, UnidentifiedImageError

from .access import DEFAULT_PERMS, PERMS, Access, dump_perms, parse_perms, resolve_access
from .config import BASE_DIR, Config
from .db import Database, now_iso
from .importer import MAX_TICKETS_AT_ONCE, clean, normalize_phone
from . import ai, importer, receipts, shop, support
from .events import Hub
from .imaging import (
    ALIGNS, clamp_box, clamp_name_box, decode_qr, extract_token, image_size, save_template,
)
from .service import (
    admin_label, current_template, design_paths, export_file, groq_key, locate_box, mask,
    notify_admin, render_preview, sync_menu_button, ticket_dict, ticket_file, ticket_png,
)
from .shop import ShopError, money
from .ui import can_delete, client_block, export_txt, fmt_dt, ticket_code

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
            "reset/info": self.reset_info,
            "types/list": self.types_list,
            "types/save": self.types_save,
            "types/delete": self.types_delete,
            "types/move": self.types_move,
            "payments/list": self.payments_list,
            "payments/get": self.payments_get,
            "payments/receipt": self.payments_receipt,
            "payments/receipt_file": self.payments_receipt_file,
            "payments/topup": self.payments_topup,
            "support/threads": self.support_threads,
            "support/thread": self.support_thread,
            "support/messages": self.support_messages,
            "support/send": self.support_send,
            "support/send_file": self.support_send_file,
            "support/file": self.support_file,
            "support/file_to_me": self.support_file_to_me,
            "support/close": self.support_close,
            "support/issue": self.support_issue,
            "support/clear": self.support_clear,
            "payments/approve": self.payments_approve,
            "payments/reject": self.payments_reject,
            "payments/resend": self.payments_resend,
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
            "pending_payments": self.db.order_counts().get("pending", 0) if access.can("payments") else 0,
            "support_unread": self.db.support_counts()["unread"] if access.can("support") else 0,
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
        type_row = None
        if data.get("type_id"):
            try:
                type_row = self.db.get_type(int(data["type_id"]))
            except (TypeError, ValueError):
                type_row = None
            if not type_row:
                raise ApiError("Тип билета не найден", 404)
            left = self.db.type_left(type_row["id"])
            if left is not None and left < count and not data.get("force"):
                return web.json_response({"over_limit": {"left": left, "title": type_row["title"]}})
        user: User = request["user"]
        tickets = [
            self.db.create_ticket(
                fields["full_name"], fields["group"], fields["university"], phone,
                user.id, user.full_name,
                type_row["id"] if type_row else None, type_row["title"] if type_row else "",
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
            "revenue": self.db.revenue(),
            "revenue_text": money(self.db.revenue()),
            "orders": self.db.order_counts(),
            "types": [
                {
                    "title": i["row"]["title"], "price": money(i["row"]["price"]), "sold": i["sold"],
                    "pending": i["pending"], "quantity": i["row"]["quantity"], "left": i["left"],
                    "revenue": money(i["revenue"]),
                }
                for i in self.db.types_overview()
            ],
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
    # Дизайн бывает общий (для билетов без типа и типов без своего дизайна) и свой у каждого типа:
    # во всех запросах необязательное поле type_id указывает, чей дизайн правим.

    def design_ctx(self, raw_type) -> tuple[int | None, Path, Path]:
        try:
            type_id = int(raw_type) if raw_type not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            raise ApiError("Некорректный тип билета")
        if type_id and not self.db.get_type(type_id):
            raise ApiError("Тип билета не найден", 404)
        final, pending = design_paths(self.config, type_id)
        return type_id, final, pending

    def design_payload(self, type_id: int | None = None, **extra) -> dict:
        path, box, name = current_template(self.db, self.config, type_id)
        own = path == design_paths(self.config, type_id)[0]
        return {
            "custom": own, "inherited": bool(type_id) and not own,
            "box": list(box), "name": name, "type_id": type_id, **extra,
        }

    async def design_state(self, request: web.Request) -> web.Response:
        self.need(request, "design")
        type_id, _, _ = self.design_ctx((await self.body(request)).get("type_id"))
        return web.json_response(self.design_payload(type_id))

    async def pending_payload(
        self, pending: Path, previous=None, hint=None, previous_name=None
    ) -> dict:
        size = await asyncio.to_thread(image_size, pending)
        box, name, source, problem = await locate_box(
            self.config, self.db, size, previous, hint, previous_name, path=pending
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
        _, _, pending = self.design_ctx(request.query.get("type_id"))
        raw = await request.read()
        if not raw:
            raise ApiError("Файл пустой")
        try:
            await asyncio.to_thread(save_template, raw, pending)
        except (UnidentifiedImageError, OSError):
            raise ApiError("Не удалось открыть картинку. Нужен PNG или JPG.")
        return web.json_response(await self.pending_payload(pending))

    @staticmethod
    def pending_size(pending: Path) -> tuple[int, int]:
        if not pending.exists():
            raise ApiError("Черновик дизайна не найден, загрузите картинку заново", 404)
        return image_size(pending)

    def pending_box(self, data: dict, pending: Path):
        size = self.pending_size(pending)
        try:
            raw = [float(v) for v in data.get("box", [])]
        except (TypeError, ValueError):
            raw = []
        if len(raw) != 3:
            raise ApiError("Не указано место под QR")
        return clamp_box(tuple(raw), *size)

    def pending_name(self, data: dict, pending: Path) -> dict | None:
        """Раскладка ФИ из запроса приложения: рамка, выравнивание, цвет; None — ФИ отключено."""
        entry = data.get("name")
        if not isinstance(entry, dict) or not entry.get("enabled"):
            return None
        try:
            box = clamp_name_box([float(v) for v in entry.get("box", [])], *self.pending_size(pending))
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
        _, _, pending = self.design_ctx(data.get("type_id"))
        previous = self.pending_box(data, pending)
        hint = str(data.get("hint", "")).strip() or None
        return web.json_response(
            await self.pending_payload(pending, previous, hint, self.pending_name(data, pending))
        )

    async def design_save(self, request: web.Request) -> web.Response:
        self.need(request, "design")
        data = await self.body(request)
        type_id, final, pending = self.design_ctx(data.get("type_id"))
        box, name = self.pending_box(data, pending), self.pending_name(data, pending)
        shutil.move(pending, final)
        if type_id:
            self.db.update_type(
                type_id, has_design=1, design_box=json.dumps(list(box)),
                design_name=json.dumps(name or {"enabled": False}),
            )
        else:
            self.db.set_setting("template_box", list(box))
            self.db.set_setting("template_name", name or {"enabled": False})
        return web.json_response(self.design_payload(type_id))

    async def design_cancel(self, request: web.Request) -> web.Response:
        self.need(request, "design")
        type_id, _, pending = self.design_ctx((await self.body(request)).get("type_id"))
        pending.unlink(missing_ok=True)
        return web.json_response(self.design_payload(type_id))

    async def design_default(self, request: web.Request) -> web.Response:
        """Вернуть стандартный дизайн (для типа — убрать его свой дизайн: будет общий)."""
        self.need(request, "design")
        type_id, final, _ = self.design_ctx((await self.body(request)).get("type_id"))
        if type_id:
            self.db.update_type(type_id, has_design=0, design_box=None, design_name=None)
        else:
            self.db.delete_setting("template_box")
            self.db.delete_setting("template_name")
        final.unlink(missing_ok=True)
        return web.json_response(self.design_payload(type_id))

    async def design_image(self, request: web.Request) -> web.Response:
        """Предпросмотр: текущий дизайн или черновик с QR и примером ФИ в указанных местах."""
        self.need(request, "design")
        data = await self.body(request)
        type_id, _, pending = self.design_ctx(data.get("type_id"))
        if data.get("pending"):
            path, box, name = pending, self.pending_box(data, pending), self.pending_name(data, pending)
        else:
            path, box, name = current_template(self.db, self.config, type_id)
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

    # ---------- типы билетов ----------

    @staticmethod
    def pay_url_value(raw) -> str:
        value = str(raw or "").strip()
        if value and not re.fullmatch(r'https://[^\s<>"]{4,500}', value):
            raise ApiError("Ссылка оплаты должна начинаться с https://")
        return value

    def type_json(self, item: dict) -> dict:
        t = item["row"]
        return {
            "id": t["id"], "title": t["title"], "description": t["description"],
            "price": t["price"], "price_text": money(t["price"]), "quantity": t["quantity"],
            "active": bool(t["active"]), "pay_url": t["pay_url"], "pay_bin": t["pay_bin"],
            "has_design": bool(t["has_design"]),
            "sold": item["sold"], "pending": item["pending"], "left": item["left"],
            "revenue": item["revenue"], "revenue_text": money(item["revenue"]),
        }

    async def types_list(self, request: web.Request) -> web.Response:
        return web.json_response({
            "types": [self.type_json(i) for i in self.db.types_overview()],
            "default_pay_url": self.db.get_setting("shop_pay_url") or "",
            "shop_enabled": shop.shop_enabled(self.db),
        })

    async def types_save(self, request: web.Request) -> web.Response:
        self.need_main(request)
        data = await self.body(request)
        title = clean(str(data.get("title", "")), 60)
        if not title:
            raise ApiError("Введите название билета")
        try:
            price = int(data.get("price"))
        except (TypeError, ValueError):
            raise ApiError("Цена — целое число тенге")
        if not 1 <= price <= 10_000_000:
            raise ApiError("Цена — от 1 до 10 000 000 ₸")
        raw_qty = data.get("quantity")
        if raw_qty in (None, ""):
            quantity = None
        else:
            try:
                quantity = int(raw_qty)
            except (TypeError, ValueError):
                raise ApiError("Количество — целое число (или пусто — без лимита)")
            if not 0 <= quantity <= 1_000_000:
                raise ApiError("Количество — от 0 до 1 000 000")
        pay_bin = str(data.get("pay_bin", "")).strip()
        if pay_bin and not re.fullmatch(r"\d{12}", pay_bin):
            raise ApiError("ИИН/БИН — 12 цифр")
        fields = {
            "title": title, "description": clean(str(data.get("description", "")), 300),
            "price": price, "quantity": quantity, "pay_url": self.pay_url_value(data.get("pay_url")),
            "pay_bin": pay_bin, "active": int(bool(data.get("active", True))),
        }
        if data.get("id"):
            if not self.db.get_type(int(data["id"])):
                raise ApiError("Тип билета не найден", 404)
            self.db.update_type(int(data["id"]), **fields)
            type_id = int(data["id"])
        else:
            type_id = self.db.add_type(
                fields["title"], fields["description"], price, quantity, fields["pay_url"],
                bool(fields["active"]), pay_bin,
            )
        return web.json_response({"id": type_id, **await self._types_payload()})

    async def _types_payload(self) -> dict:
        return {"types": [self.type_json(i) for i in self.db.types_overview()]}

    async def types_delete(self, request: web.Request) -> web.Response:
        self.need_main(request)
        try:
            type_id = int((await self.body(request)).get("id"))
        except (TypeError, ValueError):
            raise ApiError("Тип билета не найден", 404)
        if not self.db.get_type(type_id):
            raise ApiError("Тип билета не найден", 404)
        if self.db.type_usage(type_id):
            raise ApiError("С этим типом уже есть билеты или заказы — его можно только скрыть (выключить).")
        self.db.delete_type(type_id)
        for path in design_paths(self.config, type_id):
            path.unlink(missing_ok=True)
        return web.json_response(await self._types_payload())

    async def types_move(self, request: web.Request) -> web.Response:
        """Порядок типов в каталоге клиента: сдвиг вверх (-1) или вниз (1)."""
        self.need_main(request)
        data = await self.body(request)
        ids = [t["id"] for t in self.db.list_types()]
        try:
            i, step = ids.index(int(data.get("id"))), (-1 if int(data.get("dir")) < 0 else 1)
        except (TypeError, ValueError):
            raise ApiError("Тип билета не найден", 404)
        if 0 <= i + step < len(ids):
            ids[i], ids[i + step] = ids[i + step], ids[i]
        for n, type_id in enumerate(ids):
            self.db.update_type(type_id, sort=n)
        return web.json_response(await self._types_payload())

    # ---------- оплаты на проверку ----------

    def order_json(self, o, overview=None, detail: bool = False) -> dict:
        ticket = self.db.get_ticket(o["ticket_id"]) if o["ticket_id"] else None
        rows = self.db.receipts_of(o["id"], include_void=True)
        paid, unknown, short = shop.coverage(self.db, o)
        receipts_json = []
        for r in rows:
            info = receipts.Receipt.from_dict(json.loads(r["info"] or "{}"))
            receipts_json.append({
                "id": r["id"], "void": bool(r["void"]),
                "amount": money(info.amount) if info.amount is not None else "", "number": info.number,
                "buyer": info.buyer, "paid_at": info.paid_at.replace("T", " "), "method": info.method,
                "seller": info.seller, "pages": info.pages,
            })
        out = {
            "id": o["id"], "status": o["status"], "type_id": o["type_id"], "type_title": o["type_title"],
            "price": o["price"], "price_text": money(o["price"]), "full_name": o["full_name"],
            "phone": o["phone"], "university": o["university"], "username": o["username"],
            "tg_name": o["tg_name"], "user_id": o["user_id"],
            "submitted_at": fmt_dt(o["submitted_at"], self.config) if o["submitted_at"] else "",
            "reviewed_at": fmt_dt(o["reviewed_at"], self.config) if o["reviewed_at"] else "",
            "reviewed_by": o["reviewed_by"], "reject_reason": o["reject_reason"],
            "ticket_id": o["ticket_id"], "ticket_code": ticket_code(self.config, o["ticket_id"]) if ticket else "",
            "delivered": bool(o["delivered"]), "has_receipt": any(not r["void"] for r in rows),
            "paid": paid, "paid_text": money(paid), "short": short, "short_text": money(short),
            "unknown_amounts": unknown, "receipts": receipts_json,
        }
        flags = shop.review_flags(self.db, self.config, o, overview) if o["status"] in ("topup", "pending", "rejected") or detail else []
        out["worst"] = "bad" if any(f["level"] == "bad" for f in flags) else "warn" if any(f["level"] == "warn" for f in flags) else "ok"
        if detail:
            out["flags"] = flags
        return out

    def get_order(self, data: dict):
        try:
            order = self.db.get_order(int(data.get("id")))
        except (TypeError, ValueError):
            order = None
        if not order or order["status"] not in ("topup", "pending", "approved", "rejected"):
            raise ApiError("Заказ не найден", 404)
        return order

    async def payments_list(self, request: web.Request) -> web.Response:
        self.need(request, "payments")
        status = str((await self.body(request)).get("status", "pending"))
        if status not in ("pending", "topup", "approved", "rejected", "all"):
            raise ApiError("Некорректный фильтр")
        overview = self.db.types_overview()
        orders = self.db.orders_by_status(None if status == "all" else status, 150)
        counts = self.db.order_counts()
        return web.json_response({
            "orders": [self.order_json(o, overview) for o in orders],
            "counts": {k: counts.get(k, 0) for k in ("pending", "topup", "approved", "rejected")},
        })

    async def payments_get(self, request: web.Request) -> web.Response:
        self.need(request, "payments")
        order = self.get_order(await self.body(request))
        return web.json_response({"order": self.order_json(order, detail=True)})

    def pick_receipt(self, order, data: dict):
        """Чек заказа: указанный в запросе, иначе последний засчитанный."""
        rows = self.db.receipts_of(order["id"], include_void=True)
        if data.get("receipt_id"):
            rows = [r for r in rows if r["id"] == data.get("receipt_id")]
        else:
            rows = [r for r in rows if not r["void"]] or rows
        path = self.config.receipts_dir / rows[-1]["file"] if rows else None
        if not path or not path.exists():
            raise ApiError("Файл чека не найден", 404)
        return path

    async def payments_receipt(self, request: web.Request) -> web.Response:
        """Страница PDF-чека картинкой (внутри Telegram PDF не открывается)."""
        self.need(request, "payments")
        data = await self.body(request)
        path = self.pick_receipt(self.get_order(data), data)
        try:
            page = int(data.get("page", 0))
        except (TypeError, ValueError):
            page = 0
        raw = await asyncio.to_thread(path.read_bytes)
        try:
            jpeg = await asyncio.to_thread(receipts.render_page, raw, page)
        except receipts.ReceiptError as e:
            raise ApiError(str(e))
        return web.Response(body=jpeg, content_type="image/jpeg")

    async def payments_receipt_file(self, request: web.Request) -> web.Response:
        """Присылает оригиналы PDF-чеков заказа админу в чат."""
        self.need(request, "payments")
        order = self.get_order(await self.body(request))
        rows = [r for r in self.db.receipts_of(order["id"]) if (self.config.receipts_dir / r["file"]).exists()]
        if not rows:
            raise ApiError("Файл чека не найден", 404)
        for n, r in enumerate(rows, 1):
            await self.bot.send_document(
                request["user"].id,
                BufferedInputFile(
                    await asyncio.to_thread((self.config.receipts_dir / r["file"]).read_bytes),
                    filename=f"check_order_{order['id']}_{n}.pdf",
                ),
                caption=f"Чек {n} из {len(rows)} по заказу №{order['id']} · {order['full_name'] or order['tg_name']}",
            )
        return web.json_response({"sent": len(rows)})

    async def payments_topup(self, request: web.Request) -> web.Response:
        """Недоплата: просит клиента доплатить остаток (или напоминает, если уже просили)."""
        self.need(request, "payments")
        order = self.get_order(await self.body(request))
        try:
            if order["status"] != "topup":
                order, _ = shop.request_topup(self.db, order["id"], request["user"].full_name)
        except ShopError as e:
            raise ApiError(str(e), 409)
        sent = await shop.notify_topup(
            self.bot, self.db, order, "Организатор проверил оплату: внесённой суммы не хватает."
        )
        return web.json_response({"sent": sent, "order": self.order_json(self.db.get_order(order["id"]), detail=True)})

    async def payments_approve(self, request: web.Request) -> web.Response:
        access = self.need(request, "payments")
        order = self.get_order(await self.body(request))
        try:
            ticket, order = shop.approve_order(self.db, order["id"], request["user"].full_name)
        except ShopError as e:
            raise ApiError(str(e), 409)
        delivered = await shop.deliver_ticket(
            self.bot, self.db, self.config, self.bot_username, order, ticket
        )
        return web.json_response({
            "order": self.order_json(self.db.get_order(order["id"]), detail=True),
            "ticket": self.ticket_json(ticket, access), "delivered": delivered,
        })

    async def payments_reject(self, request: web.Request) -> web.Response:
        self.need(request, "payments")
        data = await self.body(request)
        order = self.get_order(data)
        try:
            order = shop.reject_order(
                self.db, order["id"], request["user"].full_name, str(data.get("reason", ""))
            )
        except ShopError as e:
            raise ApiError(str(e), 409)
        await shop.notify_rejected(self.bot, order, order["reject_reason"])
        return web.json_response({"order": self.order_json(order, detail=True)})

    async def payments_resend(self, request: web.Request) -> web.Response:
        """Повторно присылает клиенту билет (если не дошёл или клиент потерял)."""
        self.need(request, "payments")
        order = self.get_order(await self.body(request))
        ticket = self.db.get_ticket(order["ticket_id"]) if order["status"] == "approved" and order["ticket_id"] else None
        if not ticket:
            raise ApiError("У этого заказа нет выданного билета", 409)
        delivered = await shop.deliver_ticket(
            self.bot, self.db, self.config, self.bot_username, order, ticket
        )
        return web.json_response({
            "delivered": delivered, "order": self.order_json(self.db.get_order(order["id"]), detail=True),
        })

    # ---------- поддержка: чаты с клиентами ----------

    def thread_json(self, t) -> dict:
        return {
            "user_id": t["user_id"], "name": t["name"] or "Без имени", "username": t["username"] or "",
            "status": t["status"], "unread": t["unread"], "assigned_to": t["assigned_to"],
            "last_text": t["last_text"], "last_dir": t["last_dir"],
            "last_at": fmt_dt(t["last_at"], self.config) if t["last_at"] else "",
        }

    def message_json(self, m) -> dict:
        previewable = bool(m["file_id"]) and (
            m["kind"] == "photo" or (m["mime"] or "").startswith("image/")
            or (m["mime"] == "application/pdf") or (m["file_name"] or "").lower().endswith(".pdf")
        )
        return {
            "id": m["id"], "direction": m["direction"], "kind": m["kind"], "text": m["text"],
            "admin_name": m["admin_name"], "time": fmt_dt(m["created_at"], self.config),
            "day": fmt_dt(m["created_at"], self.config)[:10],
            "buttons": json.loads(m["buttons"]) if m["buttons"] else [],
            "file": {
                "name": m["file_name"], "mime": m["mime"], "size": m["size"], "preview": previewable,
            } if m["file_id"] else None,
        }

    def client_thread(self, data: dict):
        try:
            thread = self.db.get_thread(int(data.get("user_id")))
        except (TypeError, ValueError):
            thread = None
        if not thread:
            raise ApiError("Чат не найден", 404)
        return thread

    def client_profile(self, thread) -> dict:
        """Данные клиента для выдачи билета и список его заказов."""
        orders = [o for o in self.db.orders_of_user(thread["user_id"], 10)]
        latest = next((o for o in orders if o["full_name"]), None)
        return {
            "prefill": {
                "full_name": latest["full_name"] if latest else thread["name"],
                "phone": latest["phone"] if latest else "",
                "university": latest["university"] if latest else "",
            },
            "orders": [
                {"id": o["id"], "status": o["status"], "type_title": o["type_title"], "price_text": money(o["price"]),
                 "ticket_code": ticket_code(self.config, o["ticket_id"]) if o["ticket_id"] else ""}
                for o in orders
            ],
        }

    async def support_threads(self, request: web.Request) -> web.Response:
        self.need(request, "support")
        data = await self.body(request)
        threads = self.db.list_threads(only_open=data.get("filter") != "all")
        query = str(data.get("query", "")).strip().casefold()
        if query:
            threads = [
                t for t in self.db.list_threads()
                if query in (t["name"] or "").casefold() or query in (t["username"] or "").casefold()
                or query == str(t["user_id"])
            ]
        counts = self.db.support_counts()
        return web.json_response({"threads": [self.thread_json(t) for t in threads], "counts": counts})

    async def support_thread(self, request: web.Request) -> web.Response:
        """Открывает чат с клиентом: помечает прочитанным, закрепляет за админом, отдаёт историю."""
        self.need(request, "support")
        thread = self.client_thread(await self.body(request))
        admin = request["user"].full_name
        fresh = (
            thread["assigned_at"] and (time.time() - datetime.fromisoformat(thread["assigned_at"]).timestamp()) < 1800
        )
        self.db.update_thread(
            thread["user_id"], unread=0, notified=0,
            **({} if thread["assigned_to"] == admin or (thread["assigned_to"] and fresh) else {"assigned_to": admin, "assigned_at": now_iso()}),
        )
        thread = self.db.get_thread(thread["user_id"])
        messages = self.db.chat_messages(thread["user_id"])
        return web.json_response({
            "thread": self.thread_json(thread), "messages": [self.message_json(m) for m in messages],
            "busy": bool(thread["assigned_to"] and thread["assigned_to"] != admin and fresh),
            **self.client_profile(thread),
        })

    async def support_messages(self, request: web.Request) -> web.Response:
        self.need(request, "support")
        data = await self.body(request)
        thread = self.client_thread(data)
        try:
            after = int(data.get("after") or 0)
        except (TypeError, ValueError):
            after = 0
        new = self.db.chat_messages(thread["user_id"], after=after)
        if thread["unread"] or thread["notified"]:   # админ смотрит чат — новое сразу считается прочитанным
            self.db.update_thread(thread["user_id"], unread=0, notified=0)
        return web.json_response({"messages": [self.message_json(m) for m in new]})

    def clean_client(self, thread) -> int:
        if support.is_staff(self.db, self.config, thread["user_id"]):
            raise ApiError("Это администратор, а не клиент", 400)
        return thread["user_id"]

    async def support_send(self, request: web.Request) -> web.Response:
        """Сообщение клиенту от лица бота."""
        self.need(request, "support")
        data = await self.body(request)
        thread = self.client_thread(data)
        user_id = self.clean_client(thread)
        text = str(data.get("text", "")).strip()
        if not text or len(text) > 3500:
            raise ApiError("Введите сообщение (до 3500 символов)")
        admin = request["user"].full_name
        try:
            await support.as_operator(admin, lambda: self.bot.send_message(
                user_id, "👨‍💻 <b>Поддержка:</b>\n" + escape(text)))
        except TelegramAPIError:
            raise ApiError("Не удалось отправить: клиент заблокировал бота или недоступен", 502)
        self.db.update_thread(user_id, status="open", unread=0, assigned_to=admin, assigned_at=now_iso())
        return web.json_response({"ok": True})

    async def support_send_file(self, request: web.Request) -> web.Response:
        """Файл или фото клиенту от лица бота (тело запроса — сам файл)."""
        self.need(request, "support")
        thread = self.client_thread({"user_id": request.query.get("user_id")})
        user_id = self.clean_client(thread)
        raw = await request.read()
        if not raw:
            raise ApiError("Файл пустой")
        if len(raw) > 20 * 1024 * 1024:
            raise ApiError("Файл больше 20 МБ")
        name = (request.query.get("name") or "file")[:100]
        caption = str(request.query.get("caption", "")).strip()[:900]
        mime = request.content_type or ""
        file = BufferedInputFile(raw, filename=name)
        admin = request["user"].full_name
        cap = ("👨‍💻 <b>Поддержка:</b>\n" + escape(caption)) if caption else "👨‍💻 Поддержка"
        as_photo = mime in ("image/jpeg", "image/png", "image/webp") and len(raw) <= 10 * 1024 * 1024
        try:
            if as_photo:
                await support.as_operator(admin, lambda: self.bot.send_photo(user_id, file, caption=cap))
            else:
                await support.as_operator(admin, lambda: self.bot.send_document(user_id, file, caption=cap))
        except TelegramAPIError:
            raise ApiError("Не удалось отправить: клиент заблокировал бота или недоступен", 502)
        self.db.update_thread(user_id, status="open", unread=0, assigned_to=admin, assigned_at=now_iso())
        return web.json_response({"ok": True})

    def file_message(self, data: dict):
        try:
            m = self.db.get_chat_message(int(data.get("id")))
        except (TypeError, ValueError):
            m = None
        if not m or not m["file_id"]:
            raise ApiError("Файл не найден", 404)
        return m

    async def support_file(self, request: web.Request) -> web.Response:
        """Предпросмотр файла клиента: фото — картинкой, PDF — первая страница (внутри Telegram они не открываются)."""
        self.need(request, "support")
        data = await self.body(request)
        m = self.file_message(data)
        buf = io.BytesIO()
        try:
            await self.bot.download(m["file_id"], destination=buf)
        except TelegramAPIError:
            raise ApiError("Telegram не отдаёт этот файл (слишком большой или устарел). Используйте «Прислать мне».", 502)
        raw = buf.getvalue()
        if raw[:5] == b"%PDF-":
            try:
                jpeg = await asyncio.to_thread(receipts.render_page, raw, int(data.get("page") or 0))
            except receipts.ReceiptError as e:
                raise ApiError(str(e))
            return web.Response(body=jpeg, content_type="image/jpeg")
        try:
            def to_jpeg() -> bytes:
                with Image.open(io.BytesIO(raw)) as src:
                    img = Image.new("RGB", src.size, "white")
                    img.paste(src.convert("RGBA"), mask=src.convert("RGBA").getchannel("A"))
                img.thumbnail((1600, 1600), Image.LANCZOS)
                out = io.BytesIO()
                img.save(out, format="JPEG", quality=85)
                return out.getvalue()
            jpeg = await asyncio.to_thread(to_jpeg)
        except (UnidentifiedImageError, OSError):
            raise ApiError("Предпросмотр недоступен для этого файла", 415)
        return web.Response(body=jpeg, content_type="image/jpeg")

    async def support_file_to_me(self, request: web.Request) -> web.Response:
        """Присылает файл клиента админу в чат (по file_id — работает и для больших файлов)."""
        self.need(request, "support")
        m = self.file_message(await self.body(request))
        thread = self.db.get_thread(m["user_id"])
        who = (thread["name"] if thread else "") or str(m["user_id"])
        caption = f"Файл из чата с клиентом {who}" + (f"\n{m['text']}" if m["text"] else "")
        sender = {
            "photo": self.bot.send_photo, "voice": self.bot.send_voice, "video": self.bot.send_video,
            "audio": self.bot.send_audio, "sticker": self.bot.send_sticker,
        }.get(m["kind"], self.bot.send_document)
        try:
            if m["kind"] == "sticker":
                await sender(request["user"].id, m["file_id"])
            else:
                await sender(request["user"].id, m["file_id"], caption=caption[:1000])
        except TelegramAPIError:
            raise ApiError("Telegram не смог переслать этот файл", 502)
        return web.json_response({"sent": True})

    async def support_close(self, request: web.Request) -> web.Response:
        self.need(request, "support")
        data = await self.body(request)
        thread = self.client_thread(data)
        user_id = self.clean_client(thread)
        if data.get("reopen"):
            self.db.update_thread(user_id, status="open")
        else:
            self.db.update_thread(user_id, status="closed", talking=0, unread=0, notified=0, assigned_to="")
            if data.get("notify", True):
                try:
                    await support.as_operator(request["user"].full_name, lambda: self.bot.send_message(
                        user_id, "✅ Обращение закрыто. Если появятся вопросы — нажмите /start и «🆘 Поддержка»."))
                except TelegramAPIError:
                    pass
        return web.json_response({"thread": self.thread_json(self.db.get_thread(user_id))})

    async def support_issue(self, request: web.Request) -> web.Response:
        """Выдаёт билет клиенту прямо из чата: он приходит ему в чат от бота."""
        access = self.need(request, "support")
        if not access.can("new"):
            raise ApiError("Для выдачи билета нужно разрешение «Новый билет»", 403)
        data = await self.body(request)
        thread = self.client_thread(data)
        user_id = self.clean_client(thread)
        full_name = clean(str(data.get("full_name", "")))
        if not full_name:
            raise ApiError("Введите фамилию и имя")
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
        type_row = None
        if data.get("type_id"):
            type_row = self.db.get_type(int(data["type_id"]))
            if not type_row:
                raise ApiError("Тип билета не найден", 404)
            left = self.db.type_left(type_row["id"])
            if left is not None and left < count and not data.get("force"):
                return web.json_response({"over_limit": {"left": left, "title": type_row["title"]}})
        user: User = request["user"]
        tickets = [
            self.db.create_ticket(
                full_name, clean(str(data.get("group", ""))), clean(str(data.get("university", ""))), phone,
                user.id, user.full_name, type_row["id"] if type_row else None, type_row["title"] if type_row else "",
            )
            for _ in range(count)
        ]
        delivered = True
        try:
            for n, t in enumerate(tickets, 1):
                head = "🎟 Вам выдан билет" if count == 1 else f"🎟 Вам выдан билет {n} из {count}"
                file = await ticket_file(t, self.db, self.config, self.bot_username)
                await support.as_operator(user.full_name, lambda: self.bot.send_document(
                    user_id, file, caption=f"{head}\n\n" + client_block(t, self.config)))
        except TelegramAPIError:
            delivered = False
        self.db.update_thread(user_id, unread=0, assigned_to=user.full_name, assigned_at=now_iso())
        return web.json_response({
            "tickets": [self.ticket_json(t, access) for t in tickets], "delivered": delivered,
        })

    async def support_clear(self, request: web.Request) -> web.Response:
        """Удаляет переписку с клиентом из журнала (только главный админ)."""
        self.need_main(request)
        thread = self.client_thread(await self.body(request))
        self.db.clear_chat(thread["user_id"])
        return web.json_response({"cleared": True})

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
            "shop_enabled": shop.shop_enabled(self.db),
            "shop_pay_url": self.db.get_setting("shop_pay_url") or "",
            "shop_bin": self.db.get_setting("shop_bin") or "",
            "shop_welcome": self.db.get_setting("shop_welcome") or "",
            "welcome_default": shop.DEFAULT_WELCOME,
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
        if "shop_enabled" in data:
            self.db.set_setting("shop_enabled", bool(data["shop_enabled"]))
        if "shop_pay_url" in data:
            self.db.set_setting("shop_pay_url", self.pay_url_value(data["shop_pay_url"]))
        if "shop_bin" in data:
            value = str(data["shop_bin"]).strip()
            if value and not re.fullmatch(r"\d{12}", value):
                raise ApiError("ИИН/БИН — 12 цифр")
            self.db.set_setting("shop_bin", value)
        if "shop_welcome" in data:
            self.db.set_setting("shop_welcome", clean(str(data["shop_welcome"]), 500))
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

    async def reset_info(self, request: web.Request) -> web.Response:
        self.need(request, "reset")
        counts = self.db.order_counts()
        return web.json_response({
            "tickets": len(self.db.all_tickets()),
            "orders": sum(counts.values()),
            "pending": counts.get("pending", 0),
            "chats": len(self.db.list_threads()),
        })

    async def reset(self, request: web.Request) -> web.Response:
        """Полный сброс: билеты и все заказы с чеками. Типы билетов и настройки остаются."""
        self.need(request, "reset")
        if self.db.all_tickets():
            # Сначала резервная копия: если она не отправится, ничего не удаляем.
            await self.bot.send_document(
                request["user"].id,
                export_file(self.db, self.config),
                caption="Резервная копия списка перед сбросом",
            )
        for name in self.db.reset_orders():
            (self.config.receipts_dir / name).unlink(missing_ok=True)
        self.db.reset_chats()
        return web.json_response({"deleted": self.db.reset_tickets()})
