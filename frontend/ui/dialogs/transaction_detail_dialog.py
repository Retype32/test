from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QTableWidget, QTableWidgetItem, QHeaderView, QGridLayout, QScrollArea, QWidget, QMessageBox
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from ..theme import COLORS, FONTS
from .transaction_transfer_dialog import TransactionTransferDialog


class TransactionDetailDialog(QDialog):
    transferred = Signal()

    def __init__(self, txn: dict, is_admin: bool = False, parent=None):
        super().__init__(parent)
        self.txn = txn
        self.is_admin = is_admin
        self.setWindowTitle("Transaction Details")
        self.setMinimumSize(600, 700)
        self.setModal(True)
        self._build(txn)

    def _build(self, txn: dict):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        hdr = QFrame()
        hdr.setFixedHeight(56)
        hdr.setStyleSheet(f"background-color: {COLORS['primary']};")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(20, 0, 20, 0)
        title_lbl = QLabel("TRANSACTION DETAILS")
        title_lbl.setFont(QFont(FONTS["family"], FONTS["size_lg"], QFont.Bold))
        title_lbl.setStyleSheet(f"color: {COLORS['secondary']}; letter-spacing: 1px; background: transparent;")
        hl.addWidget(title_lbl)
        layout.addWidget(hdr)

        # Scrollable body
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(24, 20, 24, 20)
        bl.setSpacing(16)

        status = txn.get("balance_status", "PENDING")
        is_balanced = status == "BALANCED"
        status_color = COLORS["success"] if is_balanced else COLORS["warning"]
        status_bg = COLORS["success_bg"] if is_balanced else COLORS["warning_bg"]

        # Status badge
        status_badge = QLabel(f"  {'✓  BALANCED' if is_balanced else '⚠  NOT BALANCED'}  ")
        status_badge.setAlignment(Qt.AlignCenter)
        status_badge.setFixedHeight(40)
        status_badge.setFont(QFont(FONTS["family"], FONTS["size_md"], QFont.Bold))
        status_badge.setStyleSheet(
            f"background-color: {status_bg}; color: {status_color}; "
            f"border: 2px solid {status_color}; border-radius: 4px;"
        )
        bl.addWidget(status_badge)

        def _section(text):
            lbl = QLabel(text)
            lbl.setStyleSheet(
                f"font-size: {FONTS['size_xs']}pt; font-weight: bold; "
                f"color: {COLORS['primary']}; letter-spacing: 0.8px; "
                f"border-bottom: 1px solid {COLORS['border']}; padding-bottom: 4px;"
            )
            return lbl

        def _field_grid(fields: list[tuple[str, str]]) -> QGridLayout:
            grid = QGridLayout()
            grid.setSpacing(8)
            for i, (label, value) in enumerate(fields):
                lbl = QLabel(label)
                lbl.setObjectName("FieldLabel")
                lbl.setFixedWidth(140)
                val = QLabel(value)
                val.setObjectName("ValueLabel")
                val.setWordWrap(True)
                grid.addWidget(lbl, i, 0)
                grid.addWidget(val, i, 1)
            return grid

        created = txn.get("created_at", "")
        date_str = created[:10] if len(created) >= 10 else created
        time_str = created[11:19] if len(created) >= 19 else ""

        bl.addWidget(_section("TRANSACTION INFORMATION"))
        bl.addLayout(_field_grid([
            ("TRANSACTION ID", str(txn.get("transaction_id", ""))),
            ("DATE", date_str),
            ("TIME", time_str),
            ("BUSINESS DATE", str(txn.get("business_date", ""))),
            ("PROCESSED BY", txn.get("username") or str(txn.get("user_id", ""))),
        ]))

        bl.addWidget(_section("CUSTOMER & LOCATION"))
        bl.addLayout(_field_grid([
            ("CUSTOMER ID",  txn.get("customer_id", "")),
            ("LOCATION ID",  txn.get("location_id", "")),
            ("BAG NUMBER",   txn.get("bag_number", "")),
        ]))

        bl.addWidget(_section("DENOMINATION BREAKDOWN"))

        denoms = txn.get("denominations", [])
        if denoms:
            table = QTableWidget(len(denoms), 3)
            table.setHorizontalHeaderLabels(["Denomination", "Count", "Value"])
            table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            table.verticalHeader().setVisible(False)
            table.setEditTriggers(QTableWidget.NoEditTriggers)
            table.setAlternatingRowColors(True)
            table.setFixedHeight(len(denoms) * 34 + 38)

            for row_idx, d in enumerate(denoms):
                table.setItem(row_idx, 0, QTableWidgetItem(d.get("denomination", "")))
                table.setItem(row_idx, 1, QTableWidgetItem(str(d.get("count", 0))))
                val_item = QTableWidgetItem(f"€{float(d.get('value', 0)):,.2f}")
                val_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                table.setItem(row_idx, 2, val_item)

            bl.addWidget(table)
        else:
            bl.addWidget(QLabel("No denomination data available."))

        # Total
        total_frame = QFrame()
        total_frame.setStyleSheet(f"background-color: {COLORS['primary']}; border-radius: 3px;")
        tf = QHBoxLayout(total_frame)
        tf.setContentsMargins(16, 12, 16, 12)
        tf_lbl = QLabel("TOTAL CASH VALUE")
        tf_lbl.setStyleSheet(f"color: rgba(255,255,255,0.75); font-weight: bold; font-size: {FONTS['size_xs']}pt; background: transparent;")
        tf_val = QLabel(f"€{float(txn.get('total_value', 0)):,.2f}")
        tf_val.setFont(QFont(FONTS["family"], FONTS["size_xl"], QFont.Bold))
        tf_val.setStyleSheet(f"color: {COLORS['secondary']}; background: transparent;")
        tf.addWidget(tf_lbl)
        tf.addStretch()
        tf.addWidget(tf_val)
        bl.addWidget(total_frame)
        bl.addStretch()

        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

        # Footer
        footer = QFrame()
        footer.setStyleSheet(f"border-top: 1px solid {COLORS['border']}; background: {COLORS['bg_main']};")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(20, 12, 20, 12)

        if self.is_admin:
            transfer_btn = QPushButton("TRANSFER TO ANOTHER DAY")
            transfer_btn.setObjectName("SecondaryBtn")
            transfer_btn.setFixedHeight(38)
            transfer_btn.clicked.connect(self._on_transfer_clicked)
            fl.addWidget(transfer_btn)

        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(38)
        close_btn.setFixedWidth(100)
        close_btn.clicked.connect(self.accept)
        fl.addStretch()
        fl.addWidget(close_btn)
        layout.addWidget(footer)

    def _on_transfer_clicked(self):
        dlg = TransactionTransferDialog(self.txn, parent=self)
        if dlg.exec() == QDialog.Accepted:
            QMessageBox.information(self, "Transferred", "Transaction moved to the new business date.")
            self.transferred.emit()
            self.accept()
