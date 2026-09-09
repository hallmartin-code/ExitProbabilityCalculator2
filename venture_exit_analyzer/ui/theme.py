"""TEN Capital Network design system for the Streamlit interface.

Dark navy ground with a coral / amber / teal accent triad, echoing the three
figures in the network mark.

Where the styling lives
-----------------------
Most of it is expressed in ``.streamlit/config.toml``, which Streamlit applies to
its own components directly. That is deliberate: theme configuration is a
supported API, whereas CSS targeting Streamlit's generated class names breaks on
upgrade. The CSS here is limited to what configuration cannot express -- the
gradient rule, the brand lockup, the upload dropzone, the CTA gradient and the
step rail -- and it targets ``data-testid`` attributes, which are stable, rather
than hashed class names.

The palette below is the single source of truth. ``config.toml`` mirrors these
values, and :func:`check_config_matches_palette` asserts the two agree so they
cannot drift apart silently.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import streamlit as st

#: Public asset directory, served at /app/static/ when enableStaticServing is on.
#: Streamlit resolves this as `Path(entrypoint).parent / "static"` (see
#: streamlit/file_util.py), so it must sit beside app.py -- not at the project
#: root, where it would simply never be served.
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

#: The browser tab icon. Streamlit embeds whatever this points at, so a mid-size
#: PNG keeps the payload small while staying crisp on high-DPI displays.
FAVICON = STATIC_DIR / "favicon-192.png"

#: Artwork for the in-app brand lockup, at roughly 2x its display size.
BRAND_ICON = STATIC_DIR / "favicon-96.png"

# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
PALETTE: Dict[str, str] = {
    # Ground
    "navy_950": "#0B1526",   # page background
    "navy_900": "#101E33",   # card top
    "navy_800": "#16283F",   # card bottom / secondary surfaces
    "navy_700": "#1E354F",   # borders
    # Accent triad
    "coral": "#EE5A4E",      # primary action, risk
    "coral_soft": "#F0776C",
    "amber": "#F3A22A",      # attention, gaps
    "teal": "#35BEBB",       # confirmed, complete
    # Ink
    "ink_100": "#F3F6FA",    # primary text
    "ink_300": "#C4D0E0",    # secondary text
    "ink_500": "#7E90A8",    # muted text
    "ink_600": "#5C6E86",    # faint text
}

FONT_BODY = "Inter"
FONT_HEADING = "Sora"
FONT_MONO = "JetBrains Mono"

GOOGLE_FONTS_URL = (
    "https://fonts.googleapis.com/css2"
    "?family=Sora:wght@400;600;700;800"
    "&family=Inter:wght@400;500;600"
    "&family=JetBrains+Mono:wght@400;500"
    "&display=swap"
)

#: Semantic use of the accent triad. Colour carries meaning here rather than
#: decoration, so a glance at the interface reports state.
STATUS_COLOURS: Dict[str, str] = {
    "done": PALETTE["teal"],
    "attention": PALETTE["amber"],
    "action": PALETTE["coral"],
    "idle": PALETTE["ink_600"],
}


# ---------------------------------------------------------------------------
# Brand mark
# ---------------------------------------------------------------------------
@lru_cache(maxsize=4)
def _data_uri(path: str) -> Optional[str]:
    """Base64 data URI for a local image, or None when it is not there."""
    file = Path(path)
    if not file.is_file():
        return None
    return "data:image/png;base64," + base64.b64encode(file.read_bytes()).decode("ascii")


def brand_mark(size: int = 38) -> str:
    """The TEN Capital Network mark.

    Uses the real artwork from ``static/``, embedded as a data URI so the lockup
    needs no second request and cannot flash unstyled. The browser tab and the
    header therefore show the same mark -- two different marks in one product
    reads as a mistake.

    Falls back to a drawn approximation when the artwork is absent, so the
    interface still renders in a checkout where the icons have not been built.
    """
    uri = _data_uri(str(BRAND_ICON))
    if uri:
        return (
            f'<img src="{uri}" width="{size}" height="{size}" '
            f'alt="TEN Capital Network" style="display:block;flex-shrink:0;" />'
        )
    return _drawn_mark(size)


def _drawn_mark(size: int = 38) -> str:
    """Vector fallback, used only when the real artwork is unavailable.

    The source design rotated the coral arc by 180 degrees about (50, 79), which
    landed it on the right-hand side overlapping the teal arc and detached from
    its own circle. The transform is omitted here so each arc sits beneath the
    circle it belongs to, and the circle centres are aligned to the arc origins.
    """
    return f"""
