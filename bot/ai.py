"""Поиск места под QR-код и под ФИ гостя на дизайне билета через Groq (vision-модель)."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import re
from pathlib import Path

import aiohttp
from PIL import Image

from .imaging import ALIGNS, Box, _hex, clamp_box, clamp_name_box

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"

PROMPT = """You are looking at an event ticket design. It is a TEMPLATE: the guest's \
name is not printed on it yet.

Task 1 - QR code. Find the place where the QR code is (or should be) printed: an \
existing sample QR code, or an empty / white square reserved for it. Return the \
bounding box of that whole square area. If there is no obvious place, choose the \
best empty area for a QR code.

Task 2 - guest name. Find the best place where the guest's full name (one short line \
such as "Иванов Иван", up to about 30 characters) could be printed so that it looks \
designed. Prefer, in this order: a blank line or field next to a label such as \
"NAME", "ФИО", "Имя", "GUEST", "HOLDER"; otherwise a clean, empty horizontal strip \
near the title, the date or the QR code. The box must be wide and short (about \
6:1 to 14:1), must NOT cover any text, logo, photo, ornament or the QR code, and \
should be about as tall as the neighbouring text lines. Also give the text alignment \
that matches the surrounding text ("left", "center" or "right") and a text colour \
that is clearly readable on that background, in the same palette as the design.
If really no clean place exists, set "name" to null.

Answer with JSON only, no other text:
{"qr": {"x_min": 0, "y_min": 0, "x_max": 0, "y_max": 0},
 "name": {"x_min": 0, "y_min": 0, "x_max": 0, "y_max": 0, "align": "left", "text_color": "#RRGGBB"}}
