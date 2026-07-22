from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QTableWidget, QTableWidgetItem, QHeaderView, QLineEdit,
    QDateEdit, QMessageBox
)
from PySide6.QtCore import Qt, QDate
from PySide6.QtGui import QFont, QColor
from ..theme import COLORS, FONTS
from ...services.api_client import api
from ..dialogs.transaction_detail_dialog import TransactionDetailDialog
from ..dialogs.eod_reopen_dialog import EODReopenDialog


class TransactionViewerPage(QWidget):
    def __init__(self, user_data: dict, parent=None):
        super().__init__(parent)
        self.user_data = user_data
        self.transactions: list[dict] = []
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Page header
        header = QFrame()
        header.setObjectName("PageHeader")
        hl = QVBoxLayout(header)
        hl.setContentsMargins(24, 16, 24, 16)
        title = QLabel("Transaction Viewer")
        title.setObjectName("PageTitle")
        sub = QLabel("Supervisor  ▶  Transaction Viewer")
        sub.setObjectName("PageSubtitle")
        hl.addWidget(title)
        hl.addWidget(sub)
        layout.addWidget(header)

        # Filter bar
        filter_bar = QFrame()
        filter_bar.setStyleSheet(f"QFrame {{ background-color: {COLORS['bg_white']}; border-bottom: 1px solid {COLORS['border']}; }}")
        fb = QHBoxLayout(filter_bar)
        fb.setContentsMargins(16, 10, 16, 10)
        fb.setSpacing(10)

        fb.addWidget(QLabel("From:"))
        self.date_from = QDateEdit(QDate.currentDate().addDays(-30))
        self.date_from.setCalendarPopup(True)
        self.date_from.setFixedHeight(32)
        self.date_from.setDisplayFormat("yyyy-MM-dd")
        fb.addWidget(self.date_from)

        fb.addWidget(QLabel("To:"))
        self.date_to = QDateEdit(QDate.currentDate())
        self.date_to.setCalendarPopup(True)
        self.date_to.setFixedHeight(32)
        self.date_to.setDisplayFormat("yyyy-MM-dd")
        fb.addWidget(self.date_to)

        fb.addWidget(QLabel("Customer:"))
        self.filter_customer = QLineEdit()
        self.filter_customer.setPlaceholderText("Customer ID…")
        self.filter_customer.setFixedWidth(130)
        self.filter_customer.setFixedHeight(32)
        fb.addWidget(self.filter_customer)

        fb.addWidget(QLabel("Location:"))
        self.filter_location = QLineEdit()
        self.filter_location.setPlaceholderText("Location ID…")
        self.filter_location.setFixedWidth(130)
        self.filter_location.setFixedHeight(32)
        fb.addWidget(self.filter_location)

        search_btn = QPushButton("SEARCH")
        search_btn.setFixedHeight(32)
        search_btn.setFixedWidth(90)
        search_btn.clicked.connect(self.load_transactions)
        fb.addWidget(search_btn)

        clear_btn = QPushButton("CLEAR FILTERS")
        clear_btn.setObjectName("SecondaryBtn")
        clear_btn.setFixedHeight(32)
        clear_btn.clicked.connect(self._clear_filters)
        fb.addWidget(clear_btn)

        fb.addStretch()

        role = self.user_data.get("role", "cashier")
        if role in ("supervisor", "administrator"):
            close_day_btn = QPushButton("CLOSE DAY")
            close_day_btn.setObjectName("SecondaryBtn")
            close_day_btn.setFixedHeight(32)
            close_day_btn.clicked.connect(self._on_close_day)
            fb.addWidget(close_day_btn)

        if role == "administrator":
            reopen_day_btn = QPushButton("REOPEN DAY")
            reopen_day_btn.setObjectName("SecondaryBtn")
            reopen_day_btn.setFixedHeight(32)
            reopen_day_btn.clicked.connect(self._on_reopen_day)
            fb.addWidget(reopen_day_btn)

        self.record_count_lbl = QLabel("0 records")
        self.record_count_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: {FONTS['size_xs']}pt;")
        fb.addWidget(self.record_count_lbl)

        layout.addWidget(filter_bar)

        # Table
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels([
            "Transaction ID", "Date / Time", "User",
            "Customer", "Location", "Bag Number", "Expected", "Total", "Status"
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.doubleClicked.connect(self._on_row_double_click)
        self.table.setColumnWidth(0, 300)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 120)
        self.table.setColumnWidth(3, 160)
        self.table.setColumnWidth(4, 160)
        self.table.setColumnWidth(5, 130)
        self.table.setColumnWidth(6, 110)
        self.table.setColumnWidth(7, 110)
        layout.addWidget(self.table, 1)

        # Bottom bar
        bottom = QFrame()
        bottom.setStyleSheet(f"background: {COLORS['bg_main']}; border-top: 1px solid {COLORS['border']};")
        btm = QHBoxLayout(bottom)
        btm.setContentsMargins(16, 8, 16, 8)
        hint = QLabel("Double-click a row to view full transaction details.")
        hint.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: {FONTS['size_xs']}pt; font-style: italic;")
        btm.addWidget(hint)
        btm.addStretch()
        layout.addWidget(bottom)

    def load_transactions(self):
        try:
            params = {}
            if self.filter_customer.text().strip():
                params["customer_id"] = self.filter_customer.text().strip()
            if self.filter_location.text().strip():
                params["location_id"] = self.filter_location.text().strip()
            params["date_from"] = self.date_from.date().toString("yyyy-MM-dd") + "T00:00:00"
            params["date_to"] = self.date_to.date().toString("yyyy-MM-dd") + "T23:59:59"

            print(f"[VIEWER] Fetching transactions, params={params}")
            result = api.list_transactions(params)
            print(f"[VIEWER] status={result.status_code} ok={result.ok} error={result.error!r} data_type={type(result.data).__name__} data_len={len(result.data) if isinstance(result.data, list) else result.data}")

            if not result.ok:
                QMessageBox.warning(self, "Error", f"HTTP {result.status_code}: {result.error}")
                return

            self.transactions = result.data or []
            self._populate_table()
        except Exception as exc:
            print(f"[VIEWER] load_transactions exception: {exc}")
            import traceback; traceback.print_exc()

    def _populate_table(self):
        try:
            self.table.setSortingEnabled(False)
            self.table.setRowCount(0)

            for txn in self.transactions:
                row = self.table.rowCount()
                self.table.insertRow(row)

                txn_id = str(txn.get("transaction_id", ""))
                created = str(txn.get("created_at", ""))
                date_str = created[:10] if len(created) >= 10 else created
                time_str = created[11:19] if len(created) >= 19 else ""

                raw_expected = txn.get("expected_total")
                expected_str = f"€{float(raw_expected):,.2f}" if raw_expected is not None else "—"
                total_str = f"€{float(txn.get('total_value', 0)):,.2f}"

                items = [
                    txn_id,
                    f"{date_str}  {time_str}",
                    txn.get("username", "") or txn.get("user_id", "")[:8],
                    txn.get("customer_id", ""),
                    txn.get("location_id", ""),
                    txn.get("bag_number", ""),
                    expected_str,
                    total_str,
                    txn.get("balance_status", ""),
                ]

                for col, value in enumerate(items):
                    item = QTableWidgetItem(str(value))
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    if col in (6, 7):
                        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    self.table.setItem(row, col, item)

                # Color-code status
                status = txn.get("balance_status", "")
                status_item = self.table.item(row, 8)
                if status_item:
                    if status == "BALANCED":
                        status_item.setForeground(QColor(COLORS["success"]))
                        status_item.setFont(QFont(FONTS["family"], FONTS["size_sm"], QFont.Bold))
                    elif "NOT" in status:
                        status_item.setForeground(QColor(COLORS["warning"]))
                        status_item.setFont(QFont(FONTS["family"], FONTS["size_sm"], QFont.Bold))

            self.table.setSortingEnabled(True)
            self.record_count_lbl.setText(f"{len(self.transactions)} records")
        except Exception as exc:
            print(f"[VIEWER] _populate_table exception: {exc}")
            import traceback; traceback.print_exc()

    def _on_row_double_click(self, index):
        row = index.row()
        txn_id_item = self.table.item(row, 0)
        if not txn_id_item:
            return
        txn_id = txn_id_item.text().strip()
        if not txn_id:
            return

        result = api.get_transaction(txn_id)
        if not result.ok:
            QMessageBox.warning(self, "Error", result.error)
            return

        is_admin = self.user_data.get("role") == "administrator"
        dlg = TransactionDetailDialog(result.data, is_admin=is_admin, parent=self)
        dlg.transferred.connect(self.load_transactions)
        dlg.exec()

    def _on_close_day(self):
        today = QDate.currentDate().toString("yyyy-MM-dd")
        confirm = QMessageBox.question(
            self, "Close Day",
            f"Close business day {today} for this catalog? No further transactions "
            f"will be accepted for this catalog until an administrator reopens it.",
        )
        if confirm != QMessageBox.Yes:
            return

        result = api.close_eod(today)
        if result.ok:
            QMessageBox.information(self, "Day Closed", f"Business day {today} is now closed.")
        else:
            QMessageBox.warning(self, "Error", result.error or "Could not close the day.")

    def _on_reopen_day(self):
        dlg = EODReopenDialog(parent=self)
        dlg.exec()

    def _clear_filters(self):
        self.filter_customer.clear()
        self.filter_location.clear()
        self.date_from.setDate(QDate.currentDate().addDays(-30))
        self.date_to.setDate(QDate.currentDate())
        self.load_transactions()
