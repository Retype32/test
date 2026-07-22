from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QTableWidget, QTableWidgetItem, QHeaderView, QDateEdit, QMessageBox
)
from PySide6.QtCore import Qt, QDate
from PySide6.QtGui import QFont, QColor
from ..theme import COLORS, FONTS
from ...services.api_client import api


class StatsPage(QWidget):
    def __init__(self, user_data: dict, parent=None):
        super().__init__(parent)
        self.user_data = user_data
        self.stats: list[dict] = []
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("PageHeader")
        hl = QVBoxLayout(header)
        hl.setContentsMargins(24, 16, 24, 16)
        title = QLabel("Processor Statistics")
        title.setObjectName("PageTitle")
        sub = QLabel("Supervisor  ▶  Statistics")
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
        self.date_from = QDateEdit(QDate.currentDate().addDays(-6))
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

        search_btn = QPushButton("SEARCH")
        search_btn.setFixedHeight(32)
        search_btn.setFixedWidth(90)
        search_btn.clicked.connect(self.refresh)
        fb.addWidget(search_btn)

        fb.addStretch()

        target_lbl = QLabel("Target: 23 slips/hour")
        target_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: {FONTS['size_xs']}pt;")
        fb.addWidget(target_lbl)

        layout.addWidget(filter_bar)

        # Table
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([
            "Processor", "Slips", "Slips / Hour", "Cash Volume", "Duplicate Errors", "Transfer Errors"
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setColumnWidth(0, 160)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 120)
        self.table.setColumnWidth(3, 140)
        self.table.setColumnWidth(4, 130)
        layout.addWidget(self.table, 1)

    def refresh(self):
        try:
            date_from = self.date_from.date().toString("yyyy-MM-dd")
            date_to = self.date_to.date().toString("yyyy-MM-dd")
            result = api.processor_stats_summary(date_from, date_to)
            if not result.ok:
                QMessageBox.warning(self, "Error", f"HTTP {result.status_code}: {result.error}")
                return

            self.stats = result.data or []
            self._populate_table()
        except Exception as exc:
            print(f"[STATS] refresh exception: {exc}")
            import traceback; traceback.print_exc()

    def _populate_table(self):
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)

        for row_data in self.stats:
            row = self.table.rowCount()
            self.table.insertRow(row)

            self.table.setItem(row, 0, QTableWidgetItem(row_data.get("username", "")))
            self.table.setItem(row, 1, QTableWidgetItem(str(row_data.get("slip_count", 0))))

            slips_per_hour = row_data.get("slips_per_hour", 0.0)
            target = row_data.get("target_per_hour", 23)
            sph_item = QTableWidgetItem(f"{slips_per_hour:.2f}")
            sph_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if slips_per_hour >= target:
                sph_item.setForeground(QColor(COLORS["success"]))
            else:
                sph_item.setForeground(QColor(COLORS["warning"]))
            sph_item.setFont(QFont(FONTS["family"], FONTS["size_sm"], QFont.Bold))
            self.table.setItem(row, 2, sph_item)

            volume_item = QTableWidgetItem(f"€{float(row_data.get('cash_volume', 0)):,.2f}")
            volume_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row, 3, volume_item)

            dup_errors = row_data.get("duplicate_error_count", 0)
            dup_item = QTableWidgetItem(str(dup_errors))
            dup_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if dup_errors > 0:
                dup_item.setForeground(QColor(COLORS["error"]))
                dup_item.setFont(QFont(FONTS["family"], FONTS["size_sm"], QFont.Bold))
            self.table.setItem(row, 4, dup_item)

            transfer_errors = row_data.get("transfer_error_count", 0)
            transfer_item = QTableWidgetItem(str(transfer_errors))
            transfer_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if transfer_errors > 0:
                transfer_item.setForeground(QColor(COLORS["warning"]))
            self.table.setItem(row, 5, transfer_item)

        self.table.setSortingEnabled(True)
