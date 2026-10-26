#!/usr/bin/env bash
# Сборка установщика для Windows на Linux через Wine (когда GitHub Actions недоступен).
#   packaging/build_wine.sh [версия]      → $OUT/Sortshots-Setup-<версия>.exe
# Окружение Wine, скачанные установщики и результат лежат в $WORK (по умолчанию ~/.cache/sortshots-wine).
set -euo pipefail
[ -x "$HOME/.local/opt/wine/usr/bin/wine" ] && export PATH="$HOME/.local/opt/wine/usr/bin:$PATH"  # Wine из пакета Arch, распакованный без sudo

VERSION="${1:-0.1.0}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${WORK:-$HOME/.cache/sortshots-wine}"
OUT="${OUT:-$WORK/out}"
PY_VER=3.12.10
INNO_VER=6.7.3

export WINEPREFIX="$WORK/prefix" WINEARCH=win64 WINEDEBUG=-all
export WINEDLLOVERRIDES="mscoree,mshtml="  # без окон «установить Mono/Gecko»: для сборки они не нужны
mkdir -p "$WORK/dl" "$OUT"

fetch() {  # fetch <url> <файл>
  [ -s "$WORK/dl/$2" ] || curl -fL --retry 3 --max-time 600 -o "$WORK/dl/$2" "$1"
}
fetch "https://www.python.org/ftp/python/$PY_VER/python-$PY_VER-amd64.exe" python.exe
fetch "https://github.com/jrsoftware/issrc/releases/download/is-${INNO_VER//./_}/innosetup-$INNO_VER.exe" innosetup.exe

PY='C:\Python312\python.exe'
ISCC='C:\InnoSetup\ISCC.exe'
[ -d "$WINEPREFIX" ] || wineboot -i
if [ ! -f "$WINEPREFIX/drive_c/Python312/python.exe" ]; then
  echo "→ ставлю Python $PY_VER в Wine"
  wine "$WORK/dl/python.exe" /quiet InstallAllUsers=0 PrependPath=0 Include_test=0 Include_tcltk=0 \
    Include_launcher=0 Shortcuts=0 'TargetDir=C:\Python312'
fi
if [ ! -f "$WINEPREFIX/drive_c/InnoSetup/ISCC.exe" ]; then
  echo "→ ставлю Inno Setup $INNO_VER в Wine"
  wine "$WORK/dl/innosetup.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER '/DIR=C:\InnoSetup'
fi

echo "→ зависимости"
wine "$PY" -m pip install -q --disable-pip-version-check -r "$(winepath -w "$ROOT/requirements.txt")" "pyinstaller>=6.10,<7"

echo "→ PyInstaller"
wine "$PY" -m PyInstaller --noconfirm --log-level WARN \
  --workpath "$(winepath -w "$WORK/pyi-work")" --distpath "$(winepath -w "$WORK/dist")" \
  "$(winepath -w "$ROOT/packaging/sortshots.spec")"

echo "→ самопроверка собранного .exe"
wine "$(winepath -w "$WORK/dist/Sortshots/Sortshots.exe")" --selftest

echo "→ Inno Setup"
# ISCC берёт ..\dist относительно .iss, поэтому кладём сборку туда же, куда её кладёт CI
rm -rf "$ROOT/dist/Sortshots" && mkdir -p "$ROOT/dist" && cp -r "$WORK/dist/Sortshots" "$ROOT/dist/"
wine "$ISCC" /Q "/DAppVersion=$VERSION" "/O$(winepath -w "$OUT")" "$(winepath -w "$ROOT/packaging/installer.iss")"
rm -rf "$ROOT/dist/Sortshots"

ls -la "$OUT/Sortshots-Setup-$VERSION.exe"
