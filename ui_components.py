"""Small, reusable presentational widgets for Yeelight PC Companion.

Everything here is **pure presentation**: the widgets carry no configuration,
power, process or network logic. They render state that the pages pass in, so
the automation code stays untouched by the Stage 5 redesign.

The set is intentionally tiny — one container, one status pill, one navigation
button, one integration card and a handful of label helpers — instead of a UI
framework.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui_theme import (
    SPACE_L,
    SPACE_M,
    SPACE_S,
    SPACE_XS,
    TONE_NEUTRAL,
    pill_qss,
)


def hint_label(text="", parent=None):
    """A small muted explanatory line (the app stylesheet styles ``#hint``)."""
    label = QLabel(text, parent)
    label.setObjectName("hint")
    label.setWordWrap(True)
    return label


def muted_label(text="", parent=None):
    """A muted, non-wrapping detail label (addresses, coordinates, ...)."""
    label = QLabel(text, parent)
    label.setObjectName("hint")
    return label


def field_label(text="", parent=None):
    """A form label, visually subordinate to its control."""
    label = QLabel(text, parent)
    label.setObjectName("hint")
    label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    return label


def card_title_label(text="", parent=None):
    """Card heading text (bold, no coloured chip)."""
    label = QLabel(text, parent)
    label.setObjectName("card_title")
    return label


class CardSeparator(QFrame):
    """A one-pixel separator that follows the theme's border colour."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card_separator")
        self.setFrameShape(QFrame.Shape.HLine)
        self.setFixedHeight(1)


class SectionCard(QFrame):
    """An elevated card with a title, an actions slot and a content body.

    Layout, not behaviour: callers add their own widgets and keep their own
    data flow.
    """

    def __init__(self, title="", description="", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setFrameShape(QFrame.Shape.NoFrame)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE_L, SPACE_M, SPACE_L, SPACE_L)
        outer.setSpacing(SPACE_S)

        self.header_row = QHBoxLayout()
        self.header_row.setSpacing(SPACE_S)
        self.title_column = QVBoxLayout()
        self.title_column.setSpacing(2)

        self.lbl_title = card_title_label(title)
        self.title_column.addWidget(self.lbl_title)
        self.lbl_description = None
        if description:
            self.lbl_description = hint_label(description)
            self.title_column.addWidget(self.lbl_description)

        self.header_row.addLayout(self.title_column, 1)
        outer.addLayout(self.header_row)

        separator = CardSeparator()
        outer.addWidget(separator)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, SPACE_XS, 0, 0)
        self.body.setSpacing(SPACE_S)
        outer.addLayout(self.body)

    def add_action(self, widget):
        """Add a control to the card's header row (right-hand side)."""
        self.header_row.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
        return widget

    def add_widget(self, widget):
        self.body.addWidget(widget)
        return widget

    def add_layout(self, layout):
        self.body.addLayout(layout)
        return layout

    def add_hint(self, text):
        return self.add_widget(hint_label(text))

    def add_separator(self):
        separator = CardSeparator()
        self.body.addWidget(separator)
        return separator


class StatusPill(QLabel):
    """A compact status badge.

    The tone only *adds* to the message: the text itself always names the state
    (``Running``, ``Stopped``, ``System active``, ...), so the status is never
    conveyed by colour alone.

    ``set_status`` is a no-op when neither the text nor the tone changed. Qt does
    not do that by itself for ``setStyleSheet`` - an identical stylesheet still
    costs a full style re-application (observed as one ``StyleChange`` plus two
    ``PaletteChange`` and two ``FontChange`` events per call) - and the service
    rows are the one place that sets status on a repeating timer, where almost
    every tick repeats the previous state.
    """

    def __init__(self, text="", tone=TONE_NEUTRAL, parent=None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self._tone = None
        self.set_status(text, tone)

    def set_status(self, text, tone=TONE_NEUTRAL):
        if text == self.text() and tone == self._tone:
            return
        self._tone = tone
        self.setText(text)
        self.setStyleSheet(pill_qss(tone))


class StatusRow(QFrame):
    """One compact status line: a named state and its status pill.

    ``detail`` is explanatory text, so it is shown as a tooltip instead of a
    second line: four of these rows must stay compact enough to read at once.
    """

    def __init__(self, title="", detail="", parent=None):
        super().__init__(parent)
        self.setObjectName("status_row")
        self.setFrameShape(QFrame.Shape.NoFrame)
        if detail:
            self.setToolTip(detail)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACE_M, SPACE_XS + 2, SPACE_M, SPACE_XS + 2)
        layout.setSpacing(SPACE_M)

        self.lbl_title = QLabel(title)
        self.lbl_title.setObjectName("value_strong")
        if detail:
            self.lbl_title.setToolTip(detail)

        layout.addWidget(self.lbl_title, 1)
        self.pill = StatusPill("Checking...", TONE_NEUTRAL)
        layout.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_status(self, text, tone=TONE_NEUTRAL):
        self.pill.set_status(text, tone)


class SidebarButton(QPushButton):
    """A checkable navigation entry; exactly one is checked at a time."""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setObjectName("nav_button")
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)


