"""Δημιουργεί τις εικόνες του Inno Setup installer:
1. wizard-small.bmp (55x55 px): Εμφανίζεται πάνω δεξιά σε όλες τις εσωτερικές σελίδες του οδηγού (Select Dir, Tasks, Ready, Installing).
2. wizard-banner.bmp (164x314 px): Εμφανίζεται αριστερά στην πρώτη και στην τελευταία σελίδα του οδηγού.
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
LOGO_PATH = ROOT / "taxmatch" / "web" / "static" / "img" / "logo.png"
PKG_DIR = ROOT / "packaging"

def make_small_image():
    # 55x55 px με λευκό φόντο (Inno Setup modern wizard small image)
    bg = Image.new("RGB", (55, 55), (255, 255, 255))
    if LOGO_PATH.exists():
        logo = Image.open(LOGO_PATH).convert("RGBA")
        logo.thumbnail((47, 47), Image.LANCZOS)
        offset = ((55 - logo.width) // 2, (55 - logo.height) // 2)
        bg.paste(logo, offset, mask=logo.split()[3])
    out_path = PKG_DIR / "wizard-small.bmp"
    bg.save(out_path, format="BMP")
    print("Saved", out_path)

def make_banner_image():
    # 164x314 px με κομψό navy gradient/φόντο (Inno Setup modern wizard side banner)
    bg = Image.new("RGB", (164, 314), (15, 23, 42)) # #0f172a
    draw = ImageDraw.Draw(bg)

    # Απαλό gradient προς #1e293b
    for y in range(314):
        ratio = y / 314.0
        r = int(15 + (30 - 15) * ratio)
        g = int(23 + (41 - 23) * ratio)
        b = int(42 + (59 - 42) * ratio)
        draw.line([(0, y), (164, y)], fill=(r, g, b))

    # Προσθήκη λογοτύπου
    if LOGO_PATH.exists():
        logo = Image.open(LOGO_PATH).convert("RGBA")
        logo.thumbnail((96, 96), Image.LANCZOS)
        offset = ((164 - logo.width) // 2, 60)
        bg.paste(logo, offset, mask=logo.split()[3])

    # Κείμενο TaxMatch & ScanMyData
    # Προσπαθούμε να φορτώσουμε system Segoe UI / Arial
    try:
        font_title = ImageFont.truetype("segoeuib.ttf", 16)
        font_sub = ImageFont.truetype("segoeui.ttf", 11)
    except IOError:
        font_title = ImageFont.load_default()
        font_sub = ImageFont.load_default()

    draw.text((82, 175), "TaxMatch", fill=(255, 255, 255), font=font_title, anchor="mm")
    draw.text((82, 198), "by ScanMyData", fill=(148, 163, 184), font=font_sub, anchor="mm")

    out_path = PKG_DIR / "wizard-banner.bmp"
    bg.save(out_path, format="BMP")
    print("Saved", out_path)

if __name__ == "__main__":
    make_small_image()
    make_banner_image()
