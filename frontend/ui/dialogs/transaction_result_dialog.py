from decimal import Decimal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from ..theme import COLORS, FONTS


class TransactionResultDialog(QDialog):
    def __init__(self, txn_data: dict, expected_total: Decimal = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Transaction Result")
        self.setFixedSize(460, 420)
        self.setModal(True)
        self._build(txn_data, expected_total)

    def _build(self, txn, expected_total):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        total_value = Decimal(str(txn.get("total_value", 0)))

        if expected_total is not None and expected_total > 0:
            diff = total_value - expected_total
            if diff == 0:
                verdict = "BALANCED"
                verdict_color = COLORS["success"]
                icon_char = "✓"
            elif diff < 0:
                verdict = f"SHORTAGE  €{abs(diff):,.2f}"
                verdict_color = COLORS["warning"]
                icon_char = "▼"
            else:
                verdict = f"OVERAGE  €{abs(diff):,.2f}"
                verdict_color = "#1565C0"
                icon_char = "▲"
        else:
            diff = Decimal("0")
            verdict = txn.get("balance_status", "PENDING")
            verdict_color = COLORS["success"] if verdict == "BALANCED" else COLORS["warning"]
            icon_char = "✓" if verdict == "BALANCED" else "⚠"

        # Banner
        banner = QFrame()
        banner.setFixedHeight(140)
        banner.setStyleSheet(f"background-color: {verdict_color};")
        bl = QVBoxLayout(banner)
        bl.setAlignment(Qt.AlignCenter)
        bl.setSpacing(6)

        icon = QLabel(icon_char)
        icon.setAlignment(Qt.AlignCenter)
        icon.setFont(QFont(FONTS["family"], 36, QFont.Bold))
        icon.setStyleSheet("color: white; background: transparent;")

        verdict_lbl = QLabel(verdict)
        verdict_lbl.setAlignment(Qt.AlignCenter)
        verdict_lbl.setFont(QFont(FONTS["family"], FONTS["size_xl"], QFont.Bold))
        verdict_lbl.setStyleSheet("color: white; letter-spacing: 2px; background: transparent;")

        bl.addWidget(icon)
        bl.addWidget(verdict_lbl)
        layout.addWidget(banner)

        # Body
        body = QFrame()
        body.setStyleSheet(f"QFrame {{ background-color: {COLORS['bg_white']}; }}")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(28, 20, 28, 20)
        body_layout.setSpacing(10)

        title_lbl = QLabel("Transaction Completed")
        title_lbl.setFont(QFont(FONTS["family"], FONTS["size_lg"], QFont.Bold))
        title_lbl.setAlignment(Qt.AlignCenter)
        title_lbl.setStyleSheet(f"color: {COLORS['primary']};")
        body_layout.addWidget(title_lbl)

        def _info_row(label: str, value: str, highlight=False):
            row = QHBoxLayout()
            l = QLabel(label)
            l.setObjectName("FieldLabel")
            l.setFixedWidth(150)
            v = QLabel(value)
            if highlight:
                v.setStyleSheet(f"font-size: {FONTS['size_sm']}pt; font-weight: bold; color: {verdict_color};")
            else:
                v.setStyleSheet(f"font-size: {FONTS['size_sm']}pt; color: {COLORS['text_primary']};")
            row.addWidget(l)
            row.addWidget(v)
            row.addStretch()
            return row

        body_layout.addLayout(_info_row("TRANSACTION ID", str(txn.get("transaction_id", "—"))))
        body_layout.addLayout(_info_row("BAG NUMBER", txn.get("bag_number", "—")))
        body_layout.addLayout(_info_row("TOTAL PROCESSED", f"€{float(total_value):,.2f}"))
        if expected_total is not None and expected_total > 0:
            body_layout.addLayout(_info_row("EXPECTED TOTAL", f"€{float(expected_total):,.2f}"))
            body_layout.addLayout(_info_row(
                "DIFFERENCE",
                f"€{float(diff):+,.2f}",
                highlight=True,
            ))

        body_layout.addStretch()
        layout.addWidget(body, 1)

        # Footer
        footer = QFrame()
        footer.setStyleSheet(f"QFrame {{ border-top: 1px solid {COLORS['border']}; background: {COLORS['bg_main']}; }}")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(20, 12, 20, 12)
        ok_btn = QPushButton("OK  —  New Transaction")
        ok_btn.setFixedHeight(40)
        ok_btn.clicked.connect(self.accept)
        fl.addStretch()
        fl.addWidget(ok_btn)
        layout.addWidget(footer)
