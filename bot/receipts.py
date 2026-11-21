"""Чеки об оплате (PDF): чтение, распознавание полей и картинка для проверки.

Чек Kaspi — обычный PDF с текстом: сумма, номер чека, ИИН/БИН продавца, ФИО покупателя,
дата и время. Из него бот достаёт поля и сверяет их с заказом; решение всё равно принимает
администратор, который видит и сам чек, и результат сверки.
"""
from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass, field

import pypdfium2 as pdfium

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PAGES = 10


class ReceiptError(Exception):
    """Файл нельзя принять как чек (сообщение показывается клиенту)."""


@dataclass
class Receipt:
    amount: int | None = None        # в тенге
    number: str = ""                 # № чека
    seller_bin: str = ""             # ИИН/БИН продавца
    seller: str = ""
    buyer: str = ""                  # ФИО покупателя (Kaspi сокращает: «Наиль Г.»)
    paid_at: str = ""                # «2026-10-24T22:50» (время по Астане)
    method: str = ""                 # «с Kaspi Gold»
    success: bool = False            # в чеке есть «Оплата совершена»
    pages: int = 0
    text: str = field(default="", repr=False)

    @property
    def recognized(self) -> bool:
        return self.amount is not None or bool(self.number)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("text")
        return data

    @classmethod
    def from_dict(cls, data: dict | None) -> "Receipt":
        data = data or {}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "text"})


def _open(raw: bytes) -> pdfium.PdfDocument:
    if len(raw) > MAX_PDF_BYTES:
        raise ReceiptError("Файл больше 5 МБ.")
    if not raw.lstrip()[:5] == b"%PDF-":
        raise ReceiptError("Это не PDF-файл.")
    try:
        pdf = pdfium.PdfDocument(raw)
        pages = len(pdf)
    except Exception:
        raise ReceiptError("Не удалось открыть PDF — файл повреждён или защищён паролем.")
    if not 1 <= pages <= MAX_PAGES:
        raise ReceiptError("В чеке должно быть от 1 до 10 страниц.")
    return pdf


def _clean(text: str) -> str:
    return text.replace(" ", " ").replace(" ", " ").replace("\r", "")


def parse_pdf(raw: bytes) -> Receipt:
    """Читает PDF и достаёт из него поля чека. Для файла без текста (скан, картинка) вернёт
    Receipt без полей — такой чек администратор проверит глазами."""
    pdf = _open(raw)
    try:
        text = _clean("\n".join(
            pdf[i].get_textpage().get_text_range() for i in range(min(len(pdf), 2))
        ))
    except Exception:
        text = ""
    r = Receipt(pages=len(pdf), text=text)

    m = re.search(r"(\d[\d ]{0,11}(?:[.,]\d{1,2})?)\s*₸", text)
    if m:
        r.amount = int(re.sub(r"\D", "", re.split(r"[.,]", m.group(1))[0]) or 0) or None
    m = re.search(r"№\s*чека\s*[:\-]?\s*([A-Za-zА-Яа-я0-9\-]{5,40})", text, re.I)
    if m:
        r.number = m.group(1).strip()
    m = re.search(r"ИИН\s*/\s*БИН\s*продавца\s*[:\-]?\s*(\d{12})", text, re.I)
    if m:
        r.seller_bin = m.group(1)
    m = re.search(r"(?:ФИО|Ф\.И\.О\.?)\s*покупателя\s*[:\-]?\s*(.+)", text, re.I)
    if m:
        r.buyer = m.group(1).strip()[:80]
    m = re.search(r"^\s*((?:ИП|ТОО|АО)\s+.+)$", text, re.M)
    if m:
        r.seller = m.group(1).strip()[:80]
    m = re.search(r"Оплачено\s*[:\-]?\s*(.+)", text, re.I)
    if m:
        r.method = m.group(1).strip()[:60]
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})", text)
    if m:
        d, mo, y, h, mi = (int(x) for x in m.groups())
        if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31 and h < 24 and mi < 60:
            r.paid_at = f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}"
    r.success = bool(re.search(r"оплата\s+совершена", text, re.I))
    return r


def render_page(raw: bytes, page: int = 0, scale: float = 1.6) -> bytes:
    """Страница PDF как JPEG — для просмотра в приложении (внутри Telegram PDF не открывается)."""
    pdf = _open(raw)
    page = max(0, min(page, len(pdf) - 1))
    image = pdf[page].render(scale=scale).to_pil().convert("RGB")
    image.thumbnail((1400, 1800))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85)
    return out.getvalue()