<svg width="{size}" height="{size}" viewBox="0 0 100 100" fill="none"
     xmlns="http://www.w3.org/2000/svg" aria-label="TEN Capital Network" role="img">
  <path d="M50 6 C64 6 74 16 74 16" stroke="{PALETTE['amber']}"
        stroke-width="11" stroke-linecap="round" fill="none"/>
  <path d="M76 66 C76 82 63 92 63 92" stroke="{PALETTE['teal']}"
        stroke-width="11" stroke-linecap="round" fill="none"/>
  <path d="M24 66 C24 82 37 92 37 92" stroke="{PALETTE['coral']}"
        stroke-width="11" stroke-linecap="round" fill="none"/>
  <circle cx="50" cy="20" r="11" fill="{PALETTE['amber']}"/>
  <circle cx="76" cy="67" r="11" fill="{PALETTE['teal']}"/>
  <circle cx="24" cy="67" r="11" fill="{PALETTE['coral']}"/>
</svg>""".strip()


# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------
def _stylesheet() -> str:
    p = PALETTE
    return f"""
@import url('{GOOGLE_FONTS_URL}');

:root {{
  --navy-950:{p['navy_950']}; --navy-900:{p['navy_900']};
  --navy-800:{p['navy_800']}; --navy-700:{p['navy_700']};
  --coral:{p['coral']}; --coral-soft:{p['coral_soft']};
  --amber:{p['amber']}; --teal:{p['teal']};
  --ink-100:{p['ink_100']}; --ink-300:{p['ink_300']};
  --ink-500:{p['ink_500']}; --ink-600:{p['ink_600']};
}}

/* Ambient tri-colour glow, echoing the mark's three figures. Fixed and
   non-interactive so it never intercepts a click. */
[data-testid="stAppViewContainer"]::before {{
  content:""; position:fixed; inset:0; pointer-events:none; z-index:0;
  background:
    radial-gradient(480px 380px at 14% 6%, rgba(238,90,78,0.14), transparent 60%),
    radial-gradient(480px 380px at 86% 4%, rgba(243,162,42,0.11), transparent 60%),
    radial-gradient(620px 460px at 50% 100%, rgba(53,190,187,0.10), transparent 60%);
}}
[data-testid="stAppViewContainer"] > * {{ position:relative; z-index:1; }}

/* Streamlit's own chrome would otherwise sit as a light band above the app. */
[data-testid="stHeader"] {{ background:transparent; }}

/* ---- brand lockup ---- */
.tc-brand {{ display:flex; align-items:center; gap:12px; margin:0 0 6px; }}
.tc-brand-word {{
  font-family:'{FONT_HEADING}',sans-serif; font-weight:800; font-size:15px;
  letter-spacing:.04em; line-height:1.15; color:var(--ink-100); text-transform:uppercase;
}}
.tc-brand-word span {{
  display:block; font-weight:600; font-size:10px; letter-spacing:.22em;
  color:var(--ink-500); margin-top:2px;
}}

