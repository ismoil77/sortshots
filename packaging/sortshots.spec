# PyInstaller: собирает папку dist/Sortshots с Sortshots.exe (без консоли).
# Запуск из корня проекта: pyinstaller --noconfirm packaging/sortshots.spec
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
# onnxruntime (поиск похожих) требует MSVCP140*.dll, которых нет на чистой Windows без Visual C++ Redistributable.
# Пакет msvc-runtime кладёт их в папку Python, отсюда — рядом с onnxruntime.dll.
RUNTIME = [(os.path.join(sys.base_prefix, n), "onnxruntime/capi")
           for n in ("msvcp140.dll", "msvcp140_1.dll", "vcruntime140.dll", "vcruntime140_1.dll")
           if os.path.exists(os.path.join(sys.base_prefix, n))]

a = Analysis(
    [os.path.join(ROOT, "desktop.py")],
    pathex=[ROOT],
    binaries=RUNTIME,
    datas=[(os.path.join(ROOT, "static"), "static")],
    excludes=["tkinter", "unittest", "pydoc_data"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Sortshots",
    console=False,
    icon=os.path.join(ROOT, "packaging", "icon.ico"),
)
coll = COLLECT(exe, a.binaries, a.datas, name="Sortshots")
