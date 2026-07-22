import os
from datetime import datetime
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QLineEdit, QDateEdit, QFileDialog, QMessageBox,
    QScrollArea
)
from PySide6.QtCore import QDate
from ..theme import COLORS, FONTS
from ...services.api_client import api
from reports.report_engine import build_transactions_excel, build_transactions_csv


class ReportsPage(QWidget):
    def __init__(self, user_data: dict, parent=None):
        super().__init__(parent)
        self.user_data = user_data
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
        title = QLabel("Reports")
        title.setObjectName("PageTitle")
        sub = QLabel("Supervisor  ▶  Reports")
        sub.setObjectName("PageSubtitle")
        hl.addWidget(title)
        hl.addWidget(sub)
        layout.addWidget(header)

        # Content
        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(24, 24, 24, 24)
        cl.setSpacing(16)

        # Filters card
        card = QFrame()
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)

        hdr_frame = QFrame()
        hdr_frame.setObjectName("CardHeader")
        hf = QHBoxLayout(hdr_frame)
        hf.setContentsMargins(14, 8, 14, 8)
        hf.addWidget(QLabel("REPORT FILTERS  —  Transaction Report"))
        card_layout.addWidget(hdr_frame)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(20, 16, 20, 16)
        body_layout.setSpacing(12)

        # Date range row
        row1 = QHBoxLayout()
        row1.setSpacing(12)
        row1.addWidget(self._field_label("Date From"))
        self.date_from = QDateEdit(QDate.currentDate().addDays(-30))
        self.date_from.setCalendarPopup(True)
        self.date_from.setFixedHeight(34)
        self.date_from.setDisplayFormat("yyyy-MM-dd")
        row1.addWidget(self.date_from)
        row1.addSpacing(16)
        row1.addWidget(self._field_label("Date To"))
        self.date_to = QDateEdit(QDate.currentDate())
        self.date_to.setCalendarPopup(True)
        self.date_to.setFixedHeight(34)
        self.date_to.setDisplayFormat("yyyy-MM-dd")
        row1.addWidget(self.date_to)
        row1.addStretch()
        body_layout.addLayout(row1)

        # Customer / Location row
        row2 = QHBoxLayout()
        row2.setSpacing(12)
        row2.addWidget(self._field_label("Customer ID"))
        self.filter_customer = QLineEdit()
        self.filter_customer.setPlaceholderText("Leave blank for all")
        self.filter_customer.setFixedHeight(34)
        self.filter_customer.setFixedWidth(200)
        row2.addWidget(self.filter_customer)
        row2.addSpacing(16)
        row2.addWidget(self._field_label("Location ID"))
        self.filter_location = QLineEdit()
        self.filter_location.setPlaceholderText("Leave blank for all")
        self.filter_location.setFixedHeight(34)
        self.filter_location.setFixedWidth(200)
        row2.addWidget(self.filter_location)
        row2.addStretch()
        body_layout.addLayout(row2)

        card_layout.addWidget(body)
        cl.addWidget(card)

        # Export card
        export_card = QFrame()
        export_card.setObjectName("Card")
        ec_layout = QVBoxLayout(export_card)
        ec_layout.setContentsMargins(0, 0, 0, 0)
        ec_layout.setSpacing(0)

        ex_hdr = QFrame()
        ex_hdr.setObjectName("CardHeader")
        exh = QHBoxLayout(ex_hdr)
        exh.setContentsMargins(14, 8, 14, 8)
        exh.addWidget(QLabel("EXPORT"))
        ec_layout.addWidget(ex_hdr)

        ex_body = QWidget()
        ex_bl = QHBoxLayout(ex_body)
        ex_bl.setContentsMargins(20, 16, 20, 16)
        ex_bl.setSpacing(12)

        excel_btn = QPushButton("  EXPORT TO EXCEL  (.xlsx)")
        excel_btn.setObjectName("GoldBtn")
        excel_btn.setFixedHeight(42)
        excel_btn.clicked.connect(self._export_excel)

        csv_btn = QPushButton("  EXPORT TO CSV")
        csv_btn.setObjectName("SecondaryBtn")
        csv_btn.setFixedHeight(42)
        csv_btn.clicked.connect(self._export_csv)

        self.export_status = QLabel("")
        self.export_status.setStyleSheet(f"color: {COLORS['success']}; font-size: {FONTS['size_sm']}pt;")

        ex_bl.addWidget(excel_btn)
        ex_bl.addWidget(csv_btn)
        ex_bl.addStretch()
        ex_bl.addWidget(self.export_status)
        ec_layout.addWidget(ex_body)
        cl.addWidget(export_card)

        cl.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        layout.addWidget(scroll, 1)

    def _field_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("FieldLabel")
        lbl.setFixedWidth(100)
        return lbl

    def refresh_filters(self):
        pass

    def _build_params(self) -> dict:
        params = {}
        params["date_from"] = self.date_from.date().toString("yyyy-MM-dd") + "T00:00:00"
        params["date_to"] = self.date_to.date().toString("yyyy-MM-dd") + "T23:59:59"
        if self.filter_customer.text().strip():
            params["customer_id"] = self.filter_customer.text().strip()
        if self.filter_location.text().strip():
            params["location_id"] = self.filter_location.text().strip()
        return params

    def _fetch_transactions(self) -> list[dict] | None:
        result = api.list_transactions(self._build_params())
        if not result.ok:
            QMessageBox.warning(self, "Error", result.error)
            return None
        return result.data or []

    def _enrich_transactions(self, transactions: list[dict]) -> list[dict]:
        enriched = []
        for t in transactions:
            enriched.append({
                **t,
                "username": t.get("user_id", "")[:8],
                "customer_name": t.get("customer_id", ""),
                "location_name": t.get("location_id", ""),
                "created_at": datetime.fromisoformat(t["created_at"]) if isinstance(t.get("created_at"), str) else t.get("created_at"),
            })
        return enriched

    def _export_excel(self):
        transactions = self._fetch_transactions()
        if transactions is None:
            return
        if not transactions:
            QMessageBox.information(self, "No Data", "No transactions match the selected filters.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Save Excel Report", f"transactions_{QDate.currentDate().toString('yyyyMMdd')}.xlsx",
            "Excel Files (*.xlsx)"
        )
        if not path:
            return
        try:
            enriched = self._enrich_transactions(transactions)
            build_transactions_excel(enriched, path)
            self.export_status.setText(f"✓  Saved: {os.path.basename(path)}")
            QMessageBox.information(self, "Export Complete", f"Report saved to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", str(e))

    def _export_csv(self):
        transactions = self._fetch_transactions()
        if transactions is None:
            return
        if not transactions:
            QMessageBox.information(self, "No Data", "No transactions match the selected filters.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Save CSV Report", f"transactions_{QDate.currentDate().toString('yyyyMMdd')}.csv",
            "CSV Files (*.csv)"
        )
        if not path:
            return
        try:
            enriched = self._enrich_transactions(transactions)
            build_transactions_csv(enriched, path)
            self.export_status.setText(f"✓  Saved: {os.path.basename(path)}")
            QMessageBox.information(self, "Export Complete", f"CSV saved to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", str(e))
