from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QGridLayout, QLineEdit, QMessageBox
)
from PySide6.QtGui import QFont
from ..theme import COLORS, FONTS
from ...services.api_client import api


class DuplicateReviewDialog(QDialog):
    def __init__(self, flag: dict, parent=None):
        super().__init__(parent)
        self.flag = flag
        self.reviewed = False
        self.setWindowTitle("Review Possible Duplicate")
        self.setFixedSize(480, 420)
        self.setModal(True)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header.setFixedHeight(56)
        header.setStyleSheet(f"background-color: {COLORS['error']};")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 0, 20, 0)
        title = QLabel("POSSIBLE DUPLICATE TRANSACTION")
        title.setFont(QFont(FONTS["family"], FONTS["size_lg"], QFont.Bold))
        title.setStyleSheet("color: white; letter-spacing: 1px; background: transparent;")
        hl.addWidget(title)
        layout.addWidget(header)

        body = QFrame()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(24, 20, 24, 12)
        bl.setSpacing(10)

        matched = self.flag.get("matched_fields", [])
        info_lbl = QLabel(
            f"Transaction {str(self.flag.get('transaction_id', ''))[:8]} looks identical to "
            f"transaction {str(self.flag.get('duplicate_of_transaction_id', ''))[:8]} "
            f"entered by the same processor on the same business day."
        )
        info_lbl.setWordWrap(True)
        info_lbl.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: {FONTS['size_sm']}pt;")
        bl.addWidget(info_lbl)

        grid = QGridLayout()
        grid.setSpacing(6)
        grid.addWidget(QLabel("Matched fields:"), 0, 0)
        matched_lbl = QLabel(", ".join(matched) if matched else "—")
        matched_lbl.setWordWrap(True)
        matched_lbl.setStyleSheet(f"color: {COLORS['text_primary']}; font-weight: bold;")
        grid.addWidget(matched_lbl, 0, 1)
        grid.addWidget(QLabel("Detected at:"), 1, 0)
        grid.addWidget(QLabel(str(self.flag.get("detected_at", ""))[:19]), 1, 1)
        bl.addLayout(grid)

        bl.addWidget(QLabel("Notes (optional)"))
        self.notes_input = QLineEdit()
        self.notes_input.setPlaceholderText("What did you find when you checked this?")
        self.notes_input.setFixedHeight(36)
        bl.addWidget(self.notes_input)

        hint = QLabel(
            "Confirm if this is truly the same bag entered twice. Dismiss if it's a "
            "legitimate coincidence (e.g. two identical, separately verified bags)."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: {FONTS['size_xs']}pt; font-style: italic;")
        bl.addWidget(hint)
        bl.addStretch()

        layout.addWidget(body, 1)

        footer = QFrame()
        footer.setStyleSheet(f"border-top: 1px solid {COLORS['border']}; background: {COLORS['bg_main']};")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(20, 12, 20, 12)

        dismiss_btn = QPushButton("DISMISS (Not a Duplicate)")
        dismiss_btn.setObjectName("SecondaryBtn")
        dismiss_btn.setFixedHeight(38)
        dismiss_btn.clicked.connect(lambda: self._submit("dismissed"))

        confirm_btn = QPushButton("CONFIRM DUPLICATE")
        confirm_btn.setFixedHeight(38)
        confirm_btn.setStyleSheet(f"background-color: {COLORS['error']}; color: white; font-weight: bold;")
        confirm_btn.clicked.connect(lambda: self._submit("confirmed"))

        fl.addWidget(dismiss_btn)
        fl.addStretch()
        fl.addWidget(confirm_btn)
        layout.addWidget(footer)

    def _submit(self, status: str):
        result = api.review_duplicate(self.flag["id"], status, self.notes_input.text().strip() or None)
        if not result.ok:
            QMessageBox.warning(self, "Error", result.error or "Could not submit review.")
            return
        self.reviewed = True
        self.accept()
