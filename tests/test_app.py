"""Проверки без нейросети и без настоящих скриншотов: картинки рисуются, ответы модели подставляются.
Запуск: python -m unittest discover tests"""
import json, os, shutil, sys, tempfile, threading, time, unittest, urllib.request
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="sortshots-test-"))
os.environ["SORTSHOTS_ROOT"] = str(ROOT)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import app  # noqa: E402
import similar as SIM  # noqa: E402
import sortshots as S  # noqa: E402

SHOTS = {  # имя файла → ответ «нейросети»
    "call1.png": dict(topic="Разное", title="Звонок на номер 900000001", keywords=["звонок"]),
    "call2.png": dict(topic="Карты и адреса", title="Звонок по номеру 900000002", keywords=["звонок"]),
    "call3.png": dict(topic="Контакты и звонки", title="Звонок по номеру 900000003", keywords=["звонок"]),
    "pack1.png": dict(topic="Мемы и картинки", title="Пакет семечек Альфа 5 кг", keywords=["семечки", "пакет"]),
    "pack2.png": dict(topic="Мемы и картинки", title="Пакет с орехами Бета", keywords=["орехи", "пакет"]),
    "pack3.png": dict(topic="Мемы и картинки", title="Пакет семечек Гамма", keywords=["семечки", "пакет"]),
    "money.png": dict(topic="Банк и платежи", title="Зачисление 10 000 TJS на карту", keywords=["перевод"]),
    "wifi.png": dict(topic="Настройки телефона", title="Настройки Wi-Fi", keywords=["wi-fi"],
                     description="Экран с паролем от сети Wi-Fi"),
    "card.png": dict(topic="Переписки", title="Контакт Алишер Душанбе в Telegram", keywords=["контакт"],
                     seen="Алишер +992 900 11 22 33"),
}
ASKED = []


def answer(title, topic, keywords=(), description="", seen=""):
    return {"app": "Тест", "seen": seen or title, "topic": topic, "title": title, "description": description or title,
            "keywords": list(keywords), "entities": [], "phones": [], "confidence": 0.95}


class Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


REAL_URLOPEN = urllib.request.urlopen


def fake_urlopen(req, timeout=None):
    url = req if isinstance(req, str) else req.full_url
    if url.startswith("http://127.0.0.1:1880"):
        return REAL_URLOPEN(req, timeout=timeout)
    if url.endswith("/api/tags"):
        return Resp(json.dumps({"models": [{"name": S.FAST}, {"name": S.STRONG}]}).encode())
    body = json.loads(req.data)
    ASKED.append(body)
    out = answer("Переразобрано", "Покупки и заказы")
    return Resp(json.dumps({"message": {"content": json.dumps(out)}}).encode())


def setUpModule():
    src = S.SRC / "DCIM"
    src.mkdir(parents=True)
    S.DATA.mkdir(parents=True)
    with open(S.RESULTS, "w", encoding="utf-8") as f:
        for i, (name, a) in enumerate(SHOTS.items()):
            # у каждой картинки свой случайный узор: иначе поиск похожих считает их одним экраном
            rng = np.random.default_rng(i)
            im = Image.fromarray(rng.integers(0, 255, (24, 12, 3), dtype=np.uint8)).resize((360, 720), Image.NEAREST)
            ImageDraw.Draw(im).text((20, 40), a["title"], fill="white")
            im.save(src / name)
            sha = S.sha1(src / name)
            f.write(json.dumps({"sha": sha, "src": f"DCIM/{name}", "fast": answer(**a)}, ensure_ascii=False) + "\n")
    urllib.request.urlopen = fake_urlopen
    # без модели поиска похожих, как на чистой машине и в CI: результат не зависит от того, скачана ли она
    SIM.ready = SIM.text_ready = lambda: False
    S.ensure_ollama = lambda: None
    app.Worker.pull = lambda self, m: self.models.__setitem__(m, {"status": "готова"})
    app.serve(18810)
    S.place(verbose=False)


def tearDownModule():
    urllib.request.urlopen = REAL_URLOPEN
    shutil.rmtree(ROOT, ignore_errors=True)


def sha_of(name):
    return S.sha1(S.SRC / "DCIM" / name)


def post(path, body=None):
    req = urllib.request.Request("http://127.0.0.1:18810" + path, json.dumps(body or {}).encode(), method="POST")
    try:
        with REAL_URLOPEN(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"code": e.code, **json.loads(e.read())}


def get(path):
    with REAL_URLOPEN("http://127.0.0.1:18810" + path) as r:
        return json.loads(r.read())


def topic_of(name):
    return S.final(S.load_results()[sha_of(name)], S.topics())


class Placement(unittest.TestCase):
    def test_paths_are_relative(self):
        placed = S.load_placed()
        self.assertEqual(len(placed), len(SHOTS))
        for rel in json.loads(S.PLACED.read_text(encoding="utf-8")).values():
            self.assertFalse(Path(rel).is_absolute(), rel)
            self.assertTrue((S.ROOT / rel).exists(), rel)

    def test_old_absolute_paths_still_read(self):
        old = {"x": str(S.ROOT / "Разное" / "a.jpg"), "y": "/somewhere/else/b.jpg"}
        saved = S.PLACED.read_text(encoding="utf-8")
        try:
            S.save_json(S.PLACED, old)
            self.assertEqual(S.load_placed(), {"x": "Разное/a.jpg"})
        finally:
            S.PLACED.write_text(saved, encoding="utf-8")


