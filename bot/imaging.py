"""Работа с картинками: генерация QR, отрисовка билета, распознавание QR."""
from __future__ import annotations

import io
import re
from pathlib import Path

import cv2
import numpy as np
import qrcode
from PIL import Image, ImageDraw
from qrcode.constants import ERROR_CORRECT_M

Box = tuple[int, int, int]  # x, y, сторона квадрата

MAX_TEMPLATE_SIDE = 3000
TOKEN_RE = re.compile(r"t_([A-Za-z0-9_-]{16})")


def qr_payload(bot_username: str, token: str) -> str:
    """Содержимое QR: ссылка, открывающая бота с токеном билета."""
    return f"https://t.me/{bot_username}?start=t_{token}"


def extract_token(text: str) -> str | None:
    m = TOKEN_RE.search(text or "")
    return m.group(1) if m else None


def _make_qr(data: str, max_side: int) -> Image.Image:
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=1, border=0)
    qr.add_data(data)
    qr.make(fit=True)
    scale = max(1, max_side // qr.modules_count)
    qr.box_size = scale
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def render_ticket(template: Path | str, box: Box, data: str) -> bytes:
    """Рисует на шаблоне белую подложку и QR внутри квадрата box. Возвращает PNG."""
    with Image.open(template) as src:
        img = src.convert("RGBA" if "A" in src.getbands() else "RGB")
    x, y, size = box
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        (x, y, x + size - 1, y + size - 1), radius=max(2, size // 40), fill="white"
    )
    pad = max(2, round(size * 0.05))
    qr = _make_qr(data, size - 2 * pad)
    off = (size - qr.width) // 2
    img.paste(qr, (x + off, y + off))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def save_template(raw: bytes, dst: Path | str) -> tuple[int, int]:
    """Сохраняет присланный дизайн как PNG. Возвращает (ширина, высота)."""
    with Image.open(io.BytesIO(raw)) as src:
        src.load()
        img = src.convert("RGBA" if "A" in src.getbands() or src.mode == "P" else "RGB")
    if max(img.size) > MAX_TEMPLATE_SIDE:
        img.thumbnail((MAX_TEMPLATE_SIDE, MAX_TEMPLATE_SIDE), Image.LANCZOS)
    img.save(dst, format="PNG")
    return img.size


def image_size(path: Path | str) -> tuple[int, int]:
    with Image.open(path) as img:
        return img.size


def clamp_box(box: Box, width: int, height: int) -> Box:
    x, y, size = (int(round(v)) for v in box)
    size = max(20, min(size, width, height))
    x = max(0, min(x, width - size))
    y = max(0, min(y, height - size))
    return x, y, size


def find_qr_boxes(path: Path | str) -> list[Box]:
    """Ищет на шаблоне уже нарисованные QR-коды (образец) и возвращает
    квадраты под них — с небольшим запасом под белую подложку."""
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        return []
    h, w = img.shape[:2]
    try:
        ok, points = cv2.QRCodeDetector().detectMulti(img)
    except cv2.error:
        return []
    if not ok or points is None:
        return []
    boxes = []
    for quad in points:
        xs, ys = quad[:, 0], quad[:, 1]
        side = max(xs.max() - xs.min(), ys.max() - ys.min())
        cx, cy = (xs.max() + xs.min()) / 2, (ys.max() + ys.min()) / 2
        side *= 1.12
        boxes.append(clamp_box((cx - side / 2, cy - side / 2, side), w, h))
    return boxes


def decode_qr(raw: bytes) -> str | None:
    """Читает QR с фотографии. Пробует несколько вариантов обработки."""
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    longest = max(img.shape[:2])
    if longest > 2000:
        k = 2000 / longest
        img = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    def variants():
        yield img
        yield gray
        yield cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        yield cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        yield cv2.resize(gray, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)

    detector = cv2.QRCodeDetector()
    fallback = None
    for candidate in variants():
        try:
            ok, texts, _, _ = detector.detectAndDecodeMulti(candidate)
        except cv2.error:
            continue
        if not ok:
            continue
        for text in texts:
            if extract_token(text):
                return text
            fallback = fallback or text or None
    return fallback
