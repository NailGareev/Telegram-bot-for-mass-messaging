"""Обновление проекта: главный администратор присылает zip-архив с GitHub прямо в чат."""
from __future__ import annotations

import asyncio
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import updater
from ..access import IsMain
from ..config import BASE_DIR, Config
from ..service import Screen, download
from ..ui import inline

router = Router()

TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024   # больше бот скачать не может
_busy = asyncio.Lock()


def is_zip(document) -> bool:
    name = (document.file_name or "").lower()
    return name.endswith(".zip") or (document.mime_type or "") in {
        "application/zip", "application/x-zip-compressed",
    }


def plan_text(plan: updater.Plan, name: str) -> str:
    changed = plan.added + plan.changed
    shown = ", ".join(f"<code>{escape(f)}</code>" for f in changed[:12])
    if len(changed) > 12:
        shown += f" и ещё {len(changed) - 12}"
    text = (
        "📦 <b>Обновление проекта</b>\n"
        f"Файл: {escape(name)}\n\n"
        f"Новых файлов: <b>{len(plan.added)}</b> · изменено: <b>{len(plan.changed)}</b> · "
        f"удалено: <b>{len(plan.removed)}</b>\n"
        f"Зависимости: {'изменились, будут установлены' if plan.requirements_changed else 'без изменений'}\n"
    )
    if shown:
        text += f"\n{shown}\n"
    text += (
        "\nНастройки (.env), база билетов, дизайн билета и ключи остаются как есть. Перед заменой "
        "бот сохранит копию текущей версии и проверит, что новая запускается; если новая версия "
        "не стартует, прежняя вернётся сама."
    )
    if not updater.supervised():
        text += (
            "\n\n⚠️ Бот запущен не как служба, поэтому после установки он остановится — "
            "запустите его снова вручную."
        )
    return text


@router.message(F.document, IsMain(), F.document.func(is_zip))
async def update_received(
    message: Message, bot: Bot, state: FSMContext, config: Config, screen: Screen
):
    doc = message.document
    await state.clear()
    if doc.file_size and doc.file_size > TELEGRAM_DOWNLOAD_LIMIT:
        return await screen.show(
            "❌ Архив больше 20 МБ — Telegram не даёт боту скачать такой файл. "
            "Проект столько весить не должен: проверьте, что это архив с GitHub.",
            inline([("❌ Закрыть", "cancel")]),
        )
    await screen.show("📦 Проверяю архив…")
    try:
        raw = await download(bot, doc.file_id)
        plan = await asyncio.to_thread(
            updater.prepare, raw, BASE_DIR, config.data_dir, doc.file_name or "архив"
        )
    except updater.UpdateError as e:
        return await screen.show(f"❌ {escape(str(e))}", inline([("❌ Закрыть", "cancel")]))
    await screen.show(
        plan_text(plan, doc.file_name or "архив"),
        inline([("✅ Установить и перезапустить", "upd:go")], [("❌ Отмена", "upd:cancel")]),
    )


@router.callback_query(F.data == "upd:cancel", IsMain())
async def update_cancel(cb: CallbackQuery, config: Config, screen: Screen):
    updater.discard(config.data_dir)
    await cb.answer("Отменено")
    await screen.clear()


@router.callback_query(F.data == "upd:go", IsMain())
async def update_apply(
    cb: CallbackQuery, config: Config, screen: Screen, restarter: updater.Restarter
):
    if _busy.locked():
        return await cb.answer("Обновление уже устанавливается", show_alert=True)
    async with _busy:
        await cb.answer()
        await screen.show("⏳ Устанавливаю обновление… Это может занять до минуты.")
        try:
            plan = await asyncio.to_thread(
                updater.apply, BASE_DIR, config.data_dir,
                {"chat_id": cb.from_user.id, "who": cb.from_user.full_name},
            )
        except updater.UpdateError as e:
            return await screen.show(
                f"❌ <b>Обновление не установлено</b>\n<pre>{escape(str(e))}</pre>\n"
                "Бот продолжает работать на прежней версии.",
                inline([("❌ Закрыть", "cancel")]),
            )
        except Exception as e:
            return await screen.show(
                f"❌ Обновление не установлено: {escape(e.__class__.__name__)}. "
                "Бот продолжает работать на прежней версии.",
                inline([("❌ Закрыть", "cancel")]),
            )
        await screen.show(
            f"✅ Файлы заменены (изменено: {len(plan.changed)}, новых: {len(plan.added)}, "
            f"удалено: {len(plan.removed)}). Перезапускаюсь — через несколько секунд бот напишет, "
            "что всё работает."
        )
        restarter.request()
