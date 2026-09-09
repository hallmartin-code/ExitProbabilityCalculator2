"""Generate the favicon and app-icon set from the TEN Capital Network mark.

Run with:  python tools/generate_icons.py

Outputs land in ``static/``, which Streamlit serves publicly. The master artwork
is committed alongside them so the set can always be regenerated, and so anyone
can see what the icons were derived from.

Two deliberate choices:

* **Web icons keep their transparency.** The mark is a coloured ring on nothing,
  so it reads correctly against both a light and a dark browser tab. Baking in a
  background would make it wrong in one of the two.
* **Apple and maskable icons are opaque navy.** iOS composites a touch icon onto
  its own background and applies a corner radius; a transparent one shows through
  as black. Maskable icons are cropped to a circle by the launcher, so the mark is
  inset to the 80% safe zone to survive that crop.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
#: Streamlit serves `Path(entrypoint).parent / "static"`, so the public asset
#: directory lives beside app.py rather than at the project root.
STATIC_DIR = PROJECT_ROOT / "venture_exit_analyzer" / "static"
MASTER = STATIC_DIR / "logo-source.png"

#: Brand ground, matching PALETTE["navy_950"] in venture_exit_analyzer/ui/theme.py.
NAVY = (11, 21, 38, 255)

#: Transparent PNGs for browsers and the web manifest.
WEB_SIZES: tuple[int, ...] = (16, 32, 48, 64, 96, 192, 512)

#: Sizes embedded in the multi-resolution .ico.
ICO_SIZES: tuple[int, ...] = (16, 32, 48)

#: Share of the canvas the mark occupies inside a maskable icon. Launchers may
#: crop to a circle inscribed in the square, so anything outside is at risk.
MASKABLE_SAFE_ZONE = 0.78

#: Apple touch icons look better with a little breathing room inside the tile.
APPLE_INSET = 0.82


def _load_master() -> Image.Image:
    if not MASTER.exists():
        raise SystemExit(
            f"Master artwork not found at {MASTER}.\n"
            f"Place the square TEN Capital Network mark there and re-run."
        )
    image = Image.open(MASTER).convert("RGBA")
    if image.width != image.height:
        raise SystemExit(
            f"Master artwork must be square; {MASTER.name} is {image.width}x{image.height}."
        )
    return _trim_transparent(image)


def _trim_transparent(image: Image.Image) -> Image.Image:
    """Crop surrounding transparency, then re-pad to a square.

    Source artwork often carries uneven padding. Trimming first means every
    generated size has the mark at a predictable scale rather than inheriting
    whatever margin the original happened to have.
    """
    bbox = image.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    if bbox is None:
        return image
    cropped = image.crop(bbox)
    side = max(cropped.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.alpha_composite(
        cropped, ((side - cropped.width) // 2, (side - cropped.height) // 2)
    )
    return square


def _resized(master: Image.Image, size: int) -> Image.Image:
    return master.resize((size, size), Image.LANCZOS)


def _on_navy(master: Image.Image, size: int, inset: float) -> Image.Image:
    """Composite the mark onto an opaque navy tile at the given inset."""
    tile = Image.new("RGBA", (size, size), NAVY)
    inner = max(1, int(round(size * inset)))
    mark = _resized(master, inner)
    offset = (size - inner) // 2
    tile.alpha_composite(mark, (offset, offset))
    return tile


def _write(image: Image.Image, name: str) -> Path:
    path = STATIC_DIR / name
    image.save(path, optimize=True)
    return path


def _manifest() -> dict:
    return {
        "name": "TEN Capital Network - Exit Scenario Analyzer",
        "short_name": "Exit Analyzer",
        "description": (
            "Directional venture exit-scenario analysis from a pitch deck."
        ),
        "start_url": ".",
        "display": "standalone",
        "background_color": "#0B1526",
        "theme_color": "#0B1526",
        "icons": [
            {"src": "app/static/favicon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "app/static/favicon-512.png", "sizes": "512x512", "type": "image/png"},
            {
                "src": "app/static/icon-maskable-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    }


def generate() -> list[Path]:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    master = _load_master()
    written: list[Path] = []

    for size in WEB_SIZES:
        written.append(_write(_resized(master, size), f"favicon-{size}.png"))

    # The default favicon browsers request when no link tag names one.
    written.append(_write(_resized(master, 32), "favicon.png"))

    # Multi-resolution .ico for legacy requests to /favicon.ico.
    ico_path = STATIC_DIR / "favicon.ico"
    _resized(master, max(ICO_SIZES)).save(
        ico_path, format="ICO", sizes=[(s, s) for s in ICO_SIZES]
    )
    written.append(ico_path)

    written.append(_write(_on_navy(master, 180, APPLE_INSET), "apple-touch-icon.png"))
    written.append(
        _write(_on_navy(master, 512, MASKABLE_SAFE_ZONE), "icon-maskable-512.png")
    )

    manifest_path = STATIC_DIR / "site.webmanifest"
    manifest_path.write_text(json.dumps(_manifest(), indent=2), encoding="utf-8")
    written.append(manifest_path)

    return written


def main() -> None:
    for path in generate():
        size = path.stat().st_size
        print(f"  {path.relative_to(PROJECT_ROOT).as_posix():<38} {size:>7,} bytes")


if __name__ == "__main__":
    main()
