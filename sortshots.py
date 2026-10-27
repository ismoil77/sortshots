#!/usr/bin/env python3
"""Сортировка скриншотов локальной нейросетью (Ollama + Qwen3-VL).

  sortshots pull     скопировать папку Screenshots с телефона (adb), телефон не меняется
  sortshots sample   прогнать случайные N скриншотов и предложить список тем
  sortshots run      разобрать все скриншоты и разложить по папкам
  sortshots place    только разложить заново (после правки тем)
  sortshots app      приложение с живым прогрессом и проверкой: http://127.0.0.1:8765
"""
import argparse, base64, hashlib, io, json, os, random, re, shutil, subprocess, zipfile
import sys, threading, time, urllib.request, zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image

ROOT = Path(os.environ.get("SORTSHOTS_ROOT", Path.home() / "Pictures" / "Скриншоты"))
SRC = ROOT / "_исходники"
DATA = ROOT / "_данные"
RESULTS = DATA / "results.jsonl"
TOPICS = DATA / "topics.json"
OVERRIDES = DATA / "overrides.json"
PLACED = DATA / "placed.json"
HINTS = DATA / "hints.json"            # подсказки владельца: попадают в инструкцию модели
QUEUE = DATA / "recheck_queue.json"    # уже разобранные скриншоты, которые надо разобрать заново
NOTES = DATA / "notes.json"            # комментарии владельца к отдельным скриншотам: «это не то, а вот что»
EXAMPLES = DATA / "examples"           # вырезанные куски скриншотов — образцы для обучения
MAX_EXAMPLES = 6   # больше образцов не влезет в контекст рядом со скриншотом
CROP_SIDE = 384    # образец до 384 px: около 150 токенов, читается и не раздувает запрос

WIN = sys.platform == "win32"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if WIN else 0  # на Windows не мигать чёрными окнами консоли
APP_DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "Sortshots"

OLLAMA = "http://127.0.0.1:11434"
OLLAMA_IGPU = "http://127.0.0.1:11435"  # второй экземпляр на встроенной Intel (Vulkan)
FAST = os.environ.get("SORTSHOTS_FAST", "qwen3-vl:4b-instruct")
STRONG = os.environ.get("SORTSHOTS_STRONG", "qwen3-vl:8b-instruct")
PHONE_DIRS = ["/sdcard/Pictures/Screenshots", "/sdcard/DCIM/Screenshots"]
EXTS = {".png", ".jpg", ".jpeg", ".webp"}

CHECK = "Проверить"
OTHER = "Разное"
DEV = [
    "Разработка/GitHub-репозитории",
    "Разработка/Claude скилы и плагины",
    "Разработка/MCP-серверы",
    "Разработка/AI-инструменты и модели",
    "Разработка/Код и ошибки",
    "Разработка/Другие инструменты",
]
DEFAULT_TOPICS = DEV + [
    "Переписки", "Контакты и звонки", "Банк и платежи", "Покупки и заказы", "Документы и справки",
    "Соцсети и посты", "Видео и музыка", "Карты и адреса", "Мемы и картинки",
    "Учёба и статьи", "Настройки телефона", OTHER,
]
KINDS = ["github_repo", "skill", "mcp_server", "ai_tool", "dev_tool", "url"]
KIND_TITLES = {
    "github_repo": "GitHub-репозитории", "skill": "Скилы и плагины", "mcp_server": "MCP-серверы",
    "ai_tool": "AI-инструменты и модели", "dev_tool": "Другие инструменты", "url": "Ссылки",
}

