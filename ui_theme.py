"""Centralised visual system for Yeelight PC Companion (Stage 5 redesign).

This module owns the application's presentation tokens and Qt styles:

* the colour palette (graphite/dark-navy surfaces, one restrained indigo
  accent, semantic green/amber/red states),
* typography (Windows system font, no bundled or downloaded fonts),
* the spacing rhythm and corner radii used by every screen,
* the application stylesheet, the wizard additions and the dialog stylesheet,
* small helpers for semantic colours (pills, status text).

It is **presentation only**. Nothing here reads the configuration, touches the
network, inspects or starts processes, or talks to the power layer: the
automation engine must stay exactly as testable as it was before the redesign.
Widgets built from these tokens live in :mod:`ui_components`.

The palette is deliberately restrained and low-glare:

* surfaces are near-neutral graphite with a faint blue cast,
* the single accent is a muted indigo used for selection and primary actions,
* green = healthy/running, amber = warning/night/attention,
* red is reserved for destructive or error states.
"""

from __future__ import annotations

from PyQt6.QtGui import QColor, QPalette

# ---------------------------------------------------------
# Surfaces
# ---------------------------------------------------------
BG_APP = "#0E1116"           # window / content background
BG_SIDEBAR = "#12161D"       # navigation column
BG_CARD = "#161B22"          # elevated card
BG_ROW = "#1A2029"           # row inside a card (device row, status row)
BG_INPUT = "#0F1319"         # text fields
BG_INPUT_DISABLED = "#141922"
BG_BUTTON = "#1D232D"
BG_BUTTON_HOVER = "#262E3A"
BG_BUTTON_PRESSED = "#161B23"
BG_DISABLED = "#171C24"
BG_ACCENT_SOFT = "#1B2136"   # selected navigation item / soft accent surface

# ---------------------------------------------------------
# Borders and separators
# ---------------------------------------------------------
BORDER = "#232A35"
BORDER_STRONG = "#333C4A"

# ---------------------------------------------------------
# Text
# ---------------------------------------------------------
TEXT = "#E6EAF0"
TEXT_MUTED = "#9AA5B4"
TEXT_DIM = "#6E7A8A"
TEXT_ON_ACCENT = "#FFFFFF"

# ---------------------------------------------------------
# Accent (primary action, selection, focus)
# ---------------------------------------------------------
ACCENT = "#5B6CE0"
ACCENT_HOVER = "#6C7CEC"
ACCENT_PRESSED = "#4A5ACB"
ACCENT_TEXT = "#9BA8FF"      # accent-tinted text on a dark surface

# ---------------------------------------------------------
# Semantic states
# ---------------------------------------------------------
OK = "#3FB950"
OK_SOFT = "#14291D"
WARN = "#D29922"
WARN_SOFT = "#2E2611"
DANGER = "#E5534B"
DANGER_SOFT = "#2C1A1A"
DANGER_HOVER = "#362020"
NEUTRAL = "#8B95A5"
NEUTRAL_SOFT = "#1C222C"

# Tone names used by `ui_components` and the pages.
TONE_NEUTRAL = "neutral"
TONE_OK = "ok"
TONE_WARN = "warn"
TONE_DANGER = "danger"
TONE_ACCENT = "accent"

# tone -> (foreground, soft background) for pills and status text.
TONES = {
    TONE_NEUTRAL: (NEUTRAL, NEUTRAL_SOFT),
    TONE_OK: (OK, OK_SOFT),
    TONE_WARN: (WARN, WARN_SOFT),
    TONE_DANGER: (DANGER, DANGER_SOFT),
    TONE_ACCENT: (ACCENT_TEXT, BG_ACCENT_SOFT),
}

# ---------------------------------------------------------
# Typography
# ---------------------------------------------------------
# Windows system fonts only — nothing is downloaded or bundled.
FONT_FAMILY = "Segoe UI"
MONO_FONT_FAMILY = "Cascadia Mono"

# ---------------------------------------------------------
# Spacing rhythm and radii
# ---------------------------------------------------------
SPACE_XS = 4
SPACE_S = 8
SPACE_M = 12
SPACE_L = 16
SPACE_XL = 24

RADIUS_SM = 6
RADIUS_MD = 8
RADIUS_LG = 12
RADIUS_PILL = 10

