"""Поиск похожих скриншотов: маленькая модель SigLIP 2 превращает картинку в «отпечаток» (вектор),
по нему находятся примеры, которые владелец уже исправил или выделил. Работает на процессоре."""
import threading
from collections import Counter

import numpy as np
from PIL import Image

import sortshots as S

REPO = "https://huggingface.co/onnx-community/siglip2-base-patch16-224-ONNX/resolve/main/onnx/"
MODEL = S.APP_DATA / "models" / "siglip2" / "vision_model.onnx"
# Текстовая половина той же модели: запрос словами → вектор в том же пространстве, что и картинки.
# int8 (270 МБ вместо 1.1 ГБ): на поиске по скриншотам разницы не видно.
TEXT_MODEL = MODEL.with_name("text_model_int8.onnx")
TOKENIZER = MODEL.with_name("tokenizer.json")
VECTORS = S.DATA / "vectors.npz"  # отпечатки уже посчитанных картинок: ключ → вектор
SIDE = 224
# Куски скриншота, в которых ищется выделенный образец: трети по высоте и середина
TILES = [(0, 1 / 3), (1 / 3, 2 / 3), (2 / 3, 1), (.25, .75)]
# Пороги сходства, подобраны на настоящих скриншотах: тот же экран 0.97–0.99,
# родственный (входящий звонок и набор номера) около 0.92, чужой около 0.7
SHOW = 0.85      # пример показываем нейросети
# Выделенный кусок: порог свой у каждого. Кнопки набора номера похожи на чужие экраны на 0.80,
# кусок пакета семечек — на 0.59, поэтому порог = «как похож на случайные скриншоты» + запас.
CROP_MARGIN, CROP_MIN, CROP_MAX = 0.06, 0.60, 0.85
QUEUE_SIM = 0.94 # после исправления переразобрать такие же

_sess, _lock, _vec_lock = None, threading.Lock(), threading.Lock()
_vecs = None


def ready():
    return MODEL.exists()


def text_ready():
    return TEXT_MODEL.exists() and TOKENIZER.exists()


def fetch(progress=None):
    for url, dest in ((REPO + "vision_model.onnx", MODEL), (REPO + "text_model_int8.onnx", TEXT_MODEL),
                      (REPO.replace("onnx/", "tokenizer.json"), TOKENIZER)):
        if not dest.exists():
            S.download(url, dest, progress)


def session():
    global _sess
    with _lock:
        if _sess is None:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 4  # не забирать весь процессор у остальной системы
            _sess = ort.InferenceSession(str(MODEL), opts, providers=["CPUExecutionProvider"])
        return _sess


_text = None


def text_vector(query):
    """Запрос словами → вектор. Как у SigLIP 2: нижний регистр, <eos> в конце, добивка до 64 токенов."""
    global _text
    with _lock:
        if _text is None:
            import onnxruntime as ort
            from tokenizers import Tokenizer
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 4
            _text = (Tokenizer.from_file(str(TOKENIZER)),
                     ort.InferenceSession(str(TEXT_MODEL), opts, providers=["CPUExecutionProvider"]))
    tok, sess = _text
    eos, pad = tok.token_to_id("<eos>"), tok.token_to_id("<pad>")
    ids = [i for i in tok.encode(query.lower()).ids if i != eos][:63] + [eos]
    ids += [pad] * (64 - len(ids))
    v = sess.run(None, {"input_ids": np.array([ids], dtype=np.int64)})[1][0]
    return v / np.linalg.norm(v)


def by_meaning(query, shas):
    """Сходство запроса с каждым скриншотом, у которого уже есть вектор: {sha: сходство}."""
    if not (ready() and text_ready()):
        return {}
    vs = _load()
    with _vec_lock:
        have = [(s, vs["shot:" + s]) for s in shas if "shot:" + s in vs]
    if not have:
        return {}
    sims = np.stack([v for _, v in have]).astype(np.float32) @ text_vector(query)
    return {s: float(x) for (s, _), x in zip(have, sims)}


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


_crop_limits = {}


def crop_threshold(rel):
    """Порог для выделенного куска: выше, чем у 99% уже разобранных скриншотов, плюс запас."""
    if rel not in _crop_limits:
        vs = _load()
        with _vec_lock:
            bg = [v for k, v in vs.items() if k.startswith("tiles:")][:400] or \
                 [v[None] for k, v in vs.items() if k.startswith("shot:")][:1500]
        if not bg:
            return CROP_MAX
        sims = (np.concatenate(bg).astype(np.float32) @ crop_vector(rel))
        per_shot = sims.reshape(len(bg), -1).max(1) if bg[0].ndim == 2 and len(bg[0]) > 1 else sims
        _crop_limits[rel] = float(np.clip(np.percentile(per_shot, 99) + CROP_MARGIN, CROP_MIN, CROP_MAX))
    return _crop_limits[rel]


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
            if score >= crop_threshold(e["crop"]):
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