TOPIC_HINTS = {
    "Разработка/GitHub-репозитории": "страница репозитория или README на GitHub/GitLab, список репозиториев, "
        "пост или статья, где главное — конкретный репозиторий (owner/repo, ⭐ Star, Fork)",
    "Разработка/Claude скилы и плагины": "скилы (skills, SKILL.md), плагины, slash-команды, сабагенты, хуки, "
        "CLAUDE.md / AGENTS.md для Claude Code, Codex, Cursor, OpenCode и других AI-агентов",
    "Разработка/MCP-серверы": "Model Context Protocol: MCP-серверы, их списки, установка и конфиг (mcpServers, npx … mcp)",
    "Разработка/AI-инструменты и модели": "нейросети и AI-сервисы (ChatGPT, Claude, Gemini, Midjourney…), модели, "
        "бенчмарки, промпты, ответы ИИ — если это не код, не скил и не MCP",
    "Разработка/Код и ошибки": "код, терминал, логи, стектрейсы, сообщения об ошибках, редактор кода",
    "Разработка/Другие инструменты": "прочие программы, библиотеки, сервисы и CLI для разработчиков и дизайнеров (не AI)",
    "Переписки": "чаты в мессенджерах, SMS, письма — главное здесь разговор людей",
    "Контакты и звонки": "карточка контакта, номер телефона, профиль человека или компании (в телефонной книге, "
        "Telegram, WhatsApp), экран звонка, журнал вызовов",
    "Банк и платежи": "переводы, чеки, балансы, банковские приложения, оплата, курсы валют",
    "Покупки и заказы": "товары, цены, корзина, заказы, доставка, маркетплейсы",
    "Документы и справки": "паспорт, справки, билеты, договоры, госуслуги, официальные бумаги",
    "Соцсети и посты": "посты, сторис, профили, комментарии (Instagram, Telegram-каналы, X, TikTok) не про разработку",
    "Видео и музыка": "плееры, YouTube, фильмы, песни, кадры из видео",
    "Карты и адреса": "карты, маршруты, адреса, геолокация, такси",
    "Мемы и картинки": "мемы, юмор, красивые картинки, обои",
    "Учёба и статьи": "статьи, учебные материалы, курсы, новости, инструкции не про разработку",
    "Настройки телефона": "системные экраны Android, настройки, уведомления, системные ошибки",
    OTHER: "ничего из перечисленного не подходит",
}

PROMPT = """Ты раскладываешь скриншоты с телефона по папкам. На входе один скриншот.
Сначала внимательно рассмотри его целиком: шапку приложения, адресную строку, заголовки, логотипы, иконки, мелкий текст.
Читай текст точно, буква в букву. Ничего не придумывай: пиши только то, что действительно видно.

Заполни поля строго в этом порядке:

app — какое это приложение или сайт: «Telegram», «GitHub», «Chrome: habr.com», «Google Maps»…
  Называй только если на экране видно название, адрес сайта или узнаваемый логотип. Иначе пиши «неизвестно» — не угадывай.

seen — главное на экране, одно предложение до 20 слов, с ключевыми словами как на экране.

topic — ровно одна тема из списка:
{topics}

Как выбирать тему:
1. Решай по главному содержимому, а не по приложению. Пост в Telegram про перевод денег — «Банк и платежи».
   Карточка или профиль человека с номером, экран звонка — «Контакты и звонки», а не «Переписки»: переписка — это когда видны сообщения.
2. Разработка важнее всего: если главное на экране — программирование, ИИ, репозиторий, скил или MCP, выбирай «Разработка/…», даже если это пост в соцсети, переписка или видео.
3. Если подходит несколько тем «Разработка/…», бери самую конкретную в таком порядке: MCP-серверы, Claude скилы и плагины, GitHub-репозитории, AI-инструменты и модели, Код и ошибки, Другие инструменты.

title — название файла на русском, 3–7 слов, конкретно про содержание.
  Хорошо: «Репозиторий browser-use на GitHub», «Перевод 500 сомони Алишеру», «Ошибка CORS в Nuxt», «MCP-сервер для Figma».
  Плохо: «Скриншот экрана», «Интересный пост», «Текст», «Изображение».
  Названия программ, репозиториев и имена людей — как на экране (латиницей, если так написано). Без даты, кавычек и эмодзи.

description — одно предложение до 20 слов на русском: что конкретно на скриншоте.

keywords — 3–6 ключевых слов: русские или как на экране.

entities — всё про разработку и ИИ, что видно на экране (если ничего — пустой список):
  github_repo — репозиторий: name «owner/repo», если видны оба, иначе только имя репозитория;
  skill — скил, плагин, slash-команда или сабагент для AI-агента;
  mcp_server — MCP-сервер;
  ai_tool — AI-сервис или модель;
  dev_tool — программа, библиотека, пакет, CLI для разработки;
  url — любая другая ссылка, видимая на экране.
  name — точно как на экране. url — только если ссылка видна на экране, или для github_repo, когда виден owner/repo (https://github.com/owner/repo); иначе пустая строка.

confidence — насколько ты уверен в теме:
  0.95 — тема очевидна; 0.8 — уверен, но есть похожая тема; 0.6 — колеблешься между двумя; 0.4 и ниже — не понял, что на экране.
  Будь честен: заниженная уверенность лучше, чем ошибка.

Пиши по-русски; названия и имена — как на экране. Никогда не используй китайские или другие иероглифы.
Ответь JSON в одну строку, без отступов и переносов."""