/* ---- hero card ---- */
.tc-card {{
  background:linear-gradient(180deg,var(--navy-900) 0%,var(--navy-800) 100%);
  border:1px solid var(--navy-700); border-radius:20px;
  padding:34px 34px 28px; margin:14px 0 22px; position:relative; overflow:hidden;
  box-shadow:0 30px 60px -20px rgba(0,0,0,.55), inset 0 1px 0 rgba(255,255,255,.03);
}}
.tc-card::after {{
  content:""; position:absolute; top:-2px; left:34px; right:34px; height:2px;
  background:linear-gradient(90deg,var(--coral),var(--amber),var(--teal));
  border-radius:2px;
}}
.tc-eyebrow {{
  display:flex; align-items:center; gap:8px; font-family:'{FONT_MONO}',monospace;
  font-size:11px; letter-spacing:.14em; text-transform:uppercase;
  color:var(--teal); margin-bottom:12px;
}}
.tc-eyebrow::before {{
  content:""; width:6px; height:6px; border-radius:50%; background:var(--teal);
  box-shadow:0 0 0 3px rgba(53,190,187,.18);
}}
.tc-title {{
  font-family:'{FONT_HEADING}',sans-serif; font-size:27px; font-weight:700;
  line-height:1.25; margin:0 0 10px; letter-spacing:-.01em; color:var(--ink-100);
}}
.tc-title .arrow {{ color:var(--ink-500); font-weight:400; margin:0 4px; }}
.tc-title .to {{
  background:linear-gradient(90deg,var(--coral-soft),var(--amber));
  -webkit-background-clip:text; background-clip:text; color:transparent;
}}
.tc-lede {{ color:var(--ink-300); font-size:15px; line-height:1.6; margin:0; max-width:56ch; }}

/* ---- step rail ---- */
.tc-steps {{ display:flex; gap:8px; margin:0 0 18px; flex-wrap:wrap; }}
.tc-step {{
  display:flex; align-items:center; gap:8px; padding:7px 13px 7px 9px;
  border:1px solid var(--navy-700); border-radius:999px;
  background:rgba(255,255,255,.015); font-size:12px; color:var(--ink-500);
  font-family:'{FONT_MONO}',monospace; letter-spacing:.02em;
}}
.tc-step .dot {{
  width:18px; height:18px; border-radius:50%; display:flex; align-items:center;
  justify-content:center; font-size:10px; font-weight:600; color:var(--navy-950);
  background:var(--ink-600); flex-shrink:0;
}}
.tc-step.is-done {{ color:var(--ink-300); border-color:rgba(53,190,187,.35); }}
.tc-step.is-done .dot {{ background:var(--teal); }}
.tc-step.is-active {{ color:var(--ink-100); border-color:rgba(238,90,78,.45); }}
.tc-step.is-active .dot {{ background:var(--coral); }}

/* ---- section label ---- */
.tc-section {{
  font-family:'{FONT_MONO}',monospace; font-size:11px; letter-spacing:.14em;
  text-transform:uppercase; color:var(--ink-500);
  margin:22px 0 10px; padding-bottom:7px; border-bottom:1px solid var(--navy-700);
}}
.tc-section.first {{ margin-top:4px; }}

/* ---- accepted file types ---- */
.tc-filetypes {{
  font-family:'{FONT_MONO}',monospace; font-size:11.5px; color:var(--ink-500);
  margin:-4px 0 10px; letter-spacing:.01em;
}}
.tc-filetypes b {{ color:var(--ink-300); font-weight:500; }}

/* ---- upload dropzone ---- */
[data-testid="stFileUploaderDropzone"] {{
  border:1.5px dashed var(--navy-700) !important; border-radius:14px !important;
  background:rgba(255,255,255,.015) !important; padding:26px 20px !important;
  transition:border-color .18s ease, background .18s ease;
}}
[data-testid="stFileUploaderDropzone"]:hover {{
  border-color:var(--teal) !important; background:rgba(53,190,187,.05) !important;
}}
[data-testid="stFileUploaderDropzone"] svg {{ fill:var(--ink-500); }}

/* Streamlit's built-in hint lists every extension the uploader will accept,
   which includes .ppt -- a format this application deliberately accepts only in
   order to reject it with a useful message. Advertising it as supported would
   be misleading, and the accurate line sits directly above the dropzone, so the
   built-in one is hidden. If this selector ever stops matching, the built-in
   text simply reappears alongside ours rather than anything breaking. */
