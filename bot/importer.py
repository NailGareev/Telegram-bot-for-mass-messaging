"""Массовый залив: достаёт список клиентов из .txt / .docx и готовит его к выдаче билетов.

Основную работу делает ИИ (Groq): он понимает произвольное оформление списка —
таблицы, нумерацию, лишние заголовки. Если ИИ недоступен, срабатывает простой
разбор по правилам: одна строка — один клиент, поля через запятую, табуляцию,
«;» или «|» в порядке «ФИ, группа, университет, телефон». Обязательно только ФИ.
"""
from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_RECORDS = 300
MAX_TICKETS_TOTAL = 1000
CHUNK_CHARS = 4500
MAX_FIELD = 100

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
PHONE_RE = re.compile(r"\+?\d[\d\s()\-]{7,}\d")


class ImportError_(Exception):
    """Файл нельзя прочитать (сообщение показывается администратору)."""


# ---------- чтение файла ----------


def _paragraph(p: ET.Element) -> str:
    out = []
    for node in p.iter():
        if node.tag == W + "t" and node.text:
            out.append(node.text)
        elif node.tag == W + "tab":
            out.append("\t")
        elif node.tag == W + "br":
            out.append("\n")
    return "".join(out)


def _docx_text(raw: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > 40 * 1024 * 1024:
                raise ImportError_("Документ слишком большой")
            xml = z.read(info)
    except (zipfile.BadZipFile, KeyError):
        raise ImportError_("Не удалось открыть файл как документ Word (.docx)")
    if b"<!DOCTYPE" in xml[:2000] or b"<!ENTITY" in xml[:4000]:
        raise ImportError_("Документ содержит недопустимые данные")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise ImportError_("Документ повреждён")
    body = root.find(W + "body")
    if body is None:
        raise ImportError_("В документе нет текста")
    lines: list[str] = []

    def walk(parent: ET.Element) -> None:
        for el in parent:
            if el.tag == W + "p":
                lines.extend(_paragraph(el).split("\n"))
            elif el.tag == W + "tbl":
                for row in el.iter(W + "tr"):
                    cells = []
                    for cell in row.findall(W + "tc"):
                        cells.append(" ".join(_paragraph(p).strip() for p in cell.iter(W + "p")).strip())
                    lines.append(" | ".join(cells))
            elif el.tag in (W + "sdt", W + "sdtContent"):
                walk(el)

    walk(body)
    return "\n".join(lines)


def extract_text(raw: bytes) -> str:
    """Текст из .docx (по содержимому, а не по имени) или из текстового файла."""
    if len(raw) > MAX_FILE_BYTES:
        raise ImportError_("Файл больше 5 МБ")
    if not raw.strip():
        raise ImportError_("Файл пустой")
    if raw[:2] == b"PK":
        text = _docx_text(raw)
    else:
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text = raw.decode("utf-16", errors="replace")
        else:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("cp1251", errors="replace")
        if "\x00" in text[:1000]:
            raise ImportError_("Это не текстовый файл. Нужен .txt или .docx")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ImportError_("В файле нет текста")
    return text


def chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Режет текст на части по границам строк — запись клиента не разрывается."""
    parts, cur, length = [], [], 0
    for line in text.split("\n"):
        line = line.rstrip()
        if not line.strip():
            continue
        line = line[:1000]
        if length + len(line) > size and cur:
            parts.append("\n".join(cur))
            cur, length = [], 0
        cur.append(line)
        length += len(line) + 1
    if cur:
        parts.append("\n".join(cur))
    return parts


# ---------- разбор по правилам (запасной вариант без ИИ) ----------


NAME_WORD = re.compile(r"[A-ZА-ЯЁӘІҢҒҮҰҚӨҺ][a-zа-яёәіңғүұқөһ'’-]+")


def looks_like_name(text: str) -> bool:
    """«Иванов Иван» — 2–4 слова с заглавной буквы, без цифр. Нужно, чтобы строки только
    с ФИ (без телефона) попадали в список, а заголовки вроде «Список участников» — нет."""
    words = text.split()
    return 2 <= len(words) <= 4 and all(NAME_WORD.fullmatch(w) for w in words)


def rules_parse(text: str) -> list[dict]:
    records = []
    for line in text.split("\n"):
        line = re.sub(r"^\s*\d{1,3}[.)]\s+", "", line.strip())
        m = PHONE_RE.search(line)
        phone = m.group(0) if m else ""
        rest = line[: m.start()] + " | " + line[m.end():] if m else line
        parts = [p.strip(" ,;:|–—-\t") for p in re.split(r"[\t|;,]+|\s[–—-]\s", rest)]
        parts = [p for p in parts if p]
        if not parts:
            continue
        if not phone and len(parts) < 2 and not looks_like_name(parts[0]):
            continue   # без телефона берём строку, только если в ней есть поля или это похоже на ФИ
        records.append({
            "full_name": parts[0],
            "group": parts[1] if len(parts) > 1 else "",
            "university": " ".join(parts[2:]),
            "phone": phone,
            "count": 1,
        })
    return records


# ---------- проверка и нормализация ----------


def clean(value, limit: int = MAX_FIELD) -> str:
    return " ".join(str(value or "").split())[:limit]


def normalize_phone(text: str) -> str | None:
    digits = re.sub(r"\D", "", text)
    if not 7 <= len(digits) <= 15:
        return None
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return "+" + digits


def normalize_record(raw: dict, max_count: int) -> dict:
    try:
        count = int(raw.get("count") or 1)
    except (TypeError, ValueError):
        count = 1
    phone_raw = clean(raw.get("phone"), 40)
    return {
        "full_name": clean(raw.get("full_name")),
        "group": clean(raw.get("group")),
        "university": clean(raw.get("university")),
        "phone": normalize_phone(phone_raw) or phone_raw,
        "count": max(1, min(count, max_count)),
    }


def issues_of(record: dict) -> list[str]:
    """Обязательно только ФИ. Группа, университет и телефон могут быть пустыми,
    но телефон, если он указан, должен быть верным."""
    problems = []
    if not record["full_name"]:
        problems.append("нет ФИ")
    if record["phone"] and not re.fullmatch(r"\+\d{7,15}", record["phone"]):
        problems.append("неверный телефон")
    return problems


@dataclass
class ParseResult:
    records: list[dict] = field(default_factory=list)
    mode: str = "rules"          # ai | rules | mixed
    warnings: list[str] = field(default_factory=list)
    dropped_duplicates: int = 0
    truncated: bool = False


def finalize(raw_records: list[dict], max_count: int, mode: str, warnings: list[str]) -> ParseResult:
    result = ParseResult(mode=mode, warnings=warnings)
    seen = set()
    for raw in raw_records:
        if not isinstance(raw, dict):
            continue
        rec = normalize_record(raw, max_count)
        if not rec["full_name"]:
            continue
        key = (rec["full_name"].casefold(), rec["group"].casefold(),
               rec["university"].casefold(), rec["phone"])
        if key in seen:
            result.dropped_duplicates += 1
            continue
        seen.add(key)
        if len(result.records) >= MAX_RECORDS:
            result.truncated = True
            continue
        result.records.append(rec)
    return result
