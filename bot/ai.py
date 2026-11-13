"""Поиск места под QR-код на дизайне билета через Groq (vision-модель)."""
from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path

import aiohttp
from PIL import Image

from .imaging import Box, clamp_box

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

PROMPT = """You are looking at an event ticket design.
Find the place where the QR code is (or should be) printed: an existing sample QR \
code, or an empty square placeholder / white square reserved for it.
Return the bounding box of that whole square area.

Answer with JSON only, no other text:
{"found": true, "x_min": 0, "y_min": 0, "x_max": 0, "y_max": 0}
Coordinates are integers from 0 to 1000, relative to the image width (x) and \
height (y); (0,0) is the top-left corner.
If there is no obvious place, choose the best empty area for a QR code and still \
return a box. Use {"found": false} only if the image is not a ticket design at all."""


class AIError(Exception):
    pass


def _encode(path: Path) -> str:
    with Image.open(path) as src:
        img = src.convert("RGBA")
    bg = Image.new("RGB", img.size, "white")
    bg.paste(img, mask=img.getchannel("A"))
    bg.thumbnail((1280, 1280), Image.LANCZOS)
    buf = io.BytesIO()
    bg.save(buf, format="JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode()


async def locate_qr(
    path: Path,
    api_key: str,
    model: str,
    size: tuple[int, int],
    previous: Box | None = None,
    hint: str | None = None,
) -> Box:
    """Спрашивает у модели квадрат под QR. previous/hint — для повторной попытки,
    когда администратор отметил, что прошлый вариант неправильный."""
    width, height = size
    text = PROMPT
    if previous:
        x, y, s = previous
        text += (
            "\n\nYour previous answer was WRONG: "
            f'{{"x_min": {x * 1000 // width}, "y_min": {y * 1000 // height}, '
            f'"x_max": {(x + s) * 1000 // width}, "y_max": {(y + s) * 1000 // height}}}. '
            "Look again carefully and give a corrected box."
        )
    if hint:
        text += f"\nThe user's correction (may be in Russian): {hint[:300]}"

    b64 = await asyncio.to_thread(_encode, path)
    payload = {
        "model": model,
        "temperature": 0.4 if previous else 0,
        "max_completion_tokens": 200,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            }
        ],
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60), trust_env=True
        ) as session:
            async with session.post(
                GROQ_URL, json=payload, headers={"Authorization": f"Bearer {api_key}"}
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    msg = (body.get("error") or {}).get("message") if isinstance(body, dict) else None
                    raise AIError(f"Groq ответил {resp.status}: {msg or body}")
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        raise AIError(f"не удалось обратиться к Groq ({e.__class__.__name__})") from e

    try:
        answer = json.loads(body["choices"][0]["message"]["content"])
        if not answer.get("found", True):
            raise AIError("ИИ не нашёл на картинке место под QR")
        x0, y0, x1, y1 = (
            float(answer[k]) for k in ("x_min", "y_min", "x_max", "y_max")
        )
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise AIError("ИИ вернул непонятный ответ") from e

    x0, x1 = sorted((x0 * width / 1000, x1 * width / 1000))
    y0, y1 = sorted((y0 * height / 1000, y1 * height / 1000))
    side = min(x1 - x0, y1 - y0)
    if side < 10:
        raise AIError("ИИ вернул слишком маленькую область")
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return clamp_box((cx - side / 2, cy - side / 2, side), width, height)
