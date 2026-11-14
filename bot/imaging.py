"""Работа с картинками: генерация QR, отрисовка билета, распознавание QR."""
from __future__ import annotations

import io
import re
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
import qrcode
from PIL import Image, ImageDraw, ImageFont, ImageStat
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


# ---------- ФИ на билете ----------

FONT_FILE = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Inter-SemiBold.otf"
FONT_FALLBACKS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)
SAMPLE_NAME = "Сейтжанов Нурлан"   # так выглядит ФИ в предпросмотре дизайна
LIGHT_TEXT, DARK_TEXT = "#fff1c9", "#2a1a14"
ALIGNS = ("left", "center", "right")


@lru_cache(maxsize=64)
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in (FONT_FILE, *FONT_FALLBACKS):
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _luminance(rgb) -> float:
    def chan(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = rgb[:3]
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def _contrast(a, b) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _hex(value: str | None):
    if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value.strip()):
        v = value.strip()
        return tuple(int(v[i:i + 2], 16) for i in (1, 3, 5))
    return None


def pick_text_color(img: Image.Image, box: tuple[int, int, int, int], preferred: str | None):
    """Цвет ФИ: желаемый (из шаблона/ИИ), если он читается на этом фоне, иначе светлый или тёмный."""
    x, y, w, h = box
    region = img.convert("RGB").crop((x, y, x + w, y + h))
    background = tuple(ImageStat.Stat(region).median)
    want = _hex(preferred)
    if want and _contrast(want, background) >= 4.0:
        return want
    return max((_hex(LIGHT_TEXT), _hex(DARK_TEXT)), key=lambda c: _contrast(c, background))


def _text_width(font, text: str) -> float:
    return font.getlength(text)


def _fit(text: str, width: int, height: int):
    """Подбирает размер шрифта и разбивку на 1–2 строки, чтобы ФИ поместилось в рамку."""
    def one_line(t: str):
        size = max(8, int(height * 0.8))
        while size > 8 and _text_width(_font(size), t) > width:
            size -= 1
        return size, [t]

    size1, lines1 = one_line(text)
    fits1 = _text_width(_font(size1), text) <= width
    words = text.split()
    if (not fits1 or size1 < height * 0.45) and len(words) > 1 and height >= 36:
        best = None
        for i in range(1, len(words)):
            pair = [" ".join(words[:i]), " ".join(words[i:])]
            size = max(8, int(height / 2 * 0.86))
            while size > 8 and max(_text_width(_font(size), p) for p in pair) > width:
                size -= 1
            if best is None or size > best[0]:
                best = (size, pair)
        if best and best[0] > size1 * 1.2:
            return best
    if not fits1:  # совсем длинное — обрезаем многоточием
        font = _font(size1)
        t = text
        while len(t) > 1 and _text_width(font, t + "…") > width:
            t = t[:-1]
        return size1, [t.rstrip() + "…"]
    return size1, lines1


def clamp_name_box(box, width: int, height: int) -> list[int]:
    x, y, w, h = (int(round(float(v))) for v in box)
    w = max(80, min(w, width))
    h = max(18, min(h, height))
    x = max(0, min(x, width - w))
    y = max(0, min(y, height - h))
    return [x, y, w, h]


def draw_name(img: Image.Image, text: str, layout: dict) -> None:
    box = clamp_name_box(layout["box"], *img.size)
    x, y, w, h = box
    size, lines = _fit(" ".join(text.split()), w, h)
    font = _font(size)
    color = pick_text_color(img, (x, y, w, h), layout.get("color"))
    align = layout.get("align") if layout.get("align") in ALIGNS else "left"
    draw = ImageDraw.Draw(img)
    line_h = h / len(lines)
    for i, line in enumerate(lines):
        cy = y + line_h * (i + 0.5)
        if align == "center":
            draw.text((x + w / 2, cy), line, font=font, fill=color, anchor="mm")
        elif align == "right":
            draw.text((x + w, cy), line, font=font, fill=color, anchor="rm")
        else:
            draw.text((x, cy), line, font=font, fill=color, anchor="lm")


def render_ticket(
    template: Path | str, box: Box, data: str,
    name: str | None = None, name_layout: dict | None = None,
) -> bytes:
    """Рисует на шаблоне белую подложку и QR внутри квадрата box, а если задано —
    ФИ гостя в рамке name_layout. Возвращает PNG."""
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
    if name and name_layout and name_layout.get("enabled") and name_layout.get("box"):
        draw_name(img, name, name_layout)
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
