from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox, QInputDialog
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from ..theme import COLORS, FONTS
from ...services.api_client import api


class EODReopenDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Reopen a Closed Business Day")
        self.setMinimumSize(560, 480)
        self.setModal(True)
        self._setup_ui()
        self._load_closures()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header.setFixedHeight(56)
        header.setStyleSheet(f"background-color: {COLORS['primary']};")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 0, 20, 0)
        title = QLabel("REOPEN A CLOSED BUSINESS DAY")
        title.setFont(QFont(FONTS["family"], FONTS["size_lg"], QFont.Bold))
        title.setStyleSheet(f"color: {COLORS['secondary']}; letter-spacing: 1px; background: transparent;")
        hl.addWidget(title)
        layout.addWidget(header)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Business Date", "Status", "Closed By / At", ""])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setColumnWidth(0, 130)
        self.table.setColumnWidth(1, 100)
        self.table.setColumnWidth(2, 200)
        layout.addWidget(self.table, 1)

        footer = QFrame()
        footer.setStyleSheet(f"border-top: 1px solid {COLORS['border']}; background: {COLORS['bg_main']};")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(20, 12, 20, 12)
        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(38)
        close_btn.setFixedWidth(100)
        close_btn.clicked.connect(self.accept)
        fl.addStretch()
        fl.addWidget(close_btn)
        layout.addWidget(footer)

    def _load_closures(self):
        result = api.list_eod_closures()
        if not result.ok:
            QMessageBox.warning(self, "Error", result.error or "Could not load closures.")
            return
        self._populate_table(result.data or [])

    def _populate_table(self, closures: list[dict]):
        self.table.setRowCount(0)
        for closure in closures:
            row = self.table.rowCount()
            self.table.insertRow(row)

            self.table.setItem(row, 0, QTableWidgetItem(str(closure.get("business_date", ""))))

            status = closure.get("status", "")
            status_item = QTableWidgetItem(status.upper())
            if status == "closed":
                status_item.setForeground(Qt.GlobalColor.darkRed)
            self.table.setItem(row, 1, status_item)

            closed_at = str(closure.get("closed_at", ""))[:19]
            closed_by = str(closure.get("closed_by_user_id", ""))[:8]
            self.table.setItem(row, 2, QTableWidgetItem(f"{closed_by}  ·  {closed_at}"))

            if status == "closed":
                reopen_btn = QPushButton("Reopen")
                reopen_btn.setCursor(Qt.PointingHandCursor)
                reopen_btn.clicked.connect(
                    lambda _checked=False, bd=closure.get("business_date"): self._on_reopen(bd)
                )
                self.table.setCellWidget(row, 3, reopen_btn)

    def _on_reopen(self, business_date: str):
        reason, ok = QInputDialog.getText(
            self, "Reopen Business Day", f"Reason for reopening {business_date}:"
        )
        if not ok or not reason.strip():
            return

        result = api.reopen_eod(str(business_date), reason.strip())
        if not result.ok:
            QMessageBox.warning(self, "Error", result.error or "Could not reopen this day.")
            return

        self._load_closures()