[data-testid="stFileUploaderDropzoneInstructions"] {{ display:none !important; }}

/* ---- primary CTA gradient ---- */
.stButton button[kind="primary"], .stDownloadButton button[kind="primary"] {{
  background:linear-gradient(90deg,var(--coral) 0%,var(--coral-soft) 45%,var(--amber) 100%);
  color:#17130E !important; border:none;
  font-family:'{FONT_HEADING}',sans-serif; font-weight:700; letter-spacing:.01em;
  box-shadow:0 10px 24px -10px rgba(238,90,78,.45);
  transition:filter .15s ease, transform .15s ease;
}}
.stButton button[kind="primary"]:hover, .stDownloadButton button[kind="primary"]:hover {{
  filter:brightness(1.06); transform:translateY(-1px); color:#17130E !important;
}}
.stButton button[kind="primary"]:disabled {{
  background:var(--navy-800); color:var(--ink-600) !important; box-shadow:none;
}}

/* ---- tabs ---- */
button[data-baseweb="tab"] {{
  font-family:'{FONT_MONO}',monospace; font-size:12px !important;
  letter-spacing:.06em; text-transform:uppercase;
}}

/* ---- disclosure note ---- */
.tc-note {{
  margin-top:14px; padding-top:14px; border-top:1px solid var(--navy-700);
  font-size:12px; line-height:1.6; color:var(--ink-500);
}}
.tc-note code {{
  font-family:'{FONT_MONO}',monospace; background:var(--navy-950);
  border:1px solid var(--navy-700); color:var(--ink-300);
  padding:2px 6px; border-radius:5px; font-size:11.5px;
}}

/* ---- footer ---- */
.tc-footer {{
  text-align:center; margin:34px 0 8px; font-family:'{FONT_MONO}',monospace;
  font-size:11px; letter-spacing:.08em; color:var(--ink-600); text-transform:uppercase;
}}
"""


def page_icon():
    """What to hand ``st.set_page_config(page_icon=...)``.

    Returns the favicon path when the generated assets are present, and a
    neutral emoji otherwise: Streamlit raises on a path that does not exist, and
    a missing icon must not stop the application from starting.
    """
    return str(FAVICON) if FAVICON.is_file() else ":large_orange_diamond:"


def inject() -> None:
    """Apply the stylesheet. Call once, first thing after ``set_page_config``."""
    st.markdown(f"<style>{_stylesheet()}</style>", unsafe_allow_html=True)
    _inject_icon_links()


#: Icon relations Streamlit has no API for, added to <head> by _inject_icon_links.
_HEAD_LINKS: Tuple[Tuple[str, str, str], ...] = (
    ("apple-touch-icon", "app/static/apple-touch-icon.png", "180x180"),
    ("manifest", "app/static/site.webmanifest", ""),
)


def _inject_icon_links() -> None:
    """Add the iOS touch icon and web manifest to the document head.

    The tab favicon itself comes from ``st.set_page_config(page_icon=...)``,
    which is the supported mechanism and lands in <head> correctly. These two
    relations have no Streamlit API.

    They must go in <head>: emitting them through ``st.markdown`` puts them in
    the body, where Chrome silently ignores both -- verified by observing that
    the browser issued no request for either. A zero-height component runs a
    same-origin iframe whose script can reach ``window.parent.document.head``,
    which is the only route Streamlit offers.

    Everything here is an enhancement. The favicon does not depend on it, and a
    failure is silent by design rather than breaking the page.
    """
    if not (STATIC_DIR / "apple-touch-icon.png").is_file():
        return

    links = ", ".join(
        f'{{rel:"{rel}", href:"{href}", sizes:"{sizes}"}}'
        for rel, href, sizes in _HEAD_LINKS
    )
    st.components.v1.html(
        f"""<script>