class Secret(unittest.TestCase):
    def test_password_goes_to_secret(self):
        app.W.add_new_topics()
        f = topic_of("wifi.png")
        self.assertEqual(f["topic"], S.SECRET)
        self.assertIn("парол", f["why"])

    def test_receipt_with_masked_card_is_not_secret(self):
        self.assertIsNone(S.CARD_NUMBER.search("Перевод с карты **** 1234 на 500 TJS"))
        self.assertIsNotNone(S.CARD_NUMBER.search("Карта 4000 1234 5678 9010"))


class Search(unittest.TestCase):
    def test_words(self):
        r = get("/api/search?q=" + urllib.parse.quote("пакет семечек"))
        titles = [i["title"] for i in r["items"] if i["match"] == "по словам"]
        self.assertEqual(set(titles), {"Пакет семечек Альфа 5 кг", "Пакет семечек Гамма"})

    def test_number_with_spaces(self):
        r = get("/api/search?q=" + urllib.parse.quote("10000 tjs"))
        self.assertEqual([i["title"] for i in r["items"]][:1], ["Зачисление 10 000 TJS на карту"])

    def test_short_words_ignored(self):
        self.assertEqual(app.search_terms("мем с котом"), ["мем", "кот"])


class Contacts(unittest.TestCase):
    def test_vcf(self):
        r = post("/api/contacts")
        text = (S.ROOT / r["file"]).read_text(encoding="utf-8")
        self.assertIn("TEL;TYPE=CELL:+992900112233", text)
        self.assertIn("FN:Алишер Душанбе", text)
        self.assertIn("+992900000001", text)
        self.assertEqual(S.phone_key("123412341234123"), None)


class HintsAndUndo(unittest.TestCase):
    def test_fix_one_then_undo(self):
        sha = sha_of("money.png")
        before = S.load_placed()[sha]
        post("/api/set", {"sha": sha, "topic": "Документы и справки"})
        self.assertTrue(S.load_placed()[sha].startswith("Документы и справки/"))
        self.assertEqual(post("/api/undo")["undone"], "перенос в «Документы и справки»")
        self.assertEqual(S.load_placed()[sha], before)

    def test_word_rule_moves_existing_and_undo(self):
        r = post("/api/hints", {"mode": "all", "sha": sha_of("pack1.png"), "topic": "Покупки и заказы",
                                "words": "семечк, орех"})
        self.assertTrue(r["ok"])
        for n in ("pack1.png", "pack2.png", "pack3.png"):
            self.assertEqual(topic_of(n)["topic"], "Покупки и заказы", n)
        self.assertEqual(topic_of("pack2.png")["why"], "по словам: семечк, орех")
        post("/api/undo")
        self.assertEqual(topic_of("pack2.png")["topic"], "Мемы и картинки")

    def test_empty_rule_rejected(self):
        self.assertEqual(post("/api/hints", {"mode": "all", "sha": sha_of("pack1.png")})["code"], 400)

    def test_suggestion_after_two_moves(self):
        post("/api/set", {"sha": sha_of("pack1.png"), "topic": "Покупки и заказы"})
        post("/api/set", {"sha": sha_of("pack3.png"), "topic": "Покупки и заказы"})
        sugg = get("/api/suggestions")
        self.assertTrue(sugg, "нет предложения")
        g = sugg[0]
        self.assertEqual(g["topic"], "Покупки и заказы")
        post("/api/suggestions/accept", g)
        self.assertEqual(get("/api/suggestions"), [])
        for _ in range(3):
            post("/api/undo")


class Redo(unittest.TestCase):
    def test_comment_goes_to_model_first(self):
        sha = sha_of("call1.png")
        r = post("/api/hints", {"mode": "redo", "sha": sha, "text": "Это экран звонка"})
        self.assertTrue(r["ok"])
        self.assertEqual(S.load_json(S.QUEUE, [])[0], sha)
        ASKED.clear()
        self.assertTrue(post("/api/start")["ok"])
        for _ in range(100):
            if not app.W.thread.is_alive():
                break
            time.sleep(0.1)
        self.assertIn("Это экран звонка", ASKED[0]["messages"][-1]["content"])
        self.assertEqual(S.load_json(S.QUEUE, []), [])
        post("/api/undo")


class SameScreens(unittest.TestCase):
    def test_group_decides_together(self):
        base = np.ones(768, np.float32) / np.sqrt(768)
        vecs = {}
        for i, n in enumerate(["call1.png", "call2.png", "call3.png", "money.png"]):
            v = base.copy()
            v[i] += 0.01 if n != "money.png" else 5
            vecs["shot:" + sha_of(n)] = v / np.linalg.norm(v)
        old = SIM._vecs
        SIM._vecs = vecs
        try:
            topics = {sha_of("call1.png"): "Разное", sha_of("call2.png"): "Контакты и звонки",
                      sha_of("call3.png"): "Контакты и звонки", sha_of("money.png"): "Банк и платежи"}
            g = SIM.majority(topics)
            self.assertEqual(g, {sha_of("call1.png"): ("Контакты и звонки", 3)})
        finally:
            SIM._vecs = old


if __name__ == "__main__":
    unittest.main()
