"""Поиск похожих скриншотов: маленькая модель SigLIP 2 превращает картинку в «отпечаток» (вектор),
по нему находятся примеры, которые владелец уже исправил или выделил. Работает на процессоре."""
import threading

import numpy as np
from PIL import Image

import sortshots as S

REPO = "https://huggingface.co/onnx-community/siglip2-base-patch16-224-ONNX/resolve/main/onnx/"
MODEL = S.APP_DATA / "models" / "siglip2" / "vision_model.onnx"
VECTORS = S.DATA / "vectors.npz"  # отпечатки уже посчитанных картинок: ключ → вектор
SIDE = 224
# Куски скриншота, в которых ищется выделенный образец: трети по высоте и середина
TILES = [(0, 1 / 3), (1 / 3, 2 / 3), (2 / 3, 1), (.25, .75)]
# Пороги сходства, подобраны на настоящих скриншотах: тот же экран 0.97–0.99,
# родственный (входящий звонок и набор номера) около 0.92, чужой около 0.7
SHOW = 0.85      # пример показываем нейросети
CROP_SHOW = 0.83 # выделенный кусок похож на часть скриншота (чужой экран даёт до 0.80)
QUEUE_SIM = 0.94 # после исправления переразобрать такие же

_sess, _lock, _vec_lock = None, threading.Lock(), threading.Lock()
_vecs = None


def ready():
    return MODEL.exists()


def fetch(progress=None):
    if not MODEL.exists():
        MODEL.parent.mkdir(parents=True, exist_ok=True)
        S.download(REPO + "vision_model.onnx", MODEL, progress)


def session():
    global _sess
    with _lock:
        if _sess is None:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 4  # не забирать весь процессор у остальной системы
            _sess = ort.InferenceSession(str(MODEL), opts, providers=["CPUExecutionProvider"])
        return _sess


def embed(images):
    """Список картинок PIL → нормированные векторы (косинусное сходство = скалярное произведение)."""
    batch = np.stack([np.asarray(im.convert("RGB").resize((SIDE, SIDE), Image.BILINEAR), np.float32) for im in images])
    batch = (batch / 255 - .5) / .5
    out = session().run(None, {"pixel_values": batch.transpose(0, 3, 1, 2)})
    v = out[1] if len(out) > 1 else out[0]  # pooler_output
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def tiles(im):
    w, h = im.size
    return [im.crop((0, int(a * h), w, max(int(a * h) + 1, int(b * h)))) for a, b in TILES]


# ---------- хранилище векторов ----------

def _load():
    global _vecs
    if _vecs is None:
        _vecs = {}
        if VECTORS.exists():
            with np.load(VECTORS) as z:
                _vecs = {k: z[k] for k in z.files}
    return _vecs


def save():
    with _vec_lock:
        tmp = VECTORS.with_name("vectors.tmp.npz")
        np.savez(tmp, **_load())
        tmp.replace(VECTORS)


def _cached(key, make):
    vs = _load()
    with _vec_lock:
        if key in vs:
            return vs[key].astype(np.float32)
    v = make()
    with _vec_lock:
        vs[key] = v.astype(np.float16)  # вдвое меньше места, на сходство не влияет
    return v


def shot_vector(sha, path):
    """Вектор скриншота целиком. Считается один раз (около 0.2 с на процессоре)."""
    def make():
        with Image.open(path) as im:
            return embed([im])[0]
    return _cached("shot:" + sha, make)


def tile_vectors(sha, path):
    """Векторы кусков скриншота: нужны, только когда есть выделенные образцы."""
    def make():
        with Image.open(path) as im:
            return embed(tiles(im))
    return _cached("tiles:" + sha, make)


def has_vector(sha):
    return "shot:" + sha in _load()


def crop_vector(rel):
    def make():
        with Image.open(S.DATA / rel) as im:
            return embed([im])[0]
    return _cached("crop:" + rel, make)


# ---------- примеры от владельца ----------

def examples(by):
    """Всё, чему научил владелец: выделенные куски, исправленные и прокомментированные скриншоты."""
    out = []
    for h in S.load_json(S.HINTS, []):
        if h.get("crop") and (S.DATA / h["crop"]).exists():
            out.append({"kind": "crop", "crop": h["crop"], "sha": h.get("sha"), "topic": h.get("topic", ""), "text": h["text"]})
        elif h.get("sha") in by:
            out.append({"kind": "shot", "sha": h["sha"], "topic": h.get("topic", ""), "text": h["text"]})
    for sha, n in S.load_json(S.NOTES, {}).items():
        if sha in by:
            if n.get("crop") and (S.DATA / n["crop"]).exists():
                out.append({"kind": "crop", "crop": n["crop"], "sha": sha, "topic": "", "text": n["text"]})
            out.append({"kind": "shot", "sha": sha, "topic": "", "text": n["text"]})
    for sha, ov in S.load_json(S.OVERRIDES, {}).items():
        topic = ov if isinstance(ov, str) else (ov or {}).get("topic")
        if sha in by and topic and topic != S.CHECK:
            out.append({"kind": "shot", "sha": sha, "topic": topic, "text": ""})
    return out


def nearest(sha, path, by, k=3):
    """Самые похожие примеры владельца для скриншота: [(сходство, пример)], лучшие первыми."""
    if not ready():
        return []
    v = shot_vector(sha, path)  # считаем всегда: вектор пригодится, чтобы потом найти похожие на исправленный
    ex = [e for e in examples(by) if e.get("sha") != sha]  # сам себе не пример
    if not ex:
        return []
    t = np.vstack([v, tile_vectors(sha, path)]) if any(e["kind"] == "crop" for e in ex) else None
    scored = []
    for e in ex:
        if e["kind"] == "crop":  # выделенный кусок ищем во всём скриншоте и в его частях
            score = float((t @ crop_vector(e["crop"])).max())
            if score >= CROP_SHOW:
                scored.append((score, e))
        else:
            score = float(v @ shot_vector(e["sha"], by[e["sha"]]))
            if score >= SHOW:
                scored.append((score, dict(e, path=by[e["sha"]])))
    scored.sort(key=lambda x: -x[0])
    seen, out = set(), []
    for score, e in scored:
        key = (e.get("sha"), e["kind"])
        if key not in seen:
            seen.add(key)
            out.append((round(score, 3), e))
        if len(out) == k:
            break
    return out


def similar_shots(sha, by, done, threshold=QUEUE_SIM):
    """Уже разобранные скриншоты, похожие на этот целиком (для перепроверки после исправления).
    Смотрим только тех, у кого вектор уже посчитан: он считается при каждом разборе."""
    if not ready() or sha not in by:
        return []
    v = shot_vector(sha, by[sha])
    vs = _load()
    with _vec_lock:
        cand = [(s, vs["shot:" + s]) for s in done if s != sha and "shot:" + s in vs]
    if not cand:
        return []
    sims = np.stack([c[1] for c in cand]).astype(np.float32) @ v
    return [cand[i][0] for i in np.flatnonzero(sims >= threshold)]
