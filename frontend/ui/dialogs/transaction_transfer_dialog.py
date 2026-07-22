from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QDateEdit, QLineEdit
)
from PySide6.QtCore import QDate
from PySide6.QtGui import QFont
from ..theme import COLORS, FONTS
from ...services.api_client import api


class TransactionTransferDialog(QDialog):
    def __init__(self, txn: dict, parent=None):
        super().__init__(parent)
        self.txn = txn
        self.setWindowTitle("Transfer Transaction")
        self.setFixedSize(420, 320)
        self.setModal(True)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header.setFixedHeight(56)
        header.setStyleSheet(f"background-color: {COLORS['primary']};")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 0, 20, 0)
        title = QLabel("TRANSFER TO ANOTHER DAY")
        title.setFont(QFont(FONTS["family"], FONTS["size_lg"], QFont.Bold))
        title.setStyleSheet(f"color: {COLORS['secondary']}; letter-spacing: 1px; background: transparent;")
        hl.addWidget(title)
        layout.addWidget(header)

        body = QFrame()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(24, 20, 24, 12)
        bl.setSpacing(10)

        current_date = self.txn.get("business_date", "")
        info_lbl = QLabel(f"Bag {self.txn.get('bag_number', '')}  ·  currently posted to  {current_date}")
        info_lbl.setWordWrap(True)
        info_lbl.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: {FONTS['size_sm']}pt;")
        bl.addWidget(info_lbl)

        bl.addWidget(QLabel("New business date"))
        self.new_date = QDateEdit(QDate.currentDate())
        self.new_date.setCalendarPopup(True)
        self.new_date.setFixedHeight(36)
        self.new_date.setDisplayFormat("yyyy-MM-dd")
        bl.addWidget(self.new_date)

        bl.addWidget(QLabel("Reason (required)"))
        self.reason_input = QLineEdit()
        self.reason_input.setPlaceholderText("Why is this transaction being moved?")
        self.reason_input.setFixedHeight(36)
        bl.addWidget(self.reason_input)

        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorMsg")
        self.error_label.setWordWrap(True)
        bl.addWidget(self.error_label)
        bl.addStretch()

        layout.addWidget(body, 1)

        footer = QFrame()
        footer.setStyleSheet(f"border-top: 1px solid {COLORS['border']}; background: {COLORS['bg_main']};")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(20, 12, 20, 12)
        cancel_btn = QPushButton("CANCEL")
        cancel_btn.setObjectName("SecondaryBtn")
        cancel_btn.setFixedHeight(38)
        cancel_btn.clicked.connect(self.reject)
        transfer_btn = QPushButton("TRANSFER")
        transfer_btn.setFixedHeight(38)
        transfer_btn.clicked.connect(self._on_transfer)
        fl.addWidget(cancel_btn)
        fl.addStretch()
        fl.addWidget(transfer_btn)
        layout.addWidget(footer)

    def _on_transfer(self):
        reason = self.reason_input.text().strip()
        if not reason:
            self.error_label.setText("A reason is required.")
            return

        new_date_str = self.new_date.date().toString("yyyy-MM-dd")
        result = api.transfer_transaction(self.txn["transaction_id"], new_date_str, reason)
        if result.ok:
            self.accept()
        else:
            self.error_label.setText(result.error or "Transfer failed.")
