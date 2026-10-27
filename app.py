#!/usr/bin/env python3
"""Приложение для разбора скриншотов: живой прогресс, папки, проверка. http://127.0.0.1:8765"""
import json, mimetypes, shutil, socket, subprocess, sys, threading, time, traceback, urllib.request
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image

import similar as SIM
import sortshots as S

HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)).resolve()  # _MEIPASS — внутри собранного .exe
STATIC = (HERE / "static").resolve()
THUMBS = S.DATA / "thumbs"
PORT = 8765
SETTINGS = S.DATA / "settings.json"
OLLAMA_SETUP_URL = "https://ollama.com/download/OllamaSetup.exe"
PICK_FOLDER = None  # окно приложения подставляет сюда системный диалог выбора папки
STAGE_NAMES = {"fast": "быстрая модель", "strong": "перепроверка сильной моделью"}


class Worker:
    def __init__(self):
        self.lock = threading.Lock()
        self.place_lock = threading.Lock()
        self.phase, self.msg = "idle", ""
        self.pause, self.stop = threading.Event(), threading.Event()
        self.thread = None
        self.current = {}
        self.recent = deque(maxlen=40)
        self.errors = deque(maxlen=20)
        self.stats = {}
        self.models = {}
        self.phone = {}
        self.setup = {"ollama": "проверяю"}
        self.res = S.load_results()
        self.by = dict(S.index_sources()) if S.SRC.exists() else {}
        self.last_place = 0
        self.skip = set()  # не разобрались из-за ошибки в этом запуске
        self.done_since_save = 0
        self.sim_status = "готова" if SIM.ready() else "скачается при запуске"
        threading.Thread(target=self.check_models, daemon=True).start()

    # ---- состояние для страницы ----

    def snapshot(self):
        with self.lock:
            topics = S.topics()
            fast_done = sum(1 for s in self.by if "fast" in self.res.get(s, {}))
            strong_need = sum(1 for s in self.by if self.res.get(s, {}).get("fast")
                              and (S.needs_strong(self.res[s], topics) or self.res[s].get("strong")))
            strong_done = sum(1 for s in self.by if self.res.get(s, {}).get("strong"))
            placed = S.load_json(S.PLACED, {})
            tree = Counter(Path(p).parent.relative_to(S.ROOT).as_posix() for s, p in placed.items() if s in self.by)
            for t in topics + [S.CHECK]:  # пустые папки тоже показываем, чтобы структура была видна сразу
                tree.setdefault(t, 0)
            now = time.time()
            return {
                "phase": self.phase, "msg": self.msg,
                "running": bool(self.thread and self.thread.is_alive()), "paused": self.pause.is_set(),
                "total": len(self.by), "fast_done": fast_done,
                "strong_need": strong_need, "strong_done": strong_done,
                "eta_min": self.eta(fast_done, strong_need, strong_done),
                "speed": {k: self.speed(k) for k in ("fast", "strong")},
                "current": [dict(v, sha=k, secs=round(now - v["since"])) for k, v in self.current.items()],
                "recent": list(self.recent)[::-1],
                "tree": sorted(tree.items()),
                "models": self.models, "phone": self.phone, "setup": self.setup, "similar": self.sim_status, "windows": S.WIN,
                "errors": list(self.errors)[::-1],
                "root": str(S.ROOT), "topics": topics,
                "settings": S.load_json(SETTINGS, {"strong": True}),
                "queue": len(S.load_json(S.QUEUE, [])), "hints": S.load_json(S.HINTS, []),
            }

    def speed(self, stage):
        st = self.stats.get(stage)
        if not st or not st["times"]:
            return None
        return sum(st["times"]) / len(st["times"]) / st["workers"]  # секунд на скриншот с учётом параллельности

    def eta(self, fast_done, strong_need, strong_done):
        left_fast = len(self.by) - fast_done + len(S.load_json(S.QUEUE, []))
        minutes = left_fast * (self.speed("fast") or 4.0)
        if S.load_json(SETTINGS, {"strong": True})["strong"]:
            share = strong_need / fast_done if fast_done else 0.3  # какая доля уйдёт на перепроверку
            minutes += (strong_need - strong_done + left_fast * share) * (self.speed("strong") or 40.0)
        return round(minutes / 60, 1)

    # ---- модели ----

    def tags(self):
        try:
            with urllib.request.urlopen(S.OLLAMA + "/api/tags", timeout=3) as r:
                return {m["name"] for m in json.loads(r.read())["models"]}
        except OSError:
            return None

    def check_models(self):
        try:
            S.ensure_ollama()
        except S.OllamaMissing:
            self.setup = {"ollama": "нет"}
            return
        except Exception as e:
            self.setup = {"ollama": "ошибка", "error": str(e)}
            return
        self.setup = {"ollama": "готова"}
        have = self.tags() or set()
        for m in (S.FAST, S.STRONG):
            self.models[m] = {"status": "готова", "pct": 100} if m in have else {"status": "нет", "pct": 0}

    def install_ollama(self):
        """Windows: скачать официальный установщик Ollama и поставить его тихо, без вопросов."""
        try:
            exe = S.APP_DATA / "OllamaSetup.exe"
            self.setup = {"ollama": "качается", "pct": 0}
            S.download(OLLAMA_SETUP_URL, exe, lambda d, t: self.setup.update(pct=round(d * 100 / t) if t else 0))
            self.setup = {"ollama": "ставится"}
            S.run([str(exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"], check=True)
            exe.unlink(missing_ok=True)
            for _ in range(60):  # после установки Ollama стартует сама; ждём её
                if S.ollama_up():
                    break
                time.sleep(1)
            self.check_models()
            if self.setup["ollama"] == "готова":
                self.start_pull(S.FAST)
        except Exception as e:
            self.setup = {"ollama": "ошибка", "error": f"Не удалось поставить Ollama: {e}"}

    def start_pull(self, model):
        def go():
            try:
                self.pull(model)
            except Exception as e:
                self.models[model] = {"status": "ошибка", "pct": 0}
                self.errors.append(str(e))
        threading.Thread(target=go, daemon=True).start()

    def pull(self, model):
        if self.models.get(model, {}).get("status") == "готова":
            return
        self.models[model] = {"status": "качается", "pct": 0}
        req = urllib.request.Request(S.OLLAMA + "/api/pull", json.dumps({"model": model}).encode())
        parts = {}  # модель качается несколькими файлами, считаем прогресс по всем сразу
        with urllib.request.urlopen(req, timeout=3600) as r:
            for line in r:
                ev = json.loads(line)
                if ev.get("error"):
                    raise RuntimeError(f"{model}: {ev['error']}")
                if ev.get("total") and ev.get("digest"):
                    parts[ev["digest"]] = (ev.get("completed", 0), ev["total"])
                    done, total = sum(c for c, _ in parts.values()), sum(t for _, t in parts.values())
                    self.models[model] = {"status": "качается", "pct": round(done * 100 / total), "gb": round(total / 1e9, 1)}
        self.models[model] = {"status": "готова", "pct": 100}

    # ---- копирование с телефона ----

    def copy_phone(self):
        self.phase, self.msg = "phone", "Готовлю adb для связи с телефоном"
        adb = S.find_adb(lambda d, t: self.phone.update(adb_pct=round(d * 100 / t) if t else 0))
        self.msg = "Проверяю телефон"
        st = S.run([adb, "get-state"], capture_output=True, text=True)
        if st.stdout.strip() != "device":
            raise RuntimeError("Телефон не найден: подключи его кабелем, включи «Отладку по USB», "
                               "разблокируй и нажми «Разрешить». "
                               + (st.stderr.strip() or st.stdout.strip()))
        for d in S.PHONE_DIRS:
            if S.run([adb, "shell", "test", "-d", d]).returncode:
                continue
            n = S.run([adb, "shell", f"ls -1 '{d}' | wc -l"], capture_output=True, text=True).stdout.strip()
            dest = S.SRC / Path(d).parent.name  # Pictures/ и DCIM/ отдельно, чтобы одинаковые имена не затёрлись
            dest.mkdir(parents=True, exist_ok=True)
            self.phone = {"dir": d, "total": int(n or 0), "copied": 0}
            self.msg = f"Копирую {d}"
            proc = S.popen([adb, "pull", "-a", d, str(dest)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            target = dest / Path(d).name
            while proc.poll() is None:
                self.phone["copied"] = sum(1 for _ in target.glob("*")) if target.exists() else 0
                time.sleep(0.5)
            if proc.returncode:
                raise RuntimeError("adb pull: " + proc.stderr.read().decode()[-300:])
            self.phone["copied"] = self.phone["total"]
        self.by = dict(S.index_sources())
        self.msg = f"Скопировано {len(self.by)} скриншотов. Телефон можно отключать."

    def import_folder(self, folder):
        """Скопировать картинки из выбранной папки в _исходники/<имя папки>; оригиналы не трогаем."""
        folder = Path(folder)
        self.phase, self.msg = "phone", f"Копирую из {folder}"
        files = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in S.EXTS]
        if not files:
            raise RuntimeError(f"В папке {folder} нет картинок (png, jpg, webp)")
        dest = S.SRC / S.clean_name(folder.name)
        self.phone = {"dir": str(folder), "total": len(files), "copied": 0}
        for i, f in enumerate(files, 1):
            target = dest / f.relative_to(folder)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
            self.phone["copied"] = i
        self.by = dict(S.index_sources())
        self.msg = f"Скопировано {len(files)} картинок из {folder}."

    # ---- разбор ----

    def place(self, force=False):
        if not force and time.time() - self.last_place < 3:
            return
        with self.place_lock:
            S.place(verbose=False)
            self.last_place = time.time()
            placed = S.load_json(S.PLACED, {})
            with self.lock:
                for r in self.recent:
                    if r["sha"] in placed:
                        r["file"] = Path(placed[r["sha"]]).relative_to(S.ROOT).as_posix()

    def fast_todo(self):
        """Сначала очередь перепроверки (её пополняют прямо во время разбора), потом ещё не разобранные."""
        while not self.stop.is_set():
            queue = [s for s in S.load_json(S.QUEUE, []) if s in self.by]
            sha = queue[0] if queue else next(
                (s for s in self.by if s not in self.skip and "fast" not in self.res.get(s, {})), None)
            if not sha:
                return
            self.msg = (f"перепроверка по подсказкам: в очереди {len(queue)}" if queue
                        else f"{STAGE_NAMES['fast']}: осталось {len(self.by) - len(self.res)}")
            yield sha, self.by[sha]

    def failed(self, sha):
        """Битый файл не крутим по кругу: убираем из очереди и пропускаем до следующего запуска."""
        self.skip.add(sha)
        with S._lock:
            S.save_json(S.QUEUE, [x for x in S.load_json(S.QUEUE, []) if x != sha])

    def run_pass(self, todo, model, stage, topics, workers, label=None, after=None):
        self.phase = stage
        if isinstance(todo, list):
            self.msg = f"{label or STAGE_NAMES[stage]}: {len(todo)} шт."
        self.stats[stage] = {"times": deque(maxlen=20), "workers": workers}
        it, it_lock = iter(todo), threading.Lock()

        def loop():
            while not self.stop.is_set():
                while self.pause.is_set() and not self.stop.is_set():
                    time.sleep(0.3)
                with it_lock:
                    nxt = next(it, None)
                if not nxt or self.stop.is_set():
                    return
                sha, p = nxt
                self.current[sha] = {"name": p.name, "stage": stage, "since": time.time()}
                t0 = time.time()
                topics = S.topics()  # новая папка из подсказки доступна модели сразу
                try:
                    try:  # похожие примеры владельца; без них разбор всё равно идёт
                        similar = SIM.nearest(sha, p, self.by)
                    except Exception as e:
                        similar = None
                        self.errors.append(f"поиск похожих: {e}")
                    out = S.ask(model, p, topics, sha=sha, similar=similar)
                    S.append_result({"sha": sha, "src": p.relative_to(S.SRC).as_posix(), stage: out})
                    with self.lock:
                        r = self.res.setdefault(sha, {"sha": sha, "src": p.relative_to(S.SRC).as_posix()})
                        r[stage] = out
                        fin = S.final(r, topics)
                        self.recent.append({"sha": sha, "name": p.name, "stage": stage, "topic": fin["topic"],
                                            "title": fin["title"], "conf": round(out["confidence"], 2),
                                            "secs": round(time.time() - t0, 1)})
                    self.stats[stage]["times"].append(time.time() - t0)
                    if after:
                        after(sha)
                    self.done_since_save += 1
                    if self.done_since_save >= 25:  # векторы похожести не терять при сбое
                        self.save_vectors()
                    self.place()
                except Exception as e:  # битый файл или сбой модели: записываем и идём дальше
                    self.errors.append(f"{p.name}: {e}")
                    self.failed(sha)
                finally:
                    self.current.pop(sha, None)

        threads = [threading.Thread(target=loop) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def run(self, source):
        try:
            self.stop.clear()
            self.pause.clear()
            if source == "phone":
                self.copy_phone()
            elif source:
                self.import_folder(source)
            try:
                S.ensure_ollama()
            except S.OllamaMissing:
                self.setup = {"ollama": "нет"}
                raise RuntimeError("Сначала установи Ollama: кнопка в карточке «Модели»") from None
            self.phase, self.msg = "models", "Проверяю модели"
            self.pull(S.FAST)
            self.fetch_similar()
            strong = threading.Thread(target=self.pull, args=(S.STRONG,), daemon=True)
            strong.start()  # сильная модель докачивается, пока работает быстрая
            topics = S.topics()
            self.by = dict(S.index_sources())
            if not self.by:
                raise RuntimeError("Скриншотов нет: выбери папку или скопируй их с телефона")
            self.skip = set()
            while True:
                # на 6 ГБ видеокарты параллельно только медленнее; очередь по подсказкам идёт первой
                self.run_pass(self.fast_todo(), S.FAST, "fast", topics, 1, after=self.requeued)
                if self.stop.is_set():
                    return
                self.place(force=True)
                if not S.load_json(SETTINGS, {"strong": True})["strong"]:
                    if self.queue_left():
                        continue
                    self.phase, self.msg = "done", "Готово (без перепроверки сильной моделью)."
                    return
                if self.models.get(S.STRONG, {}).get("status") != "готова":
                    self.phase, self.msg = "models", "Жду, пока докачается сильная модель"
                    strong.join()
                topics = S.topics()
                todo = [(s, self.by[s]) for s, r in list(self.res.items()) if s in self.by and S.needs_strong(r, topics)]
                self.run_pass(todo, S.STRONG, "strong", topics, 1)
                if self.stop.is_set():
                    return
                if not self.queue_left():  # пока шла сильная модель, могли попросить переразобрать
                    break
            self.phase, self.msg = "place", "Раскладываю по папкам"
            self.place(force=True)
            self.phase, self.msg = "done", "Готово. Загляни в папку «Проверить»."
        except Exception as e:
            self.errors.append(str(e))
            self.phase, self.msg = "error", str(e)
            traceback.print_exc()
        finally:
            self.save_vectors()
            if self.stop.is_set():
                self.place(force=True)
                self.phase, self.msg = "idle", "Остановлено. Продолжить можно в любой момент."
            self.current.clear()

    def fetch_similar(self):
        """Модель поиска похожих (около 370 МБ, один раз). Не скачалась — разбор идёт без неё."""
        if SIM.ready():
            self.sim_status = "готова"
            return
        self.phase, self.msg = "models", "Скачиваю модель поиска похожих скриншотов"
        try:
            SIM.fetch(lambda done, total: setattr(self, "sim_status", f"скачивается {done * 100 // max(total, 1)}%"))
            self.sim_status = "готова"
        except Exception as e:
            self.sim_status = "ошибка"
            self.errors.append(f"модель поиска похожих: {e}")

    def save_vectors(self):
        self.done_since_save = 0
        try:
            SIM.save()
        except Exception as e:
            self.errors.append(f"векторы похожих: {e}")

    def queue_similar(self, sha):
        """Исправил один — переразобрать такие же экраны, уже разобранные раньше. Вернёт сколько."""
        try:
            with self.lock:
                done = [s for s in self.by if "fast" in self.res.get(s, {})]
            return S.queue_add(SIM.similar_shots(sha, self.by, done))
        except Exception as e:
            self.errors.append(f"поиск похожих: {e}")
            return 0

    def queue_left(self):
        return any(s in self.by for s in S.load_json(S.QUEUE, []))

    def requeued(self, sha):
        r = self.res.get(sha, {})
        S.recheck_done(sha, r)
        r["strong"] = None

    # ---- подсказки ----

    def add_hint(self, mode, text, topic, words, sha, title=None, box=None):
        """mode: one — поправить только этот скриншот вручную; redo — переразобрать его с комментарием;
        all — запомнить правило (и образец, если выделена часть картинки) для всех следующих."""
        if topic and topic != S.CHECK:  # новая папка сразу становится темой
            S.add_topic(topic)
        if mode == "redo":
            return self.redo_one(sha, text, topic, box)
        if sha and (topic or title):
            self.set_topic(sha, topic, title)
        queued = self.queue_similar(sha) if sha and (topic or mode == "all") else 0
        if mode == "one":
            return {"ok": True, "queued": queued}
        hid = int(time.time() * 1000)
        hint = {"id": hid, "text": text.strip(), "topic": topic or "", "words": words.strip(),
                "sha": sha or "", "created": time.strftime("%Y-%m-%d %H:%M")}
        if box and sha in self.by:
            hint["crop"] = S.save_crop(self.by[sha], box, f"hint_{hid}")
        S.save_json(S.HINTS, S.load_json(S.HINTS, []) + [hint])
        pat = S.words_pattern(words)
        if pat:
            with self.lock:
                queued += S.queue_matching(pat, self.res, skip={sha})
        return {"ok": True, "queued": queued}

    def redo_one(self, sha, text, topic, box):
        """Переразобрать один скриншот с комментарием владельца: он встаёт в очередь первым."""
        if sha not in self.by:
            return {"ok": False, "error": "скриншот не найден"}
        note = {"text": text.strip() + (f" Правильная папка: «{topic}»." if topic else "")}
        if box:
            note["crop"] = S.save_crop(self.by[sha], box, f"note_{sha[:16]}")
        notes = S.load_json(S.NOTES, {})
        notes[sha] = note
        S.save_json(S.NOTES, notes)
        ov = S.load_json(S.OVERRIDES, {})
        if ov.pop(sha, None) is not None:  # прошлое ручное исправление больше не держит скриншот на месте
            S.save_json(S.OVERRIDES, ov)
        try:  # такие же экраны, скорее всего, разобраны так же неверно — следом за ним
            with self.lock:
                done = [s for s in self.by if "fast" in self.res.get(s, {})]
            same = SIM.similar_shots(sha, self.by, done)
        except Exception as e:
            same = []
            self.errors.append(f"поиск похожих: {e}")
        S.queue_add([sha] + same, front=True)
        return {"ok": True, "queued": 1 + len(same), "running": bool(self.thread and self.thread.is_alive())}

    def redo_all(self):
        """После обучения: заново разобрать всё, что уже разобрано. Ручные исправления остаются."""
        with self.lock:
            done = [s for s in self.by if "fast" in self.res.get(s, {})]
        return S.queue_add(done)

    def delete_hint(self, hid):
        hints = S.load_json(S.HINTS, [])
        for h in hints:
            if h["id"] == hid and h.get("crop"):
                (S.DATA / h["crop"]).unlink(missing_ok=True)
        S.save_json(S.HINTS, [h for h in hints if h["id"] != hid])

    def start(self, source=None):
        if self.thread and self.thread.is_alive():
            return False
        self.thread = threading.Thread(target=self.run, args=(source,), daemon=True)
        self.thread.start()
        return True

    # ---- проверка ----

    def folder(self, rel):
        topics, placed = S.topics(), S.load_json(S.PLACED, {})
        out = []
        for sha, p in placed.items():
            if sha in self.res and Path(p).parent.relative_to(S.ROOT).as_posix() == rel:
                f = S.final(self.res[sha], topics)
                out.append({"sha": sha, "file": Path(p).name, "topic": f["topic"], "title": f["title"],
                            "description": f["description"], "conf": round(f["confidence"], 2),
                            "entities": f.get("entities", [])})
        return sorted(out, key=lambda x: x["file"])

    def set_topic(self, sha, topic=None, title=None):
        """Ручное исправление одного скриншота: папка и/или название."""
        ov = S.load_json(S.OVERRIDES, {})
        cur = ov.get(sha) or {}
        cur = {"topic": cur} if isinstance(cur, str) else cur
        if topic:
            cur["topic"] = topic
        if title:
            cur["title"] = title.strip()
        ov[sha] = cur
        S.save_json(S.OVERRIDES, ov)
        self.place(force=True)


W = None


def thumb(sha, width):
    THUMBS.mkdir(parents=True, exist_ok=True)
    f = THUMBS / f"{sha}_{width}.jpg"
    if not f.exists():
        p = W.by.get(sha)
        if not p:
            return None
        with Image.open(p) as im:
            im = im.convert("RGB")
            im.thumbnail((width, width * 3))
            im.save(f, "JPEG", quality=82)
    return f.read_bytes()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json", code=200):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if ctype == "image/jpeg" else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/":
            return self.send((STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if u.path.startswith("/static/"):
            f = (STATIC / u.path[8:]).resolve()
            if f.parent != STATIC or not f.exists():
                return self.send_error(404)
            return self.send(f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")
        if u.path.startswith("/img/"):
            body = thumb(u.path[5:], int(q.get("w", 320)))
            return self.send(body, "image/jpeg") if body else self.send_error(404)
        if u.path.startswith("/examples/"):
            f = (S.EXAMPLES / u.path[10:]).resolve()
            if f.parent != S.EXAMPLES.resolve() or not f.exists():
                return self.send_error(404)
            return self.send(f.read_bytes(), "image/jpeg")
        if u.path == "/api/folder":
            return self.send(W.folder(q.get("path", "")))
        if u.path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b"data: " + json.dumps(W.snapshot(), ensure_ascii=False).encode() + b"\n\n")
                    self.wfile.flush()
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError):
                return
        self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        p = urlparse(self.path).path
        if p == "/api/start":
            return self.send({"ok": W.start("phone" if body.get("phone") else None)})
        if p == "/api/import":
            folder = pick_folder()
            return self.send({"ok": bool(folder) and W.start(folder), "folder": folder})
        if p == "/api/install-ollama":
            if S.WIN and W.setup.get("ollama") in ("нет", "ошибка"):
                threading.Thread(target=W.install_ollama, daemon=True).start()
            return self.send({"ok": S.WIN})
        if p == "/api/hints":
            mode = body.get("mode") if body.get("mode") in ("one", "redo") else "all"
            box = body.get("box")
            if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)
                    and box[2] > 0.01 and box[3] > 0.01):
                box = None  # случайный клик вместо выделения
            if mode == "all" and not ((body.get("text") or "").strip() or box):
                return self.send({"ok": False, "error": "напиши, что запомнить"}, code=400)
            if mode == "redo" and not ((body.get("text") or "").strip() or body.get("topic")):
                return self.send({"ok": False, "error": "напиши, что на скриншоте на самом деле"}, code=400)
            return self.send(W.add_hint(mode, body.get("text") or "", body.get("topic"), body.get("words") or "",
                                        body.get("sha"), body.get("title"), box))
        if p == "/api/redo-all":
            return self.send({"ok": True, "queued": W.redo_all()})
        if p == "/api/queue/clear":
            with S._lock:
                S.save_json(S.QUEUE, [])
            return self.send({"ok": True})
        if p == "/api/hints/delete":
            W.delete_hint(body.get("id"))
            return self.send({"ok": True})
        if p == "/api/pull":
            if body.get("model") in (S.FAST, S.STRONG):
                W.start_pull(body["model"])
            return self.send({"ok": True})
        if p == "/api/pause":
            W.pause.set()
        elif p == "/api/resume":
            W.pause.clear()
        elif p == "/api/stop":
            W.stop.set()
        elif p == "/api/set":
            W.set_topic(body["sha"], body["topic"])
            return self.send({"ok": True, "queued": W.queue_similar(body["sha"])})
        elif p == "/api/topics":
            t = [x.strip() for x in body["topics"] if x.strip()]
            S.save_json(S.TOPICS, t)
        elif p == "/api/settings":
            S.save_json(SETTINGS, {"strong": bool(body.get("strong"))})
        elif p == "/api/open":
            f = (S.ROOT / body.get("path", "")).resolve()
            root = S.ROOT.resolve()
            if f == root or root in f.parents:
                f.mkdir(parents=True, exist_ok=True)
                S.open_path(f)
        else:
            return self.send_error(404)
        self.send({"ok": True})


def pick_folder():
    if PICK_FOLDER:
        return PICK_FOLDER()
    if shutil.which("zenity"):  # запуск без окна приложения (Linux, в браузере)
        r = S.run(["zenity", "--file-selection", "--directory", "--title=Папка со скриншотами"],
                  capture_output=True, text=True)
        return r.stdout.strip() or None
    return None


def port_free(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def serve(port=PORT):
    """Запустить Worker и HTTP-сервер (без блокировки). Вернёт сервер."""
    global W
    S.DATA.mkdir(parents=True, exist_ok=True)
    W = Worker()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    serve()
    print(f"http://127.0.0.1:{PORT}")
    threading.Event().wait()


if __name__ == "__main__":
    main()
