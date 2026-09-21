"""Παράγει τα εικονίδια της εφαρμογής από το assets/logo-source.jpg.

    python packaging/make_icons.py

Εξάγει: taxmatch/web/static/img/{logo.png, logo-192.png, favicon.ico} και packaging/taxmatch.ico (εικονίδιο exe/installer).
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "assets" / "logo-source.jpg"
IMG = ROOT / "taxmatch" / "web" / "static" / "img"


def content_bbox(img: Image.Image, tolerance: int = 14) -> tuple[int, int, int, int]:
    """Πλαίσιο του περιεχομένου: ό,τι διαφέρει αισθητά από το (σχεδόν λευκό) φόντο των γωνιών."""
    bg = Image.new("RGB", img.size, img.getpixel((4, 4)))
    diff = ImageChops.difference(img, bg).convert("L").point(lambda p: 255 if p > tolerance else 0)
    return diff.getbbox() or (0, 0, *img.size)


def rounded_tile(img: Image.Image, radius_frac: float = 0.2) -> Image.Image:
    """Λευκό στρογγυλεμένο tile: το λογότυπο (λευκό «χαρτί») δεν χάνεται σε σκούρο θέμα ούτε στο taskbar."""
    scale = 4                                        # supersampling για λείες γωνίες
    mask = Image.new("L", (img.width * scale, img.height * scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, mask.width - 1, mask.height - 1),
                                           radius=int(mask.width * radius_frac), fill=255)
    rgba = img.convert("RGBA")
    rgba.putalpha(mask.resize(img.size, Image.LANCZOS))
    return rgba


def main() -> None:
    IMG.mkdir(parents=True, exist_ok=True)
    src = Image.open(SRC).convert("RGB")
    left, top, right, bottom = content_bbox(src)
    side = max(right - left, bottom - top)
    pad = int(side * 0.04)
    cx, cy = (left + right) // 2, (top + bottom) // 2
    half = side // 2 + pad
    square = src.crop((cx - half, cy - half, cx + half, cy + half))

    logo = square.resize((512, 512), Image.LANCZOS)
    rounded_tile(logo).save(IMG / "logo.png", optimize=True)
    rounded_tile(square.resize((192, 192), Image.LANCZOS)).save(IMG / "logo-192.png", optimize=True)

    icon_base = rounded_tile(square.resize((256, 256), Image.LANCZOS))
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    icon_base.save(IMG / "favicon.ico", sizes=sizes)
    icon_base.save(ROOT / "packaging" / "taxmatch.ico", sizes=sizes)
    print("bbox", (left, top, right, bottom), "->", IMG)


if __name__ == "__main__":
    main()