Coordinates are integers from 0 to 1000, relative to the image width (x) and height \
(y); (0,0) is the top-left corner. Use {"found": false} only if the image is not a \
ticket design at all."""


class AIError(Exception):
    def __init__(self, message: str, status: int | None = None, model_problem: bool = False):
        super().__init__(message)
        self.status = status
        self.model_problem = model_problem   # модель удалена, недоступна или не умеет картинки


def _encode(path: Path) -> str:
    with Image.open(path) as src:
        img = src.convert("RGBA")
    bg = Image.new("RGB", img.size, "white")
    bg.paste(img, mask=img.getchannel("A"))
    bg.thumbnail((1280, 1280), Image.LANCZOS)
    buf = io.BytesIO()
    bg.save(buf, format="JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode()


MODEL_PROBLEM = re.compile(
    r"decommission|no longer supported|does not exist|do not have access|not found|"
    r"model_not_found|not support|invalid model|blocked at the project|permission",
    re.I,
)


def _error(status: int, body) -> AIError:
    msg = (body.get("error") or {}).get("message") if isinstance(body, dict) else None
    text = f"Groq ответил {status}: {msg or body}"
    problem = status in (404, 403) or (status == 400 and bool(MODEL_PROBLEM.search(msg or "")))
    return AIError(text, status, problem)


async def _request(method: str, url: str, api_key: str, payload: dict | None = None,
                   timeout: int = 60) -> dict:
    """Один запрос к Groq. 429 (лимит запросов) повторяется один раз после паузы."""
    for attempt in (1, 2):
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout), trust_env=True
            ) as session:
                async with session.request(
                    method, url, json=payload, headers={"Authorization": f"Bearer {api_key}"}
                ) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status == 429 and attempt == 1:
                        await asyncio.sleep(min(float(resp.headers.get("retry-after", 8)), 20))
                        continue
                    if resp.status != 200:
                        raise _error(resp.status, body)
                    return body
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            raise AIError(f"не удалось обратиться к Groq ({e.__class__.__name__})") from e
    raise AIError("Groq: превышен лимит запросов", 429)


async def _post(payload: dict, api_key: str, timeout: int = 60) -> dict:
    """Запрос чата. Если модель не принимает response_format — повторяем без него
    (в подсказках и так сказано отвечать JSON, а разбор ответа терпим к оформлению)."""
    try:
        return await _request("POST", GROQ_URL, api_key, payload, timeout)
    except AIError as e:
        if e.status == 400 and "response_format" in payload and re.search(r"response_format|json", str(e), re.I):
            payload = {k: v for k, v in payload.items() if k != "response_format"}
            return await _request("POST", GROQ_URL, api_key, payload, timeout)
        raise


# ---------- выбор модели ----------
# Groq время от времени снимает модели с поддержки (как было с llama-4-scout). Поэтому бот не
# привязан к одному названию: если модель не отвечает, он берёт список моделей, доступных вашему
# ключу, пробует подходящие и запоминает рабочую.

VISION_PREFS = ("llama-4-maverick", "llama-4-scout", "maverick", "scout", "vision", "-vl", "pixtral", "llava", "gemma-3", "gemma3")
TEXT_PREFS = ("llama-3.3-70b", "gpt-oss-120b", "gpt-oss-20b", "llama-3.1-70b", "qwen", "kimi", "llama-3.1-8b", "llama")
NOT_CHAT = ("whisper", "guard", "tts", "playai", "orpheus", "embed", "safeguard", "compound", "moderation")
MAX_CANDIDATES = 5

_store = None                       # База (get_setting / set_setting): запоминает найденные модели
_remembered: dict[str, str] = {}


def bind(db) -> None:
    """Подключает базу, чтобы найденная рабочая модель переживала перезапуск."""
    global _store
    _store = db


def remembered(kind: str) -> str | None:
    if kind not in _remembered and _store is not None:
        value = _store.get_setting(f"groq_auto_{kind}")
        if value:
            _remembered[kind] = value
    return _remembered.get(kind)


def remember(kind: str, model: str) -> None:
    _remembered[kind] = model
    if _store is not None and _store.get_setting(f"groq_auto_{kind}") != model:
        _store.set_setting(f"groq_auto_{kind}", model)


def forget(kind: str | None = None) -> None:
    for k in ([kind] if kind else ["vision", "text"]):
        _remembered.pop(k, None)
        if _store is not None:
            _store.delete_setting(f"groq_auto_{k}")


async def list_models(api_key: str) -> list[str]:
    body = await _request("GET", GROQ_MODELS_URL, api_key, timeout=30)
    ids = []
    for m in body.get("data", []) if isinstance(body, dict) else []:
        if isinstance(m, dict) and m.get("id") and m.get("active", True) is not False:
            ids.append(str(m["id"]))
    return ids


def rank_models(ids: list[str], kind: str) -> list[str]:
    ids = [i for i in ids if not any(w in i.lower() for w in NOT_CHAT)]
    prefs = VISION_PREFS if kind == "vision" else TEXT_PREFS

    def score(model: str) -> int:
        low = model.lower()
        for n, pref in enumerate(prefs):
            if pref in low:
                return n
        return len(prefs)

    ranked = sorted(ids, key=score)
    if kind == "vision":   # картинки понимают только известные семейства моделей
        ranked = [m for m in ranked if score(m) < len(prefs)]
    return ranked


async def call(
    kind: str, payload: dict, api_key: str, explicit: str | None, default: str, timeout: int = 60,
) -> dict:
    """Запрос чата к модели вида kind («vision» или «text»), с подбором рабочей модели.

    Порядок: модель из настроек → найденная раньше → название по умолчанию. Если она недоступна,
    берётся список моделей ключа и пробуются подходящие."""
    first = explicit or remembered(kind) or default
    queue, tried, last = [first], set(), None
    fetched = False
    while True:
        if not queue:
            if fetched:
                break
            fetched = True
            queue = [m for m in rank_models(await list_models(api_key), kind) if m not in tried][:MAX_CANDIDATES]
            if not queue:
                break
        model = queue.pop(0)
        tried.add(model)
        try:
            body = await _post({**payload, "model": model}, api_key, timeout)
        except AIError as e:
            if not e.model_problem:
                raise
            last = e
            continue
        remember(kind, model)
        return body
    what = "картинки" if kind == "vision" else "текст"
    raise AIError(
        f"у вашего ключа Groq не нашлось рабочей модели, которая понимает {what}"
        f" (пробовал: {', '.join(sorted(tried)) or '—'}; последняя ошибка: {last}). "
        "Укажите модель вручную в Настройках."
    )


def _json_from(content: str):
    """JSON из ответа модели, даже если он обёрнут в ```json ... ``` или в пояснения."""
    text = (content or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


EXTRACT_PROMPT = """You extract event-ticket buyers from a pasted list (Russian or Kazakh \
text, maybe a table, numbered lines, headers, notes).
Return JSON only: {"records": [{"full_name": "", "group": "", "university": "", \
"phone": "", "count": 1}]}
Rules:
- One record per person. Keep the name as written (e.g. "Иванов Иван"). A person with \
only a name is still a record: group, university and phone are optional.
- group is the study group (e.g. "ИС-21"); university is the university name or abbreviation.
- phone exactly as written, digits and "+" only if you can; no invented numbers.
- count is the number of tickets if the text says so ("2 билета", "x3"), otherwise 1.
- Use "" for anything not in the text. Never invent data. Skip headers, totals, comments.
- If a field applies to a whole section (a university heading above the people), copy it \
to each person in that section.