_TOKENS = {
    "font": FONT_FAMILY,
    "mono": MONO_FONT_FAMILY,
    "bg_app": BG_APP,
    "bg_sidebar": BG_SIDEBAR,
    "bg_card": BG_CARD,
    "bg_row": BG_ROW,
    "bg_input": BG_INPUT,
    "bg_input_disabled": BG_INPUT_DISABLED,
    "bg_button": BG_BUTTON,
    "bg_button_hover": BG_BUTTON_HOVER,
    "bg_button_pressed": BG_BUTTON_PRESSED,
    "bg_disabled": BG_DISABLED,
    "bg_accent_soft": BG_ACCENT_SOFT,
    "border": BORDER,
    "border_strong": BORDER_STRONG,
    "text": TEXT,
    "text_muted": TEXT_MUTED,
    "text_dim": TEXT_DIM,
    "text_on_accent": TEXT_ON_ACCENT,
    "accent": ACCENT,
    "accent_hover": ACCENT_HOVER,
    "accent_pressed": ACCENT_PRESSED,
    "accent_text": ACCENT_TEXT,
    "ok": OK,
    "ok_soft": OK_SOFT,
    "warn": WARN,
    "warn_soft": WARN_SOFT,
    "danger": DANGER,
    "danger_soft": DANGER_SOFT,
    "danger_hover": DANGER_HOVER,
    "neutral": NEUTRAL,
    "neutral_soft": NEUTRAL_SOFT,
    "radius_sm": f"{RADIUS_SM}px",
    "radius_md": f"{RADIUS_MD}px",
    "radius_lg": f"{RADIUS_LG}px",
    "radius_pill": f"{RADIUS_PILL}px",
    "font_small": "12px",
    "font_base": "13px",
    "font_title": "20px",
}

