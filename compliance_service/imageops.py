"""Image operations for region-level vision calls.

Cropping a table region before asking the vision model makes transcription an
order of magnitude cheaper than sending the whole page, and keeps pixel
provenance tied to one region.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image

MIN_SIDE = 48
DEFAULT_MAX_SIDE = 1200


def crop_region(image_bytes: bytes, bbox, *, max_side: int = DEFAULT_MAX_SIDE,
                upscale: float = 1.0, padding: int = 6) -> bytes:
    """Crop one region and return PNG bytes; raises ValueError on bad input."""
    if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
        raise ValueError("image bytes are required")
    if not bbox or len(bbox) != 4:
        raise ValueError("bbox must be [x0, y0, x1, y1]")
    with Image.open(BytesIO(bytes(image_bytes))) as handle:
        image = handle.convert("RGB")
        x0, y0, x1, y1 = [float(value) for value in bbox]
        left, top = max(0, int(x0) - padding), max(0, int(y0) - padding)
        right, bottom = min(image.width, int(x1) + padding), min(image.height, int(y1) + padding)
        if right - left < MIN_SIDE or bottom - top < MIN_SIDE:
            raise ValueError("region is too small")
        crop = image.crop((left, top, right, bottom))
        if upscale > 1:
            crop = crop.resize((int(crop.width * upscale), int(crop.height * upscale)),
                               Image.LANCZOS)
        if max(crop.size) > max_side:
            ratio = max_side / max(crop.size)
            crop = crop.resize((max(1, int(crop.width * ratio)), max(1, int(crop.height * ratio))),
                               Image.LANCZOS)
        buffer = BytesIO()
        crop.save(buffer, "PNG")
        return buffer.getvalue()


def image_size(image_bytes: bytes) -> tuple:
    with Image.open(BytesIO(bytes(image_bytes))) as handle:
        return handle.size
