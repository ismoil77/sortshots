"""Рисует иконку приложения: packaging/icon.ico (для Windows) и static/icon.png (для окна)."""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
S = 1024

im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
d.rounded_rectangle((40, 40, S - 40, S - 40), radius=220, fill=(28, 25, 23))
# стопка скриншотов веером
for dx, rot, col in [(-150, -14, (120, 113, 108)), (0, 0, (214, 211, 209)), (150, 14, (234, 88, 12))]:
    card = Image.new("RGBA", (300, 520), (0, 0, 0, 0))
    cd = ImageDraw.Draw(card)
    cd.rounded_rectangle((0, 0, 299, 519), radius=46, fill=col)
    for y, w in ((90, 220), (150, 180), (210, 200)):
        cd.rounded_rectangle((40, y, 40 + w, y + 26), radius=13, fill=(28, 25, 23, 90))
    card = card.rotate(-rot, expand=True, resample=Image.BICUBIC)
    im.alpha_composite(card, (S // 2 - card.width // 2 + dx, 250))
# искра — нейросеть
cx, cy, r = 760, 250, 120
d.polygon([(cx, cy - r), (cx + 28, cy - 28), (cx + r, cy), (cx + 28, cy + 28),
           (cx, cy + r), (cx - 28, cy + 28), (cx - r, cy), (cx - 28, cy - 28)], fill=(253, 186, 116))

im.save(ROOT / "packaging" / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
im.resize((256, 256), Image.LANCZOS).save(ROOT / "static" / "icon.png")
