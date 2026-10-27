#!/usr/bin/env bash
# Установка «Разбора скриншотов» на Linux из этой папки: окружение Python, команда sortshots-app и ярлык в меню.
#   packaging/install_linux.sh            установить или обновить
#   packaging/install_linux.sh --remove   убрать ярлык и команду (скриншоты и данные не трогает)
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$HOME/.local/bin/sortshots-app"
DESKTOP="$HOME/.local/share/applications/sortshots.desktop"
ICON="$HOME/.local/share/icons/hicolor/256x256/apps/sortshots.png"

if [ "${1:-}" = "--remove" ]; then
  rm -f "$BIN" "$DESKTOP" "$ICON"
  echo "Убрано. Скриншоты и данные остались на месте."
  exit 0
fi

PY=$(command -v python3) || { echo "Нужен python3 (3.10 или новее)"; exit 1; }
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "Нужен Python 3.10 или новее"; exit 1; }

echo "→ окружение Python и зависимости"
[ -x "$DIR/.venv/bin/python" ] || "$PY" -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --disable-pip-version-check -r "$DIR/requirements.txt"

echo "→ команда sortshots-app"
mkdir -p "$(dirname "$BIN")"
cat >"$BIN" <<EOF
#!/usr/bin/env bash
# «Разбор скриншотов»: поднимает локальный сервер (если ещё не запущен) и открывает окно приложения.
set -e
DIR="$DIR"
URL="http://127.0.0.1:8765"
LOG="\${XDG_STATE_HOME:-\$HOME/.local/state}/sortshots/app.log"
export PATH="\$HOME/.local/share/mise/shims:\$HOME/.local/bin:\$PATH"  # ollama может стоять через mise

cd "\$DIR"
if ! .venv/bin/python -c 'import PIL, numpy, onnxruntime' 2>/dev/null; then
  .venv/bin/pip install -q -r requirements.txt
fi
if ! curl -s -o /dev/null "\$URL"; then
  mkdir -p "\$(dirname "\$LOG")"
  nohup .venv/bin/python app.py >>"\$LOG" 2>&1 &
  for _ in \$(seq 50); do curl -s -o /dev/null "\$URL" && break; sleep 0.2; done
fi
if command -v omarchy-launch-webapp >/dev/null; then
  exec omarchy-launch-webapp "\$URL"
fi
for b in chromium google-chrome-stable google-chrome brave microsoft-edge-stable; do  # окно без вкладок
  command -v "\$b" >/dev/null && exec "\$b" --app="\$URL"
done
exec xdg-open "\$URL"
EOF
chmod +x "$BIN"

echo "→ ярлык в меню"
mkdir -p "$(dirname "$DESKTOP")" "$(dirname "$ICON")"
cp "$DIR/static/icon.png" "$ICON"
cat >"$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Разбор скриншотов
Comment=Локальная нейросеть раскладывает скриншоты с телефона по папкам
Exec=$BIN
Icon=sortshots
Terminal=false
Categories=Utility;Graphics;
Keywords=screenshots;скриншоты;sort;ollama;
EOF
command -v update-desktop-database >/dev/null && update-desktop-database -q "$(dirname "$DESKTOP")" || true

echo "→ самопроверка"
"$DIR/.venv/bin/python" "$DIR/desktop.py" --selftest

if ! command -v ollama >/dev/null && [ ! -x "$HOME/.local/share/mise/shims/ollama" ]; then
  echo
  echo "Осталось поставить Ollama (нейросеть): https://ollama.com/download/linux"
  echo "  curl -fsSL https://ollama.com/install.sh | sh"
fi
echo
echo "Готово: «Разбор скриншотов» в меню приложений или команда sortshots-app."
