"""Окно приложения «Разбор скриншотов».

Запускает локальный сервер и показывает его в родном окне (pywebview: на Windows это Edge WebView2).
  python desktop.py             обычный запуск
  python desktop.py --selftest  проверка сборки без окна: сервер поднялся и отдаёт данные, код возврата 0
"""
import json, sys, time, traceback, urllib.request

import app
import sortshots as S

TITLE = "Разбор скриншотов"

if sys.stdout is None:  # .exe без консоли: print и traceback иначе падают, пишем в лог
    S.APP_DATA.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = open(S.APP_DATA / "app.log", "a", encoding="utf-8", buffering=1)
for stream in (sys.stdout, sys.stderr):  # консоль Windows в cp1252/cp866: кириллица роняла print
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def wait_up(url, seconds=15):
    for _ in range(seconds * 10):
        try:
            urllib.request.urlopen(url, timeout=1).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def selftest():
    port = next(p for p in range(18765, 18800) if app.port_free(p))
    app.serve(port)
    base = f"http://127.0.0.1:{port}"
    assert wait_up(base + "/"), "сервер не ответил"
    html = urllib.request.urlopen(base + "/").read().decode("utf-8")
    assert "Разбор скриншотов" in html, "нет страницы"
    for f in ("alpine.min.js", "pico.min.css"):
        assert urllib.request.urlopen(f"{base}/static/{f}").status == 200, f
    with urllib.request.urlopen(base + "/api/events") as r:
        snap = json.loads(r.readline().decode("utf-8")[6:])
    assert "Разработка/MCP-серверы" in snap["topics"], "нет тем"
    import onnxruntime, similar  # поиск похожих: грузит родную библиотеку (на Windows — DLL)
    assert "CPUExecutionProvider" in onnxruntime.get_available_providers(), "onnxruntime без CPU"
    if similar.ready():
        from PIL import Image
        v = similar.embed([Image.new("RGB", (100, 200), "white")])
        assert v.shape == (1, 768), f"вектор {v.shape}"
    if similar.text_ready():  # поиск по смыслу: токенизатор и текстовая модель
        t = similar.text_vector("экран звонка")
        assert t.shape == (768,), f"вектор текста {t.shape}"
    else:
        import tokenizers  # noqa: F401  библиотека должна быть в сборке, даже если модель ещё не скачана
    print("selftest ok:", snap["phase"], "root =", snap["root"], "ollama =", snap["setup"], "похожие =", snap["similar"])


def main():
    if "--selftest" in sys.argv:
        try:
            selftest()
        except Exception:  # без консоли нельзя показывать окно с ошибкой — сборка на CI зависла бы
            traceback.print_exc()
            S.APP_DATA.mkdir(parents=True, exist_ok=True)
            (S.APP_DATA / "selftest.log").write_text(traceback.format_exc(), encoding="utf-8")
            sys.exit(1)
        return
    base = f"http://127.0.0.1:{app.PORT}"
    if app.port_free(app.PORT):  # иначе уже открыто другое окно — подключаемся к его серверу
        app.serve(app.PORT)
    wait_up(base + "/")
    try:
        import webview
    except ImportError:  # без pywebview (например, на Linux) открываем в браузере
        browser(base)
    win = webview.create_window(TITLE, base, width=1280, height=860, min_size=(900, 600))
    folder = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG

    def pick():
        r = win.create_file_dialog(folder)
        return (r[0] if isinstance(r, (list, tuple)) else r) if r else None

    app.PICK_FOLDER = pick
    try:
        # только Edge WebView2: старый движок IE не потянет интерфейс
        webview.start(gui="edgechromium" if S.WIN else None)
    except Exception:
        traceback.print_exc()
        app.PICK_FOLDER = None
        browser(base)


def browser(url):
    S.open_path(url)
    while True:  # сервер живёт, пока открыт процесс
        time.sleep(3600)


if __name__ == "__main__":
    main()