(function () {{
  try {{
    var head = window.parent.document.head;
    if (!head) return;
    [{links}].forEach(function (spec) {{
      // Idempotent: Streamlit reruns this on every interaction.
      var id = "tc-icon-" + spec.rel;
      if (window.parent.document.getElementById(id)) return;
      var link = window.parent.document.createElement("link");
      link.id = id;
      link.rel = spec.rel;
      link.href = spec.href;
      if (spec.sizes) link.sizes = spec.sizes;
      head.appendChild(link);
    }});
  }} catch (e) {{
    /* Cross-origin or sandboxed: the favicon is unaffected. */
  }}
}})();
</script>""",
        height=0,
    )


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------
def brand_header() -> None:
    st.markdown(
        f'<div class="tc-brand">{brand_mark()}'
        f'<div class="tc-brand-word">Ten Capital<span>Network</span></div></div>',
        unsafe_allow_html=True,
    )


def hero(eyebrow: str, title_html: str, lede: str, note_html: str = "") -> None:
    """The framing card at the top of the app."""
    note = f'<div class="tc-note">{note_html}</div>' if note_html else ""
    st.markdown(
        f'<div class="tc-card"><div class="tc-eyebrow">{eyebrow}</div>'
        f'<div class="tc-title">{title_html}</div>'
        f'<p class="tc-lede">{lede}</p>{note}</div>',
        unsafe_allow_html=True,
    )


def step_rail(steps: List[Tuple[str, str]]) -> None:
    """Workflow progress. ``steps`` is a list of (label, state).

    States are ``done``, ``active`` or ``idle``. This exists because the review
    step is a hard gate -- the report cannot be generated until inputs are
    confirmed -- and a gate the user cannot see reads as a broken button.
    """
    chips = []
    for index, (label, state) in enumerate(steps, start=1):
        css = {"done": "is-done", "active": "is-active"}.get(state, "")
        glyph = "&#10003;" if state == "done" else str(index)
        chips.append(
            f'<div class="tc-step {css}"><span class="dot">{glyph}</span>{label}</div>'
        )
    st.markdown(f'<div class="tc-steps">{"".join(chips)}</div>', unsafe_allow_html=True)


def section(label: str, first: bool = False) -> None:
    st.markdown(
        f'<div class="tc-section{" first" if first else ""}">{label}</div>',
        unsafe_allow_html=True,
    )


def footer(text: str = "Powered by TEN Capital Network") -> None:
    st.markdown(f'<div class="tc-footer">{text}</div>', unsafe_allow_html=True)


def status_dot(state: str, label: str) -> str:
    """An inline coloured status marker for the sidebar."""
    colour = STATUS_COLOURS.get(state, STATUS_COLOURS["idle"])
    return (
        f'<span style="display:inline-flex;align-items:center;gap:7px;">'
        f'<span style="width:7px;height:7px;border-radius:50%;background:{colour};'
        f'display:inline-block;"></span>'
        f'<span style="color:{PALETTE["ink_300"]};font-size:13px;">{label}</span></span>'
    )


# ---------------------------------------------------------------------------
# Drift guard
# ---------------------------------------------------------------------------
def check_config_matches_palette(config_text: str) -> List[str]:
    """Report palette values that ``config.toml`` does not agree with.

    The theme is expressed in two places -- Python tokens and Streamlit
    configuration -- because Streamlit styles its own widgets from the latter.
    Two sources of truth drift, so a test calls this to keep them honest.
    """
    required = {
        "backgroundColor": PALETTE["navy_950"],
        "secondaryBackgroundColor": PALETTE["navy_800"],
        "primaryColor": PALETTE["coral"],
        "textColor": PALETTE["ink_100"],
        "borderColor": PALETTE["navy_700"],
    }
    lowered = config_text.lower()
    return [
        f"{key} should be {value}"
        for key, value in required.items()
        if f'{key.lower()} = "{value.lower()}"' not in lowered
    ]
