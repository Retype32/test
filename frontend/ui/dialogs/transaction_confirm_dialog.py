from decimal import Decimal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QWidget
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from ..theme import COLORS, FONTS


class TransactionConfirmDialog(QDialog):
    def __init__(self, customer: dict, location: dict, bag_number: str,
                 denominations: list[dict], total: Decimal,
                 expected_total: Decimal = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Confirm Transaction")
        self.setMinimumSize(480, 420)
        self.setModal(True)
        self._build(customer, location, bag_number, total, expected_total)

    def _build(self, customer, location, bag_number, total, expected_total):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        hdr = QFrame()
        hdr.setFixedHeight(56)
        hdr.setStyleSheet(f"background-color: {COLORS['primary']};")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(20, 0, 20, 0)
        title = QLabel("CONFIRM TRANSACTION")
        title.setFont(QFont(FONTS["family"], FONTS["size_lg"], QFont.Bold))
        title.setStyleSheet(f"color: {COLORS['secondary']}; letter-spacing: 1px; background: transparent;")
        hl.addWidget(title)
        layout.addWidget(hdr)

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(28, 24, 28, 24)
        bl.setSpacing(10)

        def _row(label: str, value: str, bold_value=False):
            row = QHBoxLayout()
            l = QLabel(label)
            l.setObjectName("FieldLabel")
            l.setFixedWidth(150)
            v = QLabel(value)
            v.setObjectName("ValueLabel")
            if bold_value:
                v.setStyleSheet(f"font-weight: bold; font-size: {FONTS['size_sm']}pt;")
            row.addWidget(l)
            row.addWidget(v)
            row.addStretch()
            return row

        bl.addLayout(_row("CUSTOMER", f"{customer['customer_name']}  ({customer['customer_id']})"))
        bl.addLayout(_row("LOCATION", f"{location['location_name']}  ({location['location_id']})"))
        bl.addLayout(_row("BAG NUMBER", bag_number))

        sep = QFrame()
        sep.setObjectName("Separator")
        bl.addWidget(sep)

        # Summary block
        summary = QFrame()
        summary.setStyleSheet(
            f"background-color: {COLORS['bg_main']}; border: 1px solid {COLORS['border']}; border-radius: 4px;"
        )
        s_layout = QVBoxLayout(summary)
        s_layout.setContentsMargins(16, 14, 16, 14)
        s_layout.setSpacing(10)

        expected_str = f"€{expected_total:,.2f}" if expected_total else "—"
        total_str = f"€{total:,.2f}"

        if expected_total and expected_total > 0:
            diff = total - expected_total
            if diff == 0:
                diff_str = "€0.00  ✓  BALANCED"
                diff_color = COLORS["success"]
            elif diff < 0:
                diff_str = f"−€{abs(diff):,.2f}  ▼  SHORTAGE"
                diff_color = COLORS["warning"]
            else:
                diff_str = f"+€{abs(diff):,.2f}  ▲  OVERAGE"
                diff_color = "#1565C0"
        else:
            diff_str = "—"
            diff_color = COLORS["text_secondary"]

        def _summary_row(label: str, value: str, color: str = None):
            row = QHBoxLayout()
            l = QLabel(label)
            l.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: {FONTS['size_xs']}pt; font-weight: bold; letter-spacing: 0.5px;")
            v = QLabel(value)
            style = f"font-size: {FONTS['size_md']}pt; font-weight: bold;"
            if color:
                style += f" color: {color};"
            v.setStyleSheet(style)
            v.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(l)
            row.addStretch()
            row.addWidget(v)
            return row

        s_layout.addLayout(_summary_row("EXPECTED TOTAL", expected_str))
        s_layout.addLayout(_summary_row("TOTAL RECEIVED", total_str, COLORS["primary"]))

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setStyleSheet(f"border: 1px solid {COLORS['border']};")
        s_layout.addWidget(sep2)

        s_layout.addLayout(_summary_row("DISCREPANCY", diff_str, diff_color))

        bl.addWidget(summary)
        bl.addStretch()

        layout.addWidget(body, 1)

        # Buttons
        btn_bar = QFrame()
        btn_bar.setStyleSheet(f"QFrame {{ border-top: 1px solid {COLORS['border']}; background: {COLORS['bg_main']}; }}")
        bb = QHBoxLayout(btn_bar)
        bb.setContentsMargins(20, 12, 20, 12)
        bb.setSpacing(12)

        back_btn = QPushButton("◀  Go Back to Transaction")
        back_btn.setObjectName("SecondaryBtn")
        back_btn.setFixedHeight(40)
        back_btn.clicked.connect(self.reject)

        accept_btn = QPushButton("Accept Transaction  ✓")
        accept_btn.setFixedHeight(40)
        accept_btn.clicked.connect(self.accept)

        bb.addWidget(back_btn)
        bb.addStretch()
        bb.addWidget(accept_btn)
        layout.addWidget(btn_bar)
