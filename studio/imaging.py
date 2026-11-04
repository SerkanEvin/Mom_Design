"""Free, local image work (no fal spend): validation, cut-out normalisation, contrast."""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageOps

MAX_UPLOAD_PX = 2048


class ImageError(Exception):
    pass


def load_upload(data: bytes) -> Image.Image:
    try:
        im = Image.open(io.BytesIO(data))
        im.verify()
        im = Image.open(io.BytesIO(data))
        im = ImageOps.exif_transpose(im)
    except Exception as e:
        raise ImageError("Görsel açılamadı (PNG, JPG veya WEBP olmalı).") from e
    if im.width < 128 or im.height < 128:
        raise ImageError("Görsel çok küçük (en az 128×128 piksel).")
    im = im.convert("RGBA")
    im.thumbnail((MAX_UPLOAD_PX, MAX_UPLOAD_PX))
    return im


def png_bytes(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def save_png(im: Image.Image, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "PNG", optimize=True)
    return path


def normalize_cutout(im: Image.Image, size: int = 768, margin: float = 0.08) -> Image.Image:
    """Crop to the object's alpha box, pad to a centred square, even out contrast."""
    im = im.convert("RGBA")
    alpha = im.getchannel("A")
    box = alpha.point(lambda a: 255 if a > 16 else 0).getbbox()
    if not box:
        raise ImageError("Görselde nesne bulunamadı (arka plan silinince boş kaldı).")
    obj = im.crop(box)
    rgb = ImageOps.autocontrast(obj.convert("RGB"), cutoff=1)
    obj = Image.merge("RGBA", (*rgb.split(), obj.getchannel("A")))
    side = int(max(obj.width, obj.height) * (1 + 2 * margin))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(obj, ((side - obj.width) // 2, (side - obj.height) // 2), obj)
    return canvas.resize((size, size), Image.LANCZOS)


def square_rgb(im: Image.Image, size: int, bg=(235, 235, 235)) -> Image.Image:
    """Reference image for view synthesis: object on a plain light background."""
    im = im.convert("RGBA")
    side = max(im.width, im.height)
    canvas = Image.new("RGBA", (side, side), (*bg, 255))
    canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2), im)
    return canvas.convert("RGB").resize((size, size), Image.LANCZOS)


def alpha_fraction(im: Image.Image) -> float:
    a = im.convert("RGBA").getchannel("A")
    hist = a.histogram()
    return sum(hist[17:]) / max(1, sum(hist))