def topic_lines(topic_list):
    return "\n".join(f"- {t}: {TOPIC_HINTS[t]}" if t in TOPIC_HINTS else f"- {t}" for t in topic_list)


def schema(topics):
    return {
        "type": "object",
        "properties": {  # порядок важен: модель сначала описывает, что видит, и только потом выбирает тему
            "app": {"type": "string"},
            "seen": {"type": "string"},
            "topic": {"type": "string", "enum": topics} if topics else {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "keywords": {"type": "array", "items": {"type": "string"}},
            "entities": {"type": "array", "items": {"type": "object", "properties": {
                "kind": {"type": "string", "enum": KINDS},
                "name": {"type": "string"}, "url": {"type": "string"}},
                "required": ["kind", "name", "url"]}},
            "confidence": {"type": "number"},
        },
        "required": ["app", "seen", "topic", "title", "description", "keywords", "entities", "confidence"],
    }


# ---------- общие ----------

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def topics():
    t = load_json(TOPICS, None)
    if t is None:
        save_json(TOPICS, DEFAULT_TOPICS)
        t = DEFAULT_TOPICS
    return t if OTHER in t else t + [OTHER]


def sources():
    return sorted(p for p in SRC.rglob("*") if p.suffix.lower() in EXTS and p.is_file())


def sha1(path):
    return hashlib.sha1(path.read_bytes()).hexdigest()


def load_results():
    res = {}
    if RESULTS.exists():
        for line in RESULTS.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                res.setdefault(r["sha"], {}).update(r)
    return res


_lock = threading.Lock()


def append_result(rec):
    with _lock, RESULTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def shot_date(path):
    m = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", path.name)
    if m:
        return f"{m[1]}-{m[2]}-{m[3]}"
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")


# ---------- ollama ----------

def ollama_up():
    try:
        urllib.request.urlopen(OLLAMA + "/api/version", timeout=2)
        return True
    except OSError:
        return False


class OllamaMissing(RuntimeError):
    pass


def run(cmd, **kw):
    return subprocess.run(cmd, creationflags=NO_WINDOW, **kw)


def popen(cmd, **kw):
    return subprocess.Popen(cmd, creationflags=NO_WINDOW, **kw)


def open_path(path):
    if WIN:
        os.startfile(path)
    else:
        popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)], start_new_session=True)


def find_ollama():
    found = shutil.which("ollama")
    if not found and WIN:  # установщик Ollama кладёт сюда; PATH обновится только после перезапуска
        exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        found = str(exe) if exe.exists() else None
    return found


def ensure_ollama():
    if ollama_up():
        return
    exe = find_ollama()
    if not exe:
        raise OllamaMissing("Ollama не установлена")
    DATA.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, OLLAMA_NUM_PARALLEL="1", OLLAMA_FLASH_ATTENTION="1", OLLAMA_KEEP_ALIVE="30m")
    popen([exe, "serve"], env=env, stdout=open(DATA / "ollama.log", "ab"),
          stderr=subprocess.STDOUT, start_new_session=not WIN)
    for _ in range(60):
        if ollama_up():
            return
        time.sleep(0.5)
    raise RuntimeError("Ollama не запустилась, смотри " + str(DATA / "ollama.log"))


