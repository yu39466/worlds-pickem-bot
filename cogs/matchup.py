"""Compose several team logos into one side-by-side image.

A Discord embed has two picture slots and they are different sizes - a small
thumbnail top-right and a large image below - so two logos put one in each
come out mismatched. The only way to show them at equal size is to draw them
into a single picture and attach that.

Everything here is synchronous (httpx and Pillow both are), so callers run it
through a thread.
"""

import io
import logging

import httpx
from PIL import Image

log = logging.getLogger("lina.matchup")

LOGO_PX = 128          # height of each logo; a little larger than a thumbnail
GAP_PX = 36            # breathing room between them
MAX_LOGOS = 4

_cache: dict[tuple, bytes | None] = {}


def _fetch(url, client):
    response = client.get(url, timeout=15, follow_redirects=True)
    response.raise_for_status()
    image = Image.open(io.BytesIO(response.content))
    # Fandom serves some logos as palette or greyscale PNGs; normalising to
    # RGBA means alpha compositing works the same for all of them.
    return image.convert("RGBA")


def _scaled(image):
    """Fit to LOGO_PX tall, keeping the aspect ratio."""
    ratio = LOGO_PX / image.height
    return image.resize((max(1, round(image.width * ratio)), LOGO_PX), Image.LANCZOS)


def compose(urls):
    """Return PNG bytes of the logos in a row, or None if it cannot be built.

    Transparent background, so it sits on whatever colour the reader's Discord
    theme uses rather than carrying a white box around.
    """
    urls = tuple(urls)[:MAX_LOGOS]
    if len(urls) < 2:
        return None
    if urls in _cache:
        return _cache[urls]

    try:
        with httpx.Client(headers={"User-Agent": "world-pickem-bot/0.1"}) as client:
            logos = [_scaled(_fetch(u, client)) for u in urls]
    except Exception as e:
        log.warning("could not build matchup image: %s", e)
        _cache[urls] = None
        return None

    width = sum(l.width for l in logos) + GAP_PX * (len(logos) - 1)
    canvas = Image.new("RGBA", (width, LOGO_PX), (0, 0, 0, 0))

    x = 0
    for logo in logos:
        canvas.paste(logo, (x, 0), logo)       # third arg = use its own alpha
        x += logo.width + GAP_PX

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    data = buffer.getvalue()
    _cache[urls] = data
    return data
