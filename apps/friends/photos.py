"""Profile photos and shared step note photos.

Upload pipeline (all before anything is stored):
1. Decode with Pillow — anything that is not a real still image is refused.
2. Fix orientation and re-encode as JPEG. Profiles use a 512 px square; note
   photos preserve their aspect ratio with a long edge of at most 1600 px.
   Re-encoding drops every metadata block, so a phone's GPS/EXIF never survives.
3. The image safety check (OpenAI's moderation model, free). Flagged → refused with
   a kind message. If the check can't run, the upload fails closed: better "try
   again later" than an unchecked photo in front of neighbors.

The photo is never given to the assistant; it is only served to people allowed by
the access helpers (profile visibility or active project membership).
"""

from __future__ import annotations

import base64
import io
import logging
import os

from django.conf import settings
from rest_framework.exceptions import APIException, ValidationError

from . import access

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MIN_SIDE = 96
OUTPUT_SIDE = 512
MODERATION_MODEL = "omni-moderation-latest"


class PhotoCheckUnavailable(APIException):
    status_code = 503
    default_detail = "We couldn’t check your photo right now. Please try again in a little while."
    default_code = "photo_check_unavailable"


class PhotoRefused(APIException):
    status_code = 422
    default_detail = "That photo can’t be used. Please choose a different one."
    default_code = "photo_refused"


def normalize(raw: bytes, *, note=False) -> bytes:
    """Clean JPEG without metadata: a 512 px profile square, or a note up to 1600 px."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not raw:
        raise ValidationError("Choose a photo to upload.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValidationError("That photo is too large. Please choose one under 10 MB.")
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            probe.verify()
        image = Image.open(io.BytesIO(raw))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValidationError("That file isn’t a photo we can use.") from exc
    if getattr(image, "is_animated", False):
        raise ValidationError("Please choose a still photo.")
    image = ImageOps.exif_transpose(image)
    if min(image.size) < MIN_SIDE:
        raise ValidationError("That photo is too small. Please choose a bigger one.")
    image = image.convert("RGB")
    if note:
        image.thumbnail((1600, 1600), resample=Image.Resampling.LANCZOS)
    else:
        image = ImageOps.fit(image, (OUTPUT_SIDE, OUTPUT_SIDE), method=Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85, optimize=True)
    return out.getvalue()


def _api_key() -> str:
    return getattr(settings, "OPENAI_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")


def check_safe(jpeg: bytes) -> None:
    """Raise PhotoRefused if the safety check flags the image; fail closed if the
    check cannot run."""
    key = _api_key()
    if not key:
        logger.error("profile photo: OPENAI_API_KEY missing — refusing upload (fail closed)")
        raise PhotoCheckUnavailable()
    try:
        from openai import OpenAI

        client = OpenAI(api_key=key, timeout=20, max_retries=1)
        data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
        result = client.moderations.create(
            model=MODERATION_MODEL, input=[{"type": "image_url", "image_url": {"url": data_url}}]
        )
        if not result.results or type(result.results[0].flagged) is not bool:
            raise ValueError("Missing moderation verdict")
        flagged = result.results[0].flagged
    except Exception:
        logger.warning("profile photo: safety check failed to run — refusing upload", exc_info=True)
        raise PhotoCheckUnavailable() from None
    if flagged:
        logger.info("profile photo: refused by the safety check")
        raise PhotoRefused()


def set_photo(profile, raw: bytes) -> str:
    jpeg = normalize(raw)
    check_safe(jpeg)
    access.save_photo(profile, jpeg)
    return access.photo_url(profile)


def remove_photo(profile) -> None:
    access.delete_photo(profile)