# ---------------------------------------------------------
# Application stylesheet
# ---------------------------------------------------------
# `%`-templating (not an f-string) because QSS is full of braces.
APP_QSS = """
QWidget {
    color: %(text)s;
    font-family: "%(font)s";
    font-size: %(font_base)s;
}

QMainWindow, QDialog, QWizard {
    background-color: %(bg_app)s;
}

QFrame#sidebar {
    background-color: %(bg_sidebar)s;
    border: none;
    border-right: 1px solid %(border)s;
}

QLabel {
    background: transparent;
}

/* --- typography roles ------------------------------------------------- */
QLabel#page_title {
    font-size: %(font_title)s;
    font-weight: 600;
    color: %(text)s;
}
QLabel#page_subtitle {
    font-size: %(font_small)s;
    color: %(text_muted)s;
}
QLabel#card_title {
    font-size: 14px;
    font-weight: 600;
    color: %(text)s;
}
QLabel#card_description, QLabel#hint {
    font-size: %(font_small)s;
    color: %(text_muted)s;
}
QLabel#warning {
    font-size: %(font_small)s;
    color: %(warn)s;
}
QLabel#value_strong {
    font-weight: 600;
    color: %(text)s;
}
QLabel#app_name {
    font-size: 15px;
    font-weight: 600;
    color: %(text)s;
}
QLabel#app_section {
    font-size: 11px;
    font-weight: 600;
    color: %(text_dim)s;
}

/* --- containers ------------------------------------------------------- */
QFrame#card {
    background-color: %(bg_card)s;
    border: 1px solid %(border)s;
    border-radius: %(radius_lg)s;
}
QFrame#card_separator {
    background-color: %(border)s;
    border: none;
    max-height: 1px;
}
QFrame#device_row, QFrame#status_row {
    background-color: %(bg_row)s;
    border: 1px solid %(border)s;
    border-radius: %(radius_md)s;
}
QScrollArea {
    background: transparent;
    border: none;
}

/* --- navigation ------------------------------------------------------- */
QPushButton#nav_button {
    background-color: transparent;
    border: 1px solid transparent;
    border-left: 3px solid transparent;
    border-radius: %(radius_md)s;
    color: %(text_muted)s;
    font-weight: 600;
    padding: 8px 10px;
    text-align: left;
}
QPushButton#nav_button:hover {
    background-color: %(bg_button)s;
    color: %(text)s;
}
QPushButton#nav_button:checked {
    background-color: %(bg_accent_soft)s;
    border-left: 3px solid %(accent)s;
    color: %(text)s;
}
QPushButton#nav_button:focus {
    border: 1px solid %(accent)s;
    border-left: 3px solid %(accent)s;
}

/* --- buttons ---------------------------------------------------------- */
QPushButton {
    background-color: %(bg_button)s;
    border: 1px solid %(border_strong)s;
    border-radius: %(radius_md)s;
    color: %(text)s;
    font-weight: 600;
    padding: 6px 14px;
    min-height: 18px;
}
QPushButton:hover {
    background-color: %(bg_button_hover)s;
}
QPushButton:pressed {
    background-color: %(bg_button_pressed)s;
}
QPushButton:focus {
    border: 1px solid %(accent)s;
}
QPushButton:disabled {
    background-color: %(bg_disabled)s;
    border-color: %(border)s;
    color: %(text_dim)s;
}
QPushButton#btn_primary {
    background-color: %(accent)s;
    border: 1px solid %(accent)s;
    color: %(text_on_accent)s;
}
QPushButton#btn_primary:hover {
    background-color: %(accent_hover)s;
    border-color: %(accent_hover)s;
}
QPushButton#btn_primary:pressed {
    background-color: %(accent_pressed)s;
}
QPushButton#btn_primary:disabled {
    background-color: %(bg_disabled)s;
    border-color: %(border)s;
    color: %(text_dim)s;
}
QPushButton#btn_warning {
    background-color: %(warn_soft)s;
    border: 1px solid %(warn)s;
    color: %(warn)s;
}
QPushButton#btn_warning:hover {
    background-color: #3A3014;
}
QPushButton#btn_danger {
    background-color: %(danger_soft)s;
    border: 1px solid %(danger)s;
    color: %(danger)s;
}
QPushButton#btn_danger:hover {
    background-color: %(danger_hover)s;
}

/* --- inputs ----------------------------------------------------------- */
QLineEdit {
    background-color: %(bg_input)s;
    border: 1px solid %(border_strong)s;
    border-radius: %(radius_sm)s;
    color: %(text)s;
    padding: 6px 8px;
    selection-background-color: %(accent)s;
    selection-color: %(text_on_accent)s;
}
QLineEdit:hover {
    border-color: #3D4757;
}
QLineEdit:focus {
    border: 1px solid %(accent)s;
}
QLineEdit:disabled {
    background-color: %(bg_input_disabled)s;
    border-color: %(border)s;
    color: %(text_dim)s;
}

QCheckBox {
    color: %(text)s;
    spacing: 8px;
    font-size: %(font_base)s;
}
QCheckBox:disabled {
    color: %(text_dim)s;
}
QRadioButton {
    color: %(text)s;
    spacing: 8px;
}
QRadioButton:disabled {
    color: %(text_dim)s;
}

/* --- log view --------------------------------------------------------- */
QTextEdit#log_display {
    background-color: #0B0E13;
    border: 1px solid %(border)s;
    border-radius: %(radius_md)s;
    color: #B9C6D2;
    font-family: "%(mono)s", "Consolas", monospace;
    font-size: 12px;
    padding: 10px;
    selection-background-color: %(accent)s;
}

/* --- group boxes (wizard sections) ------------------------------------ */
QGroupBox {
    background-color: %(bg_card)s;
    border: 1px solid %(border)s;
    border-radius: %(radius_lg)s;
    margin-top: 14px;
    padding: 18px 14px 14px 14px;
    font-weight: 600;
    color: %(text)s;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    background-color: transparent;
    color: %(text_muted)s;
    font-size: %(font_small)s;
}

/* --- tables (discovery results) --------------------------------------- */
QTableWidget {
    background-color: %(bg_input)s;
    alternate-background-color: %(bg_card)s;
    border: 1px solid %(border)s;
    border-radius: %(radius_md)s;
    color: %(text)s;
    gridline-color: %(border)s;
    selection-background-color: %(bg_accent_soft)s;
    selection-color: %(text)s;
}
QHeaderView::section {
    background-color: %(bg_card)s;
    color: %(text_muted)s;
    border: none;
    border-right: 1px solid %(border)s;
    border-bottom: 1px solid %(border)s;
    padding: 6px;
    font-weight: 600;
}

/* --- scroll bars ------------------------------------------------------ */
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #313A47;
    border-radius: 5px;
    min-height: 28px;
}
QScrollBar::handle:vertical:hover {
    background: #3D4757;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 2px;
}
QScrollBar::handle:horizontal {
    background: #313A47;
    border-radius: 5px;
    min-width: 28px;
}
QScrollBar::add-line, QScrollBar::sub-line {
    width: 0px;
    height: 0px;
}
QScrollBar::add-page, QScrollBar::sub-page {
    background: transparent;
}

/* --- misc ------------------------------------------------------------- */
QToolTip {
    background-color: %(bg_card)s;
    border: 1px solid %(border_strong)s;
    border-radius: %(radius_sm)s;
    color: %(text)s;
    padding: 4px 6px;
}
""" % _TOKENS

# ---------------------------------------------------------
# Wizard additions (QWizard-specific rules the app sheet omits)
# ---------------------------------------------------------
WIZARD_QSS = """
QWizard {
    background-color: %(bg_app)s;
}
QWizardPage {
    background-color: transparent;
}
QWizard QLabel#hint {
    color: %(text_muted)s;
    font-size: %(font_small)s;
}
QWizard QLabel#warning {
    color: %(warn)s;
    font-size: %(font_small)s;
}
""" % _TOKENS

