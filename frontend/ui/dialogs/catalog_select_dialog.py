from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from ..theme import COLORS, FONTS
from ...services.api_client import api


class CatalogSelectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Brink's Nexus — Select Processing Catalog")
        self.setFixedSize(420, 520)
        self.setWindowFlags(Qt.Dialog)
        self.selected_catalog_code: str | None = None
        self.selected_catalog_name: str | None = None
        self._error_label: QLabel | None = None
        self._body_layout: QVBoxLayout | None = None
        self._setup_ui()
        self._load_catalogs()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header.setFixedHeight(110)
        header.setStyleSheet(f"background-color: {COLORS['primary']};")
        h_layout = QVBoxLayout(header)
        h_layout.setAlignment(Qt.AlignCenter)
        h_layout.setSpacing(4)

        title = QLabel("SELECT PROCESSING CATALOG")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont(FONTS["family"], FONTS["size_lg"], QFont.Bold))
        title.setStyleSheet(f"color: {COLORS['secondary']}; letter-spacing: 1.5px;")

        subtitle = QLabel("Choose which FI you're processing for")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet(f"color: rgba(255,255,255,0.75); font-size: {FONTS['size_sm']}pt;")

        h_layout.addWidget(title)
        h_layout.addWidget(subtitle)

        body = QFrame()
        body.setStyleSheet(f"background-color: {COLORS['bg_white']};")
        self._body_layout = QVBoxLayout(body)
        self._body_layout.setContentsMargins(24, 24, 24, 24)
        self._body_layout.setSpacing(10)

        self._error_label = QLabel("")
        self._error_label.setObjectName("ErrorMsg")
        self._error_label.setAlignment(Qt.AlignCenter)
        self._error_label.setWordWrap(True)
        self._body_layout.addWidget(self._error_label)
        self._body_layout.addStretch()

        footer = QFrame()
        footer.setFixedHeight(36)
        footer.setStyleSheet(f"background-color: {COLORS['bg_main']}; border-top: 1px solid {COLORS['border']};")
        f_layout = QHBoxLayout(footer)
        f_layout.setContentsMargins(16, 0, 16, 0)
        hint = QLabel("You can switch catalogs by logging out and back in")
        hint.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: {FONTS['size_xs']}pt;")
        f_layout.addWidget(hint)

        layout.addWidget(header)
        layout.addWidget(body, 1)
        layout.addWidget(footer)

    def _load_catalogs(self):
        result = api.list_catalogs()
        if not result.ok:
            self._error_label.setText(result.error or "Could not load catalogs from the server.")
            return

        for entry in result.data:
            self._body_layout.insertWidget(self._body_layout.count() - 1, self._make_catalog_button(entry))

    def _make_catalog_button(self, entry: dict) -> QPushButton:
        btn = QPushButton(entry["display_name"])
        btn.setFixedHeight(52)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFont(QFont(FONTS["family"], FONTS["size_md"], QFont.Bold))
        btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {COLORS['bg_main']};
                color: {COLORS['text_primary']};
                border: 1px solid {COLORS['border']};
                border-radius: 4px;
                text-align: left;
                padding-left: 16px;
            }}
            QPushButton:hover {{
                background-color: {COLORS['row_hover']};
                border-color: {COLORS['secondary']};
            }}
            """
        )
        btn.clicked.connect(lambda: self._on_select(entry["code"], entry["display_name"]))
        return btn

    def _on_select(self, code: str, display_name: str):
        self.selected_catalog_code = code
        self.selected_catalog_name = display_name
        api.set_catalog(code)
        self.accept()
