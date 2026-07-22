from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QColor
from ..theme import COLORS, FONTS
from ...services.api_client import api
from .duplicate_review_dialog import DuplicateReviewDialog


class NotificationCenterDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.notifications: list[dict] = []
        self.setWindowTitle("Notifications")
        self.setMinimumSize(700, 480)
        self.setModal(True)
        self._setup_ui()
        self._load()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header.setFixedHeight(56)
        header.setStyleSheet(f"background-color: {COLORS['primary']};")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 0, 20, 0)
        title = QLabel("NOTIFICATIONS")
        title.setFont(QFont(FONTS["family"], FONTS["size_lg"], QFont.Bold))
        title.setStyleSheet(f"color: {COLORS['secondary']}; letter-spacing: 1px; background: transparent;")
        hl.addWidget(title)
        layout.addWidget(header)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["When", "Severity", "Message", "", ""])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setColumnWidth(0, 140)
        self.table.setColumnWidth(1, 80)
        self.table.setColumnWidth(2, 280)
        self.table.setColumnWidth(3, 90)
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

    def _load(self):
        result = api.list_notifications(status="open")
        if not result.ok:
            QMessageBox.warning(self, "Error", result.error or "Could not load notifications.")
            return
        self.notifications = result.data or []
        self._populate_table()

    def _populate_table(self):
        self.table.setRowCount(0)
        for notification in self.notifications:
            row = self.table.rowCount()
            self.table.insertRow(row)

            when = str(notification.get("created_at", ""))[:19]
            self.table.setItem(row, 0, QTableWidgetItem(when))

            severity = notification.get("severity", "info")
            sev_item = QTableWidgetItem(severity.upper())
            color_map = {"error": COLORS["error"], "warning": COLORS["warning"], "info": COLORS["text_muted"]}
            sev_item.setForeground(QColor(color_map.get(severity, COLORS["text_muted"])))
            sev_item.setFont(QFont(FONTS["family"], FONTS["size_sm"], QFont.Bold))
            self.table.setItem(row, 1, sev_item)

            self.table.setItem(row, 2, QTableWidgetItem(notification.get("message", "")))

            event_type = notification.get("event_type", "")
            if event_type == "DUPLICATE_TRANSACTION":
                review_btn = QPushButton("Review")
                review_btn.setCursor(Qt.PointingHandCursor)
                review_btn.clicked.connect(
                    lambda _checked=False, n=notification: self._on_review_duplicate(n)
                )
                self.table.setCellWidget(row, 3, review_btn)
            else:
                dismiss_btn = QPushButton("Dismiss")
                dismiss_btn.setCursor(Qt.PointingHandCursor)
                dismiss_btn.clicked.connect(
                    lambda _checked=False, n=notification: self._on_dismiss(n)
                )
                self.table.setCellWidget(row, 3, dismiss_btn)

    def _on_dismiss(self, notification: dict):
        result = api.resolve_notification(notification["id"], "resolved")
        if not result.ok:
            QMessageBox.warning(self, "Error", result.error or "Could not resolve notification.")
            return
        self._load()

    def _on_review_duplicate(self, notification: dict):
        related_txn_id = notification.get("related_transaction_id")
        flags_result = api.list_duplicates(status="pending")
        if not flags_result.ok:
            QMessageBox.warning(self, "Error", flags_result.error or "Could not load duplicate details.")
            return

        matching = [f for f in (flags_result.data or []) if f.get("transaction_id") == related_txn_id]
        if not matching:
            QMessageBox.information(self, "Already Reviewed", "This duplicate has already been reviewed.")
            self._load()
            return

        dlg = DuplicateReviewDialog(matching[0], parent=self)
        if dlg.exec() == QDialog.Accepted and dlg.reviewed:
            self._load()