SAME_GROUP = 0.98  # почти одинаковые экраны: тот же набор номера, тот же калькулятор


def majority(topic_of, voters=None, min_share=0.6):
    """Почти одинаковые экраны должны лежать в одной папке. topic_of — {sha: тема от нейросети},
    voters — чьё мнение считать (без тех, кто ждёт переразбора: их тема устарела).
    Решение на всю группу сразу, иначе одни переезжают туда, другие оттуда.
    Вернёт {sha: (тема группы, размер группы)} для тех, кто лежит не так, как группа."""
    vs = _load()
    with _vec_lock:
        have = [(s, vs["shot:" + s]) for s in topic_of if "shot:" + s in vs]
    if len(have) < 3:
        return {}
    shas = [s for s, _ in have]
    V = np.stack([v for _, v in have]).astype(np.float32)
    parent = list(range(len(shas)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i0 in range(0, len(shas), 512):  # кусками: вся матрица на 6000 скриншотов — 140 МБ
        block = V[i0:i0 + 512] @ V.T
        for k, row in enumerate(block):
            for j in np.flatnonzero(row >= SAME_GROUP):
                a, b = root(i0 + k), root(j)
                if a != b:
                    parent[a] = b
    groups = {}
    for i, s in enumerate(shas):
        groups.setdefault(root(i), []).append(s)
    voters = set(voters if voters is not None else topic_of)
    out = {}
    for members in groups.values():
        votes = Counter(topic_of[s] for s in members if s in voters)
        if len(members) < 3 or not votes:
            continue
        top, n = votes.most_common(1)[0]
        if n >= min_share * sum(votes.values()):
            out.update({s: (top, len(members)) for s in members if topic_of[s] != top})
    return out


# ---------- классификатор по разметке ----------

_model = (None, None)  # (по каким файлам обучен, (темы, W, b))


def _fit(X, y, k, l2=1e-3, steps=500, lr=0.5, scale=20):
    """Логистическая регрессия поверх векторов SigLIP (около секунды на тысячу примеров)."""
    W, b, Y = np.zeros((X.shape[1], k), np.float32), np.zeros(k, np.float32), np.eye(k, dtype=np.float32)[y]
    for _ in range(steps):
        z = X @ W * scale + b
        p = np.exp(z - z.max(1, keepdims=True))
        p /= p.sum(1, keepdims=True)
        g = (p - Y) / len(X)
        W -= lr * (scale * X.T @ g + l2 * W)
        b -= lr * g.sum(0)
    return W, b


def learn(min_examples=20):
    """Учится на разметке (labels.json) и ручных исправлениях, раскладывает все скриншоты с вектором
    и пишет learned.json. Заново обучается, только если разметка или исправления изменились.
    На 800 размеченных: 84 % на чужих 100 скриншотах против 72 % у нейросети."""
    global _model
    taught = dict(S.load_json(S.LABELS, {}))
    for sha, ov in S.load_json(S.OVERRIDES, {}).items():
        topic = ov if isinstance(ov, str) else (ov or {}).get("topic")
        if topic and topic != S.CHECK:
            taught[sha] = topic
    vs = _load()
    with _vec_lock:
        shas = [k[5:] for k in vs if k.startswith("shot:")]
        V = np.stack([vs["shot:" + s] for s in shas]).astype(np.float32) if shas else None
    have = [s for s in taught if "shot:" + s in vs]
    if V is None or len(have) < min_examples or len({taught[s] for s in have}) < 2:
        S.save_json(S.LEARNED, {})
        return {}
    key = tuple(sorted((s, taught[s]) for s in have))
    if _model[0] != key:
        topics = sorted({taught[s] for s in have})
        X = np.stack([vs["shot:" + s] for s in have]).astype(np.float32)
        W, b = _fit(X, np.array([topics.index(taught[s]) for s in have]), len(topics))
        _model = (key, (topics, W, b))
    topics, W, b = _model[1]
    z = V @ W * 20 + b
    p = np.exp(z - z.max(1, keepdims=True))
    p /= p.sum(1, keepdims=True)
    best = p.argmax(1)
    out = {s: [topics[best[i]], round(float(p[i, best[i]]), 3)] for i, s in enumerate(shas)}
    if out != S.load_json(S.LEARNED, {}):
        S.save_json(S.LEARNED, out)
    return out