TEXT:
"""


async def extract_records(
    text: str, api_key: str, model: str, explicit: str | None = None
) -> list[dict]:
    """Список клиентов из куска текста."""
    payload = {
        "temperature": 0,
        "max_completion_tokens": 4096,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": EXTRACT_PROMPT + text}],
    }
    body = await call("text", payload, api_key, explicit, model, timeout=90)
    try:
        answer = _json_from(body["choices"][0]["message"]["content"])
        records = answer["records"]
        if not isinstance(records, list):
            raise TypeError
        return [r for r in records if isinstance(r, dict)]
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise AIError("ИИ вернул непонятный ответ") from e


def _norm_box(entry, width: int, height: int) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = (float(entry[k]) for k in ("x_min", "y_min", "x_max", "y_max"))
    x0, x1 = sorted((x0 * width / 1000, x1 * width / 1000))
    y0, y1 = sorted((y0 * height / 1000, y1 * height / 1000))
    return x0, y0, x1, y1


def _rel(box, width: int, height: int) -> dict:
    x, y, w, h = box
    return {
        "x_min": round(x * 1000 / width), "y_min": round(y * 1000 / height),
        "x_max": round((x + w) * 1000 / width), "y_max": round((y + h) * 1000 / height),
    }


async def locate_layout(
    path: Path,
    api_key: str,
    model: str,
    size: tuple[int, int],
    previous_qr: Box | None = None,
    previous_name: dict | None = None,
    hint: str | None = None,
    explicit: str | None = None,
) -> tuple[Box, dict | None]:
    """Спрашивает у модели, где на шаблоне QR-код и где можно печатать ФИ.

    Возвращает (квадрат под QR, раскладка ФИ или None, если места нет). previous_* и hint —
    для повторной попытки, когда администратор отметил, что прошлый вариант неправильный:
    модель должна исправить только то, на что он указал."""
    width, height = size
    text = PROMPT
    if previous_qr or previous_name:
        prev = {}
        if previous_qr:
            x, y, side = previous_qr
            prev["qr"] = _rel((x, y, side, side), width, height)
        if previous_name and previous_name.get("box"):
            prev["name"] = {**_rel(previous_name["box"], width, height),
                            "align": previous_name.get("align", "left")}
        text += (
            "\n\nYour previous answer, which the user did NOT accept: "
            + json.dumps(prev) + ". Look again carefully. Change only what the user's "
            "correction is about and keep the other box exactly as it was."
        )
    if hint:
        text += f"\nThe user's correction (may be in Russian): {hint[:300]}"

    b64 = await asyncio.to_thread(_encode, path)
    payload = {
        "temperature": 0.4 if (previous_qr or previous_name) else 0,
        "max_completion_tokens": 600,
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
    body = await call("vision", payload, api_key, explicit, model)

    try:
        answer = _json_from(body["choices"][0]["message"]["content"])
        if not isinstance(answer, dict) or not answer.get("found", True):
            raise AIError("ИИ не нашёл на картинке билет")
        x0, y0, x1, y1 = _norm_box(answer["qr"], width, height)
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise AIError("ИИ вернул непонятный ответ") from e

    side = min(x1 - x0, y1 - y0)
    if side < 10:
        raise AIError("ИИ вернул слишком маленькую область")
    qr = clamp_box(((x0 + x1) / 2 - side / 2, (y0 + y1) / 2 - side / 2, side), width, height)

    name = None
    try:
        entry = answer.get("name")
        if isinstance(entry, dict):
            nx0, ny0, nx1, ny1 = _norm_box(entry, width, height)
            nw, nh = nx1 - nx0, ny1 - ny0
            qx, qy, qs = qr
            overlap = (
                min(nx1, qx + qs) > max(nx0, qx) and min(ny1, qy + qs) > max(ny0, qy)
            )
            if nw >= 60 and nh >= 14 and nw >= 2.5 * nh and not overlap:
                color = entry.get("text_color")
                name = {
                    "enabled": True,
                    "box": clamp_name_box((nx0, ny0, nw, nh), width, height),
                    "align": entry.get("align") if entry.get("align") in ALIGNS else "left",
                    "color": color if isinstance(color, str) and _hex(color) else None,
                }
    except (KeyError, TypeError, ValueError):
        name = None
    return qr, name


# ---------- проверка подключения (кнопка в Настройках) ----------


def _probe_image() -> str:
    img = Image.new("RGB", (96, 96), "white")
    for x in range(24, 72):
        for y in range(24, 72):
            img.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


async def check(
    api_key: str, default_vision: str, default_text: str,
    explicit_vision: str | None = None, explicit_text: str | None = None,
) -> dict:
    """Проверяет ключ и обе модели: что список моделей читается, что текстовая модель
    отвечает, что модель для картинок принимает изображение."""
    result: dict = {"models": [], "text": None, "vision": None, "error": ""}
    try:
        result["models"] = await list_models(api_key)
    except AIError as e:
        result["error"] = str(e)
        return result

    async def probe(kind: str, content, default: str, explicit: str | None) -> dict:
        payload = {"temperature": 0, "max_completion_tokens": 60,
                   "messages": [{"role": "user", "content": content}]}
        try:
            body = await call(kind, payload, api_key, explicit, default, timeout=45)
            return {"ok": True, "model": remembered(kind) or explicit or default,
                    "answer": str(body["choices"][0]["message"]["content"])[:80]}
        except AIError as e:
            return {"ok": False, "model": "", "error": str(e)}

    result["text"] = await probe("text", 'Reply with the single word: ok', default_text, explicit_text)
    result["vision"] = await probe("vision", [
        {"type": "text", "text": "Is there a black square in the picture? Answer yes or no."},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_probe_image()}"}},
    ], default_vision, explicit_vision)
    return result