# ---------------------------------------------------------
# Dialog stylesheet
# ---------------------------------------------------------
# Dialogs are top-level windows, so they do not inherit the main window's
# stylesheet. They get the shared tokens plus the container/table rules they
# actually use, so device, discovery and result dialogs cannot drift into a
# second, slightly different dark palette.
DIALOG_QSS = APP_QSS + """
QDialog {
    background-color: %(bg_app)s;
}
QProgressBar {
    background-color: %(bg_input)s;
    border: 1px solid %(border)s;
    border-radius: %(radius_sm)s;
    color: %(text)s;
    text-align: center;
}
QProgressBar::chunk {
    background-color: %(accent)s;
    border-radius: %(radius_sm)s;
}
""" % _TOKENS

# The dialog palette roles. Kept explicit so a dialog constructed before (or
# outside) the application-wide palette still renders dark.
_DIALOG_PALETTE_ROLES = (
    (QPalette.ColorRole.Window, BG_APP),
    (QPalette.ColorRole.WindowText, TEXT),
    (QPalette.ColorRole.Base, BG_INPUT),
    (QPalette.ColorRole.AlternateBase, BG_CARD),
    (QPalette.ColorRole.Text, TEXT),
    (QPalette.ColorRole.Button, BG_BUTTON),
    (QPalette.ColorRole.ButtonText, TEXT),
    (QPalette.ColorRole.Highlight, ACCENT),
    (QPalette.ColorRole.HighlightedText, TEXT_ON_ACCENT),
    (QPalette.ColorRole.ToolTipBase, BG_CARD),
    (QPalette.ColorRole.ToolTipText, TEXT),
    (QPalette.ColorRole.PlaceholderText, TEXT_DIM),
)


def dark_palette():
    """The application-wide dark palette (surfaces + roles Qt paints itself)."""
    palette = QPalette()
    for role, value in _DIALOG_PALETTE_ROLES:
        palette.setColor(role, QColor(value))
    # Disabled groups must stay readable, not vanish.
    for group in (QPalette.ColorGroup.Disabled,):
        palette.setColor(group, QPalette.ColorRole.WindowText, QColor(TEXT_DIM))
        palette.setColor(group, QPalette.ColorRole.Text, QColor(TEXT_DIM))
        palette.setColor(group, QPalette.ColorRole.ButtonText, QColor(TEXT_DIM))
        palette.setColor(group, QPalette.ColorRole.Base, QColor(BG_DISABLED))
    return palette


def apply_app_theme(app):
    """Apply the palette, widget style and stylesheet to the whole application.

    Fusion is used deliberately: unlike the Windows Vista style it paints every
    control from the palette, so a dark theme needs no image assets and stays
    consistent at any DPI scaling. Idempotent and safe to call again.
    """
    if app is None:
        return
    try:
        app.setStyle("Fusion")
    except Exception:  # pragma: no cover - defensive: a missing style is not fatal
        pass
    app.setPalette(dark_palette())
    app.setStyleSheet(APP_QSS)


def apply_top_level_theme(widget, stylesheet=None):
    """Apply the dark palette (and an optional stylesheet) to a top-level widget.

    Only ``palette()``/``setPalette()``/``setStyleSheet()`` are used, so this
    also works on the lightweight dialog stand-ins used by the UI tests.
    """
    palette = widget.palette()
    for role, value in _DIALOG_PALETTE_ROLES:
        palette.setColor(role, QColor(value))
    widget.setPalette(palette)
    if stylesheet is not None:
        widget.setStyleSheet(stylesheet)


def apply_dialog_style(dialog):
    """Give a dialog window the same dark palette and stylesheet as the app."""
    apply_top_level_theme(dialog, DIALOG_QSS)


# ---------------------------------------------------------
# Semantic helpers
# ---------------------------------------------------------
def tone_foreground(tone):
    """The text colour of a semantic tone."""
    return TONES.get(tone, TONES[TONE_NEUTRAL])[0]


def tone_background(tone):
    """The soft surface colour of a semantic tone."""
    return TONES.get(tone, TONES[TONE_NEUTRAL])[1]


def pill_qss(tone):
    """Stylesheet for a compact status pill in the given tone."""
    foreground = tone_foreground(tone)
    return (
        f"background-color: {tone_background(tone)};"
        f" border: 1px solid {foreground};"
        f" border-radius: {RADIUS_PILL}px;"
        f" color: {foreground};"
        " font-size: 11px;"
        " font-weight: 600;"
        " padding: 2px 10px;"
    )


def text_qss(tone, weight=600, size=None):
    """Stylesheet for semantic coloured text (never colour-only: text stays)."""
    parts = [f"color: {tone_foreground(tone)};", f"font-weight: {weight};"]
    if size is not None:
        parts.append(f"font-size: {size}px;")
    return " ".join(parts)
