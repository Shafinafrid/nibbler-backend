"""
Generated card artwork, for when the book itself has no usable picture.

Provider-agnostic on purpose: one function does the call, so swapping OpenAI
for Stability or Replicate is an edit in one place rather than a refactor.
Default is OpenAI `gpt-image-1` at low quality, which is the cheapest tier that
still looks like a deliberate illustration rather than a thumbnail.

── COST, because this is the part that can quietly hurt ──────────────────────
Every generated picture is a real charge. The caps are therefore enforced in
the SESSION layer (1 / 2 / 3 pictures for a 5 / 10 / 15-minute read), and every
result is uploaded to S3 and stored on the session, so a given nibble is paid
for exactly once no matter how many times it is re-read from the Nibble Bank.

Worst case per premium user is 3 sessions/day x 3 pictures = 9 images/day. At
gpt-image-1 low quality (~$0.011) that is ~$0.10/day, ~$3/month against a
$9.99 subscription — material, and worth watching in the Anthropic/OpenAI
dashboards once this is live.

Without an API key configured this module is INERT: generate() returns None,
the card simply carries no picture, and nothing else in the pipeline changes.
That is the deliberate default so the feature can ship dark and be switched on
by adding one Railway variable.
"""

import base64
import logging
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

OPENAI_IMAGE_URL = "https://api.openai.com/v1/images/generations"

# Landscape suits a card better than a square: the card is wider than it is
# tall, and a square crops badly on small screens.
SIZE = "1536x1024"
TIMEOUT = 60.0

# Prepended to every prompt so generated art reads as one set rather than a
# grab-bag of styles, and so it sits with the app's warm editorial palette.
STYLE_PREFIX = (
    "Editorial illustration for a reading app. Warm, muted palette — cream, "
    "terracotta, soft amber, deep navy. Clean, uncluttered, calm. No text, no "
    "words, no letters, no logos, no watermarks, no people's faces. "
)


def enabled() -> bool:
    """True when a key is configured. Everything else checks this first."""
    return bool(getattr(settings, "openai_api_key", "") and
                getattr(settings, "image_generation_enabled", True))


def generate(prompt: str) -> Optional[bytes]:
    """Render `prompt` and return PNG bytes, or None.

    Returns None rather than raising on EVERY failure path — no key, a refused
    prompt, a timeout, a provider outage. A missing picture must never take
    down a nibble the user is waiting for.
    """
    if not enabled():
        return None
    if not (prompt or "").strip():
        return None

    body = {
        "model": getattr(settings, "image_model", "gpt-image-1"),
        "prompt": STYLE_PREFIX + prompt.strip(),
        "size": SIZE,
        "quality": getattr(settings, "image_quality", "low"),
        "n": 1,
    }
    headers = {"Authorization": f"Bearer {settings.openai_api_key}"}

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.post(OPENAI_IMAGE_URL, json=body, headers=headers)
        if r.status_code != 200:
            # Body is logged because the useful part of an image-API failure is
            # almost always the refusal reason, not the status code.
            logger.warning("Image generation failed (%s): %s", r.status_code, r.text[:300])
            return None
        data = r.json().get("data") or []
        if not data:
            return None
        b64 = data[0].get("b64_json")
        if b64:
            return base64.b64decode(b64)
        url = data[0].get("url")
        if url:
            with httpx.Client(timeout=TIMEOUT) as client:
                img = client.get(url)
            if img.status_code == 200:
                return img.content
        return None
    except Exception as e:
        logger.warning("Image generation error: %s", e)
        return None


def generate_and_store(prompt: str, item_id: str, slot: str) -> Optional[str]:
    """Generate, upload to S3, and return the stored ref (or None)."""
    data = generate(prompt)
    if not data:
        return None
    try:
        from app.services.s3_service import S3Service
        return S3Service().upload_file(
            data, f"generated-images/{item_id}/{slot}.png", "image/png"
        )
    except Exception as e:
        logger.warning("Could not store generated image for %s: %s", item_id, e)
        return None