def download(url, dest, progress=None):
    """Скачать файл с прогрессом progress(done, total)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as r, tmp.open("wb") as f:
        total, done = int(r.headers.get("Content-Length") or 0), 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    tmp.replace(dest)


def find_adb(progress=None):
    """adb из PATH, иначе свой из Android platform-tools (скачивается один раз, ~13 МБ)."""
    found = shutil.which("adb")
    if found:
        return found
    exe = APP_DATA / "platform-tools" / ("adb.exe" if WIN else "adb")
    if not exe.exists():
        osname = "windows" if WIN else "darwin" if sys.platform == "darwin" else "linux"
        z = APP_DATA / "platform-tools.zip"
        download(f"https://dl.google.com/android/repository/platform-tools-latest-{osname}.zip", z, progress)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(APP_DATA)
        z.unlink()
        if not WIN:
            exe.chmod(0o755)
    return str(exe)


def prep_image(path):
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((1080, 2160))  # мелкий текст (имена репозиториев) должен читаться
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def crop_hints():
    """Подсказки с выделенным куском скриншота: модель видит их как картинки-образцы."""
    return [h for h in load_json(HINTS, []) if h.get("crop") and (DATA / h["crop"]).exists()][-MAX_EXAMPLES:]


def save_crop(path, box, name):
    """Вырезать часть скриншота (box — доли от 0 до 1: x, y, ширина, высота) в образец. Вернёт путь от DATA."""
    x, y, w, h = (max(0.0, min(1.0, float(v))) for v in box)
    EXAMPLES.mkdir(parents=True, exist_ok=True)
    with Image.open(path) as im:
        im = im.convert("RGB")
        iw, ih = im.size
        left, top = int(x * iw), int(y * ih)
        part = im.crop((left, top, max(left + 1, min(iw, int((x + w) * iw))), max(top + 1, min(ih, int((y + h) * ih)))))
        part.thumbnail((CROP_SIDE, CROP_SIDE))
        out = EXAMPLES / f"{name}.jpg"
        part.save(out, "JPEG", quality=88)
    return out.relative_to(DATA).as_posix()


def b64_file(path):
    return base64.b64encode(Path(path).read_bytes()).decode()


def examples_messages():
    """Образцы идут отдельной парой сообщений перед скриншотом: префикс одинаковый, Ollama его кеширует."""
    ex = crop_hints()
    if not ex:
        return []
    lines = [f"Картинка {i}: {h['text'] or 'пример'}" + (f" → тема «{h['topic']}»" if h.get("topic") else "")
             for i, h in enumerate(ex, 1)]
    return [{"role": "user", "images": [b64_file(DATA / h["crop"]) for h in ex],
             "content": "Это куски скриншотов, которые выделил владелец, и что они значат:\n" + "\n".join(lines)
                        + "\nЕсли на скриншоте есть похожее (тот же интерфейс, значок, форма), применяй это правило."},
            {"role": "assistant", "content": "Понял, буду сверять скриншоты с этими образцами."}]


def hints_block():
    with_crop = {h["id"] for h in crop_hints()}
    hints = [h for h in load_json(HINTS, []) if h["id"] not in with_crop]  # подсказки с образцом уже в картинках
    if not hints:
        return ""
    lines = [f"- {h['text']}" + (f" → тема «{h['topic']}»" if h.get("topic") else "") for h in hints]
    return "\n\nПодсказки владельца скриншотов. Они важнее общих правил выше:\n" + "\n".join(lines)


def answer_text(f):
    """Текст прошлого ответа модели, по которому ищем похожие скриншоты."""
    return " ".join([f.get(k, "") for k in ("app", "seen", "title", "description")] + f.get("keywords", [])) if f else ""


def words_pattern(words):
    """«контакт, номер телефона» → регулярка, которая находит любое из слов."""
    parts = [re.escape(w.strip()) for w in re.split(r"[,;\n]", words) if w.strip()]
    return re.compile("|".join(parts), re.I) if parts else None


def queue_matching(pattern, res=None, skip=()):
    """Поставить в очередь перепроверки разобранные скриншоты, чей ответ подходит под pattern. Вернёт сколько."""
    res = res if res is not None else load_results()
    return queue_add([sha for sha, r in res.items() if sha not in skip and pattern.search(answer_text(r.get("fast")))])


def queue_add(shas, front=False):
    """Добавить в очередь перепроверки; front — разобрать раньше всех остальных. Вернёт сколько новых."""
    with _lock:
        q, new = load_json(QUEUE, []), list(dict.fromkeys(shas))
        old, first = set(q), set(new)
        if front:
            save_json(QUEUE, new + [x for x in q if x not in first])
            return len(first - old)
        added = [x for x in new if x not in old]
        save_json(QUEUE, q + added)
        return len(added)


def add_topic(name):
    t = topics()
    if name and name not in t:
        t.insert(len(t) - 1 if t[-1] == OTHER else len(t), name)
        save_json(TOPICS, t)
    return t


def ask(model, path, topic_list, host=OLLAMA, sha=None):
    user = {"role": "user", "images": [prep_image(path)], "content": "Разбери этот скриншот."}
    note = load_json(NOTES, {}).get(sha) if sha else None
    if note:
        user["content"] += ("\n\nВладелец уже смотрел этот скриншот и сказал, что прошлый разбор неверный. "
                            "Его комментарий важнее всего остального: " + (note.get("text") or "разбери внимательнее"))
        if note.get("crop") and (DATA / note["crop"]).exists():
            user["images"].append(b64_file(DATA / note["crop"]))
            user["content"] += "\nВторая картинка — часть этого скриншота, на которую владелец просит обратить внимание."
    body = {
        "model": model, "stream": False, "think": False, "keep_alive": "30m",
        "format": schema(topic_list),
        "options": {"temperature": 0, "num_ctx": 6144},
        "messages": [  # инструкция в system: Ollama кеширует её между запросами, модель читает только картинку
            {"role": "system", "content": PROMPT.format(topics=topic_lines(topic_list or DEFAULT_TOPICS)) + hints_block()},
            *examples_messages(), user],
    }
    req = urllib.request.Request(host + "/api/chat", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        out = json.loads(json.loads(r.read())["message"]["content"])
    out["confidence"] = max(0.0, min(1.0, float(out.get("confidence") or 0)))
    return out


def analyze(items, model, stage, topic_list, workers=1):
    done, total, t0 = 0, len(items), time.time()
    with ThreadPoolExecutor(workers) as pool:
        futs = {pool.submit(ask, model, p, topic_list, sha=sha): (sha, p) for sha, p in items}
        for fut in as_completed(futs):
            sha, p = futs[fut]
            done += 1
            try:
                append_result({"sha": sha, "src": p.relative_to(SRC).as_posix(), stage: fut.result()})
            except Exception as e:  # одна битая картинка не должна останавливать весь прогон
                print(f"  ошибка {p.name}: {e}", file=sys.stderr)
            if done % 10 == 0 or done == total:
                el = time.time() - t0
                print(f"  {stage}: {done}/{total}, {el / done:.1f} с/шт, осталось ~{el / done * (total - done) / 60:.0f} мин")


# ---------- решение по теме ----------

def final(r, topic_list):
    ov = load_json(OVERRIDES, {}).get(r["sha"])
    fast, strong = r.get("fast"), r.get("strong")
    best = strong or fast
    if not best:
        return None
    # Самооценке маленькой модели верить нельзя (почти всегда 0.95), поэтому главный сигнал —
    # согласие двух моделей. Разошлись — решает человек в «Проверить».
    topic = best["topic"] if best["topic"] in topic_list else CHECK
    if strong and fast and strong["topic"] != fast["topic"]:
        topic = CHECK
    elif max(x["confidence"] for x in (fast, strong) if x) < 0.7:
        topic = CHECK
    if isinstance(ov, str):  # старый формат: только папка
        ov = {"topic": ov}
    ov = ov or {}
    out = dict(best, topic=ov.get("topic") or topic)
    if ov.get("title"):  # название, исправленное вручную
        out["title"] = ov["title"]
    return out


def needs_strong(r, topic_list):
    f = r.get("fast")
    return f and not r.get("strong") and (
        f["confidence"] < 0.8 or f["topic"].startswith("Разработка/") or f["topic"] not in topic_list)


# ---------- метаданные (без пережатия картинки) ----------

XMP_HDR = b"http://ns.adobe.com/xap/1.0/\x00"


def xmp_packet(title, desc, keywords, topic):
    li = "".join(f"<rdf:li>{escape(k)}</rdf:li>" for k in keywords)
    return (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
        f'<dc:title><rdf:Alt><rdf:li xml:lang="x-default">{escape(title)}</rdf:li></rdf:Alt></dc:title>'
        f'<dc:description><rdf:Alt><rdf:li xml:lang="x-default">{escape(desc)}</rdf:li></rdf:Alt></dc:description>'
        f"<dc:subject><rdf:Bag>{li}</rdf:Bag></dc:subject><xmp:Label>{escape(topic)}</xmp:Label>"
        '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
    ).encode()


def utf16(s):
    return (s + "\x00").encode("utf-16-le")


def jpeg_meta(data, path, title, desc, keywords, xmp):
    with Image.open(path) as im:
        exif = im.getexif()
    exif[0x010E] = desc.encode()   # ImageDescription, байты: иначе Pillow заменит кириллицу на «?»
    exif[0x9C9B] = utf16(title)    # XPTitle (видно в свойствах файла в Windows)
    exif[0x9C9C] = utf16(desc)     # XPComment
    exif[0x9C9E] = utf16("; ".join(keywords))  # XPKeywords
    exif_bytes = exif.tobytes()
    if not exif_bytes.startswith(b"Exif"):
        exif_bytes = b"Exif\x00\x00" + exif_bytes
    i, keep = 2, []
    while i + 4 <= len(data) and data[i] == 0xFF and data[i + 1] != 0xDA:
        n = int.from_bytes(data[i + 2:i + 4], "big")
        seg = data[i:i + 2 + n]
        if not (data[i + 1] == 0xE1 and (seg[4:].startswith(b"Exif\x00\x00") or seg[4:].startswith(XMP_HDR))):
            keep.append(seg)
        i += 2 + n
    app1 = lambda p: b"\xff\xe1" + (len(p) + 2).to_bytes(2, "big") + p
    new = [app1(exif_bytes)] if len(exif_bytes) < 65000 else []
    new.append(app1(XMP_HDR + xmp))
    app0 = [s for s in keep if s[1] == 0xE0]
    return b"\xff\xd8" + b"".join(app0 + new + [s for s in keep if s[1] != 0xE0]) + data[i:]


def png_meta(data, title, desc, keywords, xmp):
    chunk = lambda t, b: len(b).to_bytes(4, "big") + t + b + zlib.crc32(t + b).to_bytes(4, "big")
    itxt = lambda k, v: chunk(b"iTXt", k.encode() + b"\x00\x00\x00\x00\x00" + v)
    ours = {b"XML:com.adobe.xmp", b"Title", b"Description", b"Keywords"}
    out, i = [data[:8]], 8
    while i < len(data):
        n = int.from_bytes(data[i:i + 4], "big")
        t, body = data[i + 4:i + 8], data[i + 8:i + 8 + n]
        if not (t in (b"iTXt", b"tEXt", b"zTXt") and body.split(b"\x00", 1)[0] in ours):
            out.append(data[i:i + 12 + n])
        if t == b"IHDR":
            out += [itxt("XML:com.adobe.xmp", xmp), itxt("Title", title.encode()),
                    itxt("Description", desc.encode()), itxt("Keywords", "; ".join(keywords).encode())]
        i += 12 + n
    return b"".join(out)


def write_with_meta(src, dst, info):
    data, kw = src.read_bytes(), [k for k in info["keywords"] if k][:12]
    xmp = xmp_packet(info["title"], info["description"], kw, info["topic"])
    try:
        if data[:2] == b"\xff\xd8":
            data = jpeg_meta(data, src, info["title"], info["description"], kw, xmp)
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            data = png_meta(data, info["title"], info["description"], kw, xmp)
    except Exception as e:  # метаданные — бонус, файл всё равно копируем
        print(f"  метаданные не записались в {dst.name}: {e}", file=sys.stderr)
        data = src.read_bytes()
    dst.write_bytes(data)
    st = src.stat()
    os.utime(dst, (st.st_atime, st.st_mtime))
    for k, v in (("user.xdg.comment", info["description"]), ("user.xdg.tags", ",".join(kw))):
        try:
            os.setxattr(dst, k, v.encode())
        except (OSError, AttributeError):  # AttributeError: на Windows нет setxattr
            pass


# ---------- раскладка ----------

def clean_name(s):
    s = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", s).strip(" .")
    return re.sub(r"\s+", " ", s)[:90] or "скриншот"


def place(verbose=True):
    topic_list, res = topics(), load_results()
    placed, taken = load_json(PLACED, {}), set()
    by_src = {p.relative_to(SRC).as_posix(): p for p in sources()}
    rows, ents = [], []
    for sha, r in sorted(res.items(), key=lambda kv: kv[1]["src"]):
        src = by_src.get(r["src"])
        info = final(r, topic_list) if src else None
        if not info:
            continue
        base = f"{shot_date(src)} {clean_name(info['title'])}"
        folder = ROOT / info["topic"]
        name, n = base + src.suffix.lower(), 2
        while str(folder / name) in taken:
            name, n = f"{base} ({n}){src.suffix.lower()}", n + 1
        dst = folder / name
        taken.add(str(dst))
        old = placed.get(sha)
        if old != str(dst) or not dst.exists():
            if old and Path(old).exists() and Path(old) != dst:
                Path(old).unlink()
            folder.mkdir(parents=True, exist_ok=True)
            write_with_meta(src, dst, info)
            placed[sha] = str(dst)
        rel = dst.relative_to(ROOT).as_posix()
        rows.append([str(rel), info["topic"], info["title"], info["description"],
                     ", ".join(info["keywords"]), f"{info['confidence']:.2f}", r["src"]])
        ents += [(e, info, rel) for e in info.get("entities", []) if e.get("name")]
    save_json(PLACED, placed)
    for d in sorted(ROOT.rglob("*"), reverse=True):  # пустые папки от старых тем
        if d.is_dir() and not d.name.startswith("_") and not any(d.iterdir()):
            d.rmdir()
    write_index(rows)
    write_dev(ents)
    if verbose:
        print(Counter(r[1] for r in rows).most_common())
        print(f"Готово: {len(rows)} файлов в {ROOT}")


def write_index(rows):
    import csv
    with (ROOT / "index.csv").open("w", newline="", encoding="utf-8-sig") as f:  # -sig: Excel поймёт кириллицу
        w = csv.writer(f)
        w.writerow(["файл", "тема", "название", "описание", "ключевые слова", "уверенность", "исходник"])
        w.writerows(rows)


def write_dev(ents):
    seen = {}
    for e, info, rel in ents:
        key = (e["kind"], e["name"].strip().lower())
        item = seen.setdefault(key, {"e": e, "files": [], "desc": info["description"]})
        if e.get("url") and not item["e"].get("url"):
            item["e"] = e
        item["files"].append(rel)
    lines = ["# Разработка: всё, что нашлось на скриншотах", "",
             "Ссылки собраны нейросетью с экрана. Если ссылки нет, значит на скриншоте её не было видно.", ""]
    for kind in KINDS:
        items = sorted((v for k, v in seen.items() if k[0] == kind), key=lambda v: v["e"]["name"].lower())
        if not items:
            continue
        lines += [f"## {KIND_TITLES[kind]} ({len(items)})", ""]
        for v in items:
            e = v["e"]
            url = e.get("url") or (f"https://github.com/{e['name']}" if kind == "github_repo"
                                   and re.fullmatch(r"[\w.-]+/[\w.-]+", e["name"]) else "")
            head = f"[{e['name']}]({url})" if url else e["name"]
            shots = ", ".join(f"[{i + 1}](<{f}>)" for i, f in enumerate(v["files"][:5]))
            desc = v["desc"].rstrip(" .") + "."
            lines.append(f"- **{head}**: {desc} Скриншоты: {shots}")
        lines.append("")
    (ROOT / "РАЗРАБОТКА.md").write_text("\n".join(lines), encoding="utf-8")


# ---------- команды ----------

def cmd_pull(_):
    adb = find_adb()
    out = run([adb, "get-state"], capture_output=True, text=True)
    if out.stdout.strip() != "device":
        sys.exit("Телефон не готов: " + (out.stderr.strip() or out.stdout.strip())
                 + "\nРазблокируй телефон и нажми «Разрешить отладку по USB».")
    SRC.mkdir(parents=True, exist_ok=True)
    for d in PHONE_DIRS:
        if run([adb, "shell", "test", "-d", d]).returncode == 0:
            print("Копирую", d)
            dest = SRC / Path(d).parent.name  # Pictures/ и DCIM/ отдельно, чтобы одинаковые имена не затёрлись
            dest.mkdir(parents=True, exist_ok=True)
            run([adb, "pull", "-a", d, str(dest)], check=True)
    print(f"На компьютере: {len(sources())} скриншотов в {SRC}. Телефон больше не нужен.")


def index_sources():
    cache = load_json(DATA / "sha_cache.json", {})
    out, changed = [], False
    for p in sources():
        st = p.stat()
        key = f"{p.relative_to(SRC)}|{st.st_size}|{int(st.st_mtime)}"
        if key not in cache:
            cache[key], changed = sha1(p), True
        out.append((cache[key], p))
    if changed:
        save_json(DATA / "sha_cache.json", cache)
    uniq = {}
    for sha, p in out:
        uniq.setdefault(sha, p)  # одинаковые скриншоты разбираем один раз
    return list(uniq.items())


def cmd_sample(a):
    ensure_ollama()
    items = index_sources()
    random.seed(1)
    pick = random.sample(items, min(a.n, len(items)))
    res = []
    lock = threading.Lock()

    def one(it):
        r = ask(FAST, it[1], None)
        with lock:
            res.append({"src": it[1].name, **r})

    print(f"Пробный прогон: {len(pick)} скриншотов")
    with ThreadPoolExecutor(1) as pool:
        list(pool.map(one, pick))
    save_json(DATA / "sample.json", res)
    for t, n in Counter(r["topic"] for r in res).most_common():
        print(f"{n:4}  {t}")


def cmd_run(a):
    ensure_ollama()
    topic_list, items = topics(), index_sources()
    res = load_results()
    todo = [(s, p) for s, p in items if "fast" not in res.get(s, {})]
    print(f"Всего {len(items)} уникальных, новых {len(todo)}")
    if todo:
        analyze(todo, FAST, "fast", topic_list)
    res = load_results()
    by = dict(items)
    todo = [(s, by[s]) for s, r in res.items() if s in by and needs_strong(r, topic_list)]
    if todo and not a.no_strong:
        print(f"Перепроверка сильной моделью: {len(todo)}")
        analyze(todo, STRONG, "strong", topic_list, workers=1)
    place()


CONTACT_HINT = "контакт, профил, звон, вызов, визитк, номер телефона, телефонн"


def cmd_recheck(a):
    """Поставить в очередь и заново разобрать скриншоты, где в прошлом ответе есть одно из слов a.match."""
    if a.add_topic and not a.dry_run:
        add_topic(a.add_topic)
    pat = words_pattern(a.match)
    if a.dry_run:
        print("Подходит:", sum(1 for r in load_results().values() if pat.search(answer_text(r.get("fast")))))
        return
    print("В очередь добавлено:", queue_matching(pat))
    if not a.queue_only:
        process_queue()


def process_queue():
    ensure_ollama()
    res, by = load_results(), dict(index_sources())
    todo = [(sha, by[sha]) for sha in load_json(QUEUE, []) if sha in by]
    print(f"Перепроверка: {len(todo)}")
    if todo:
        analyze(todo, FAST, "fast", topics())
        for sha, _ in todo:
            recheck_done(sha, res.get(sha, {}))
        place()


def recheck_done(sha, r):
    """Скриншот разобран заново: убрать из очереди и сбросить старую перепроверку сильной моделью."""
    if r.get("strong"):
        append_result({"sha": sha, "src": r["src"], "strong": None})
    with _lock:
        save_json(QUEUE, [x for x in load_json(QUEUE, []) if x != sha])


def cmd_place(_):
    place()


def cmd_app(_):
    import app
    app.main()


def main():
    for stream in (sys.stdout, sys.stderr):  # консоль Windows не в UTF-8: без этого кириллица роняет print
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pull").set_defaults(f=cmd_pull)
    s = sub.add_parser("sample")
    s.add_argument("-n", type=int, default=80)
    s.set_defaults(f=cmd_sample)
    r = sub.add_parser("run")
    r.add_argument("--no-strong", action="store_true", help="без перепроверки сильной моделью")
    r.set_defaults(f=cmd_run)
    sub.add_parser("place").set_defaults(f=cmd_place)
    rc = sub.add_parser("recheck", help="перепроверить часть скриншотов (по умолчанию — похожие на контакты)")
    rc.add_argument("--match", default=CONTACT_HINT, help="слова через запятую, которые ищем в прошлом ответе модели")
    rc.add_argument("--queue-only", action="store_true", help="только поставить в очередь (разберёт приложение)")
    rc.add_argument("--add-topic", default="Контакты и звонки", help="добавить тему, если её ещё нет ('' — не добавлять)")
    rc.add_argument("--dry-run", action="store_true", help="только посчитать")
    rc.set_defaults(f=cmd_recheck)
    sub.add_parser("app").set_defaults(f=cmd_app)
    a = ap.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    a.f(a)


if __name__ == "__main__":
    main()
