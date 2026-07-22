from decimal import Decimal, InvalidOperation
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFrame
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from ..theme import COLORS, FONTS


class BagEntryDialog(QDialog):
    def __init__(self, customer_name: str, location_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Bag Details")
        self.setFixedSize(440, 340)
        self.setModal(True)
        self.bag_number: str = ""
        self.expected_total: Decimal = Decimal("0")
        self._build(customer_name, location_name)

    def _build(self, customer_name: str, location_name: str):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header = QFrame()
        header.setFixedHeight(64)
        header.setStyleSheet(f"background-color: {COLORS['primary']};")
        hl = QVBoxLayout(header)
        hl.setContentsMargins(20, 10, 20, 10)
        hl.setSpacing(2)
        title = QLabel("BAG DETAILS")
        title.setFont(QFont(FONTS["family"], FONTS["size_lg"], QFont.Bold))
        title.setStyleSheet(f"color: {COLORS['secondary']}; letter-spacing: 2px;")
        sub = QLabel(f"{customer_name}  ·  {location_name}")
        sub.setStyleSheet("color: rgba(255,255,255,0.7); font-size: 9pt;")
        hl.addWidget(title)
        hl.addWidget(sub)
        layout.addWidget(header)

        # Body
        body = QFrame()
        body.setStyleSheet(f"background-color: {COLORS['bg_white']};")
        bl = QVBoxLayout(body)
        bl.setContentsMargins(32, 28, 32, 28)
        bl.setSpacing(18)

        def _field(label_text, placeholder, echo=False):
            col = QVBoxLayout()
            col.setSpacing(6)
            lbl = QLabel(label_text)
            lbl.setObjectName("FieldLabel")
            inp = QLineEdit()
            inp.setPlaceholderText(placeholder)
            inp.setFixedHeight(40)
            if echo:
                pass
            col.addWidget(lbl)
            col.addWidget(inp)
            return col, inp

        bag_col, self.bag_input = _field("BAG NUMBER", "e.g. BAG-2026-001")
        total_col, self.total_input = _field("EXPECTED TOTAL (€)", "e.g. 1500.00")

        bl.addLayout(bag_col)
        bl.addLayout(total_col)

        self.error_lbl = QLabel("")
        self.error_lbl.setObjectName("ErrorMsg")
        self.error_lbl.setAlignment(Qt.AlignCenter)
        bl.addWidget(self.error_lbl)

        layout.addWidget(body, 1)

        # Footer
        footer = QFrame()
        footer.setStyleSheet(f"background: {COLORS['bg_main']}; border-top: 1px solid {COLORS['border']};")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(20, 12, 20, 12)
        fl.setSpacing(10)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("SecondaryBtn")
        cancel_btn.setFixedHeight(38)
        cancel_btn.setFixedWidth(100)
        cancel_btn.clicked.connect(self.reject)

        ok_btn = QPushButton("PROCEED  ▶")
        ok_btn.setFixedHeight(38)
        ok_btn.setFixedWidth(140)
        ok_btn.setStyleSheet(
            f"background-color: {COLORS['primary']}; color: white; font-weight: bold; border-radius: 3px;"
        )
        ok_btn.clicked.connect(self._on_ok)
        self.bag_input.returnPressed.connect(lambda: self.total_input.setFocus())
        self.total_input.returnPressed.connect(self._on_ok)

        fl.addStretch()
        fl.addWidget(cancel_btn)
        fl.addWidget(ok_btn)
        layout.addWidget(footer)

    def _on_ok(self):
        bag = self.bag_input.text().strip()
        total_text = self.total_input.text().strip()

        if not bag:
            self.error_lbl.setText("Bag number is required.")
            return
        if not total_text:
            self.error_lbl.setText("Expected total is required.")
            return

        try:
            total = Decimal(total_text.replace(",", "."))
            if total < 0:
                raise ValueError
        except (InvalidOperation, ValueError):
            self.error_lbl.setText("Enter a valid amount (e.g. 1500.00).")
            return

        self.bag_number = bag
        self.expected_total = total
        self.accept()