class IntegrationCard(SectionCard):
    """One integration: enable toggle, executable path and native Browse.

    The enable toggle driving the path/Browse enablement is the same
    presentation rule the settings form always had; nothing here decides what an
    integration *means* at runtime.
    """

    def __init__(
        self,
        label,
        description="",
        placeholder="",
        with_action=False,
        with_service_status=False,
        parent=None,
    ):
        super().__init__(label, description, parent)

        self.chk_enabled = QCheckBox("Enabled")
        self.chk_enabled.setToolTip(
            "A disabled integration is never launched, stopped, waited for or used for self-healing."
        )
        self.add_action(self.chk_enabled)

        path_row = QHBoxLayout()
        path_row.setSpacing(SPACE_S)
        self.txt_path = QLineEdit()
        self.txt_path.setPlaceholderText(placeholder)
        self.txt_path.setMinimumWidth(220)
        self.btn_browse = QPushButton("Browse...")
        path_row.addWidget(self.txt_path, 1)
        path_row.addWidget(self.btn_browse, 0)
        self.add_layout(path_row)

        self.chk_enabled.toggled.connect(self.txt_path.setEnabled)
        self.chk_enabled.toggled.connect(self.btn_browse.setEnabled)

        # Optional status area (used by OpenRGB for the seamless elevated
        # launch state: a status pill, one action button and a hint line).
        self.lbl_status = None
        self.btn_action = None
        self.lbl_hint = None
        if with_action:
            status_row = QHBoxLayout()
            status_row.setSpacing(SPACE_S)
            status_label = field_label("Elevated launch")
            status_label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            self.lbl_status = StatusPill("Checking...", TONE_NEUTRAL)
            self.btn_action = QPushButton("")
            status_row.addWidget(status_label, 0)
            status_row.addWidget(self.lbl_status, 0)
            status_row.addStretch(1)
            status_row.addWidget(self.btn_action, 0)
            self.add_layout(status_row)

            self.lbl_hint = hint_label("")
            self.add_widget(self.lbl_hint)

        # Optional second status area for the OpenRGB Windows-service conflict.
        # Deliberately separate from the elevated-launch status above: the two
        # describe different things (Task Scheduler launch vs. SCM service).
        self.lbl_service_status = None
        self.btn_service_action = None
        self.lbl_service_hint = None
        if with_service_status:
            service_row = QHBoxLayout()
            service_row.setSpacing(SPACE_S)
            service_label = field_label("Windows service")
            service_label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            self.lbl_service_status = StatusPill("Checking...", TONE_NEUTRAL)
            self.btn_service_action = QPushButton("")
            service_row.addWidget(service_label, 0)
            service_row.addWidget(self.lbl_service_status, 0)
            service_row.addStretch(1)
            service_row.addWidget(self.btn_service_action, 0)
            self.add_layout(service_row)

            self.lbl_service_hint = hint_label("")
            self.add_widget(self.lbl_service_hint)

    def set_enabled_state(self, enabled):
        self.chk_enabled.setChecked(bool(enabled))
        self.txt_path.setEnabled(bool(enabled))
        self.btn_browse.setEnabled(bool(enabled))


def scrollable(widget):
    """Wrap a page body in a transparent, resizable scroll area."""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    scroll.setWidget(widget)
    return scroll


def page_body():
    """A transparent page container with the standard spacing rhythm."""
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, SPACE_S, 0)
    layout.setSpacing(SPACE_M)
    widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
    return widget, layout


__all__ = [
    "CardSeparator",
    "IntegrationCard",
    "SectionCard",
    "SidebarButton",
    "StatusPill",
    "StatusRow",
    "card_title_label",
    "field_label",
    "hint_label",
    "muted_label",
    "page_body",
    "scrollable",
]
