"""The interface design system.

The theme lives in two places -- Python tokens and Streamlit configuration --
because Streamlit styles its own widgets from the latter and cannot read the
former. Two sources of truth drift, so the first test here holds them together.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from venture_exit_analyzer.ui import theme

CONFIG_PATH = Path(__file__).resolve().parent.parent / ".streamlit" / "config.toml"


@pytest.fixture(scope="module")
def config_text() -> str:
    return CONFIG_PATH.read_text(encoding="utf-8")


class TestPaletteAndConfigAgree:
    def test_config_matches_the_palette(self, config_text):
        """A colour changed in one place and not the other is a silent bug."""
        assert theme.check_config_matches_palette(config_text) == []

    def test_drift_is_actually_detected(self):
        """The guard must fail when the two disagree, or it guarantees nothing."""
        issues = theme.check_config_matches_palette('backgroundColor = "#FFFFFF"')
        assert issues
        assert any("backgroundColor" in issue for issue in issues)

    def test_every_palette_entry_is_a_hex_colour(self):
        for name, value in theme.PALETTE.items():
            assert re.fullmatch(r"#[0-9A-Fa-f]{6}", value), f"{name} is not a hex colour"

    def test_config_declares_the_three_brand_fonts(self, config_text):
        assert 'font = "Inter, sans-serif"' in config_text
        assert 'headingFont = "Sora, sans-serif"' in config_text
        assert 'codeFont = "JetBrains Mono, monospace"' in config_text


class TestBrandMark:
    def test_uses_the_real_artwork_when_present(self):
        """The tab icon and the header must show the same mark."""
        mark = theme.brand_mark()
        assert mark.startswith('<img src="data:image/png;base64,')
        assert 'alt="TEN Capital Network"' in mark

    def test_honours_the_requested_size(self):
        mark = theme.brand_mark(size=64)
        assert 'width="64"' in mark and 'height="64"' in mark

    def test_falls_back_to_the_drawn_mark_without_artwork(self, monkeypatch, tmp_path):
        """A checkout with no generated icons must still render."""
        monkeypatch.setattr(theme, "BRAND_ICON", tmp_path / "absent.png")
        assert theme.brand_mark().lstrip().startswith("<svg")


class TestDrawnFallback:
    def test_coral_arc_is_not_rotated_onto_the_teal_side(self):
        """The source design rotated the coral arc away from its own circle.

        rotate(180 50 79) maps the arc from the bottom-left to the bottom-right,
        where it overlaps the teal arc and detaches from the coral circle.
        """
        assert "rotate(" not in theme._drawn_mark()

    def test_uses_all_three_accent_colours(self):
        mark = theme._drawn_mark()
        for key in ("coral", "amber", "teal"):
            assert theme.PALETTE[key] in mark

    def test_each_arc_sits_beneath_its_own_circle(self):
        """Arc origin and circle centre must share a side, or the mark reads broken."""
        mark = theme._drawn_mark()
        arc_x = [float(x) for x in re.findall(r'd="M(\d+)', mark)]
        circle_x = [float(x) for x in re.findall(r'cx="(\d+)"', mark)]
        assert sorted(arc_x) == pytest.approx(sorted(circle_x), abs=1.0)

    def test_is_accessible_and_scalable(self):
        mark = theme._drawn_mark(size=64)
        assert 'role="img"' in mark
        assert 'aria-label="TEN Capital Network"' in mark
        assert 'width="64"' in mark
        assert 'viewBox="0 0 100 100"' in mark


class TestIconAssets:
    """The generated favicon set in static/."""

    @pytest.mark.parametrize("size", [16, 32, 48, 64, 96, 192, 512])
    def test_web_icon_exists_at_the_declared_size(self, size):
        from PIL import Image

        path = theme.STATIC_DIR / f"favicon-{size}.png"
        assert path.is_file(), f"{path.name} has not been generated"
        with Image.open(path) as image:
            assert image.size == (size, size)

    def test_web_icons_keep_transparency(self):
        """The mark must read on both a light and a dark browser tab."""
        from PIL import Image

        with Image.open(theme.STATIC_DIR / "favicon-32.png") as image:
            assert image.convert("RGBA").getchannel("A").getextrema()[0] == 0

    def test_ico_carries_multiple_resolutions(self):
        from PIL import Image

        with Image.open(theme.STATIC_DIR / "favicon.ico") as image:
            assert len(getattr(image, "ico", image).sizes()) >= 3

    def test_apple_touch_icon_is_opaque(self):
        """iOS shows transparency as black behind its rounded tile."""
        from PIL import Image

        with Image.open(theme.STATIC_DIR / "apple-touch-icon.png") as image:
            rgba = image.convert("RGBA")
            assert rgba.size == (180, 180)
            assert rgba.getchannel("A").getextrema() == (255, 255)

    def test_maskable_icon_survives_a_circular_crop(self):
        """Launchers crop maskable icons to the inscribed circle."""
        import numpy as np
        from PIL import Image

        with Image.open(theme.STATIC_DIR / "icon-maskable-512.png") as image:
            pixels = np.array(image.convert("RGBA"))
        height, width = pixels.shape[:2]
        yy, xx = np.mgrid[0:height, 0:width]
        outside = ((xx - width / 2) ** 2 + (yy - height / 2) ** 2) > (width / 2) ** 2
        navy = np.array([11, 21, 38])
        drift = np.abs(pixels[..., :3][outside].astype(int) - navy).sum(axis=1)
        assert (drift > 40).sum() == 0, "mark extends outside the maskable safe zone"

    def test_manifest_is_valid_and_points_at_real_files(self):
        import json

        manifest = json.loads(
            (theme.STATIC_DIR / "site.webmanifest").read_text(encoding="utf-8")
        )
        assert manifest["background_color"] == theme.PALETTE["navy_950"]
        assert manifest["icons"], "manifest declares no icons"
        for icon in manifest["icons"]:
            name = icon["src"].rsplit("/", 1)[-1]
            assert (theme.STATIC_DIR / name).is_file(), f"manifest names missing {name}"

    def test_no_python_is_served_from_the_public_directory(self):
        """static/ is served publicly; source there would be downloadable."""
        assert not list(theme.STATIC_DIR.glob("*.py"))


class TestHeadLinks:
    """Relations Streamlit has no API for, injected into <head> by a component."""

    def test_every_declared_link_points_at_a_real_file(self):
        for rel, href, _ in theme._HEAD_LINKS:
            name = href.rsplit("/", 1)[-1]
            assert (theme.STATIC_DIR / name).is_file(), f"{rel} names missing {name}"

    def test_hrefs_use_the_streamlit_static_route(self):
        """Files are reachable only under app/static/, never a bare path."""
        for _, href, _ in theme._HEAD_LINKS:
            assert href.startswith("app/static/")

    def test_covers_the_relations_streamlit_cannot_set(self):
        rels = {rel for rel, _, _ in theme._HEAD_LINKS}
        assert {"apple-touch-icon", "manifest"} <= rels


class TestPageIcon:
    def test_returns_the_favicon_path_when_present(self):
        assert str(theme.FAVICON) == theme.page_icon()
        assert theme.FAVICON.is_file()

    def test_falls_back_to_an_emoji_when_absent(self, monkeypatch, tmp_path):
        """Streamlit raises on a page_icon path that does not exist."""
        monkeypatch.setattr(theme, "FAVICON", tmp_path / "absent.png")
        assert theme.page_icon().startswith(":")


class TestStylesheet:
    def test_defines_every_palette_token_as_a_css_variable(self):
        css = theme._stylesheet()
        for value in theme.PALETTE.values():
            assert value in css

    def test_loads_the_brand_webfonts(self):
        css = theme._stylesheet()
        assert "fonts.googleapis.com" in css
        for family in ("Sora", "Inter", "JetBrains+Mono"):
            assert family in css

    def test_ambient_glow_cannot_intercept_clicks(self):
        """A fixed full-viewport overlay must never swallow a click."""
        css = theme._stylesheet()
        glow = css.split('[data-testid="stAppViewContainer"]::before')[1].split("}")[0]
        assert "pointer-events:none" in glow

    def test_targets_stable_test_ids_not_hashed_class_names(self):
        """Emotion class names change between Streamlit releases; testids do not."""
        css = theme._stylesheet()
        assert "st-emotion-cache" not in css
        assert 'data-testid="stAppViewContainer"' in css

    def test_hides_the_built_in_uploader_hint(self):
        """It advertises .ppt, which the application accepts only to reject."""
        assert 'stFileUploaderDropzoneInstructions' in theme._stylesheet()


class TestStepRail:
    def test_marks_done_active_and_idle_distinctly(self):
        html = _render(theme.step_rail, [("Upload", "done"), ("Review", "active"), ("Report", "idle")])
        assert "is-done" in html
        assert "is-active" in html
        assert html.count("tc-step") == 4  # container plus three chips

    def test_completed_steps_show_a_check_not_a_number(self):
        html = _render(theme.step_rail, [("Upload", "done")])
        assert "&#10003;" in html

    def test_pending_steps_are_numbered_in_order(self):
        html = _render(theme.step_rail, [("A", "idle"), ("B", "idle"), ("C", "idle")])
        assert ">1<" in html and ">2<" in html and ">3<" in html


class TestStatusDot:
    @pytest.mark.parametrize(
        "state,expected",
        [("done", "teal"), ("attention", "amber"), ("action", "coral"), ("idle", "ink_600")],
    )
    def test_maps_state_to_the_semantic_colour(self, state, expected):
        assert theme.PALETTE[expected] in theme.status_dot(state, "label")

    def test_unknown_state_falls_back_to_idle(self):
        assert theme.PALETTE["ink_600"] in theme.status_dot("nonsense", "label")


def _render(func, *args) -> str:
    """Capture what a theme component writes to Streamlit."""
    captured = []

    class _Recorder:
        @staticmethod
        def markdown(body, **kwargs):
            captured.append(body)

    original = theme.st
    theme.st = _Recorder()
    try:
        func(*args)
    finally:
        theme.st = original
    return "".join(captured)
