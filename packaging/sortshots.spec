# PyInstaller: собирает папку dist/Sortshots с Sortshots.exe (без консоли).
# Запуск из корня проекта: pyinstaller --noconfirm packaging/sortshots.spec
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis(
    [os.path.join(ROOT, "desktop.py")],
    pathex=[ROOT],
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
