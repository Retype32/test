from decimal import Decimal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFrame, QComboBox, QGridLayout,
    QStackedWidget, QScrollArea, QMessageBox, QDialog
)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from ..theme import COLORS, FONTS
from ...services.api_client import api
from ..dialogs.transaction_confirm_dialog import TransactionConfirmDialog
from ..dialogs.transaction_result_dialog import TransactionResultDialog
from ..dialogs.bag_entry_dialog import BagEntryDialog

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
from hardware import get_counter


class CounterWorker(QThread):
    """Background thread that connects to the cash counter and waits for a result."""
    count_received = Signal(dict)   # emits {denomination: count} on success
    error_occurred = Signal(str)    # emits error message on failure

    def run(self):
        counter = None
        try:
            counter = get_counter()
            counter.connect()
            result = counter.wait_for_count_result()
            self.count_received.emit(result.denominations)
        except Exception as exc:
            self.error_occurred.emit(str(exc))
        finally:
            if counter:
                try:
                    counter.disconnect()
                except Exception:
                    pass


DENOMINATIONS = ["€500", "€200", "€100", "€50", "€20", "€10", "€5", "Coins"]
DENOM_VALUES = {
    "€500": Decimal("500"), "€200": Decimal("200"), "€100": Decimal("100"),
    "€50": Decimal("50"),  "€20": Decimal("20"),   "€10": Decimal("10"),
    "€5": Decimal("5"),    "Coins": Decimal("1"),
}


def _section_header(text: str) -> QFrame:
    frame = QFrame()
    frame.setObjectName("CardHeader")
    layout = QHBoxLayout(frame)
    layout.setContentsMargins(14, 8, 14, 8)
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color: white; font-weight: bold; font-size: {FONTS['size_sm']}pt; background: transparent;")
    layout.addWidget(lbl)
    return frame


def _card() -> QFrame:
    f = QFrame()
    f.setObjectName("Card")
    return f


class NewTransactionPage(QWidget):
    def __init__(self, user_data: dict, parent=None):
        super().__init__(parent)
        self.user_data = user_data
        self.selected_customer: dict | None = None
        self.selected_location: dict | None = None
        self.bag_number: str = ""
        self.expected_total: Decimal = Decimal("0")
        self._counter_worker: CounterWorker | None = None
        self._setup_ui()

    def _setup_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QFrame()
        header.setObjectName("PageHeader")
        h_layout = QVBoxLayout(header)
        h_layout.setContentsMargins(24, 16, 24, 16)
        title = QLabel("New Transaction")
        title.setObjectName("PageTitle")
        subtitle = QLabel("Cashier  ▶  New Transaction")
        subtitle.setObjectName("PageSubtitle")
        h_layout.addWidget(title)
        h_layout.addWidget(subtitle)
        outer.addWidget(header)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)

        def _scrolled(widget):
            s = QScrollArea()
            s.setWidget(widget)
            s.setWidgetResizable(True)
            s.setFrameShape(QFrame.NoFrame)
            return s

        self.step1 = self._build_step1()
        self.step2 = self._build_step2()
        self.stack.addWidget(_scrolled(self.step1))
        self.stack.addWidget(_scrolled(self.step2))

    # ── STEP 1: Customer Selection ──────────────────────────────────────────
    def _build_step1(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        step_lbl = QLabel("STEP 1 OF 2  —  Customer & Location Selection")
        step_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: {FONTS['size_xs']}pt; font-weight: bold; letter-spacing: 1px;")
        layout.addWidget(step_lbl)

        card = _card()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)
        card_layout.addWidget(_section_header("CUSTOMER VALIDATION"))

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(20, 20, 20, 20)
        body_layout.setSpacing(14)

        cid_layout = QHBoxLayout()
        cid_lbl = QLabel("Customer ID")
        cid_lbl.setObjectName("FieldLabel")
        cid_lbl.setFixedWidth(120)
        self.customer_id_input = QLineEdit()
        self.customer_id_input.setPlaceholderText("Enter customer ID…")
        self.customer_id_input.setFixedHeight(36)
        self.customer_id_input.setFixedWidth(220)
        self.validate_btn = QPushButton("VALIDATE")
        self.validate_btn.setFixedHeight(36)
        self.validate_btn.setFixedWidth(110)
        self.validate_btn.clicked.connect(self._validate_customer)
        self.customer_id_input.returnPressed.connect(self._validate_customer)
        self.cid_error = QLabel("")
        self.cid_error.setObjectName("ErrorMsg")

        cid_layout.addWidget(cid_lbl)
        cid_layout.addWidget(self.customer_id_input)
        cid_layout.addWidget(self.validate_btn)
        cid_layout.addWidget(self.cid_error)
        cid_layout.addStretch()
        body_layout.addLayout(cid_layout)

        cname_layout = QHBoxLayout()
        cname_lbl = QLabel("Customer Name")
        cname_lbl.setObjectName("FieldLabel")
        cname_lbl.setFixedWidth(120)
        self.customer_name_display = QLabel("—")
        self.customer_name_display.setObjectName("ValueLabel")
        self.customer_name_display.setMinimumWidth(220)
        cname_layout.addWidget(cname_lbl)
        cname_layout.addWidget(self.customer_name_display)
        cname_layout.addStretch()
        body_layout.addLayout(cname_layout)

        sep = QFrame()
        sep.setObjectName("Separator")
        body_layout.addWidget(sep)

        loc_label = QLabel("LOCATION SELECTION")
        loc_label.setStyleSheet(f"font-size: {FONTS['size_xs']}pt; font-weight: bold; color: {COLORS['primary']}; letter-spacing: 0.8px;")
        body_layout.addWidget(loc_label)

        loc_id_layout = QHBoxLayout()
        loc_id_lbl = QLabel("Location ID")
        loc_id_lbl.setObjectName("FieldLabel")
        loc_id_lbl.setFixedWidth(120)
        self.location_id_input = QLineEdit()
        self.location_id_input.setPlaceholderText("Enter location ID…")
        self.location_id_input.setFixedHeight(36)
        self.location_id_input.setFixedWidth(220)
        self.location_id_input.setEnabled(False)
        or_lbl = QLabel("  OR  ")
        or_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; font-weight: bold;")
        self.location_combo = QComboBox()
        self.location_combo.setFixedHeight(36)
        self.location_combo.setFixedWidth(260)
        self.location_combo.setEnabled(False)
        self.location_combo.currentIndexChanged.connect(self._on_location_combo_changed)

        loc_id_layout.addWidget(loc_id_lbl)
        loc_id_layout.addWidget(self.location_id_input)
        loc_id_layout.addWidget(or_lbl)
        loc_id_layout.addWidget(self.location_combo)
        loc_id_layout.addStretch()
        body_layout.addLayout(loc_id_layout)

        self.loc_error = QLabel("")
        self.loc_error.setObjectName("ErrorMsg")
        body_layout.addWidget(self.loc_error)

        card_layout.addWidget(body)
        layout.addWidget(card)

        btn_row = QHBoxLayout()
        self.apply_btn = QPushButton("APPLY SELECTION  ▶")
        self.apply_btn.setFixedHeight(42)
        self.apply_btn.setFixedWidth(200)
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self._apply_selection)
        btn_row.addStretch()
        btn_row.addWidget(self.apply_btn)
        layout.addLayout(btn_row)
        layout.addStretch()

        return page

    # ── STEP 2: Denomination Entry ───────────────────────────────────────────
    def _build_step2(self) -> QWidget:
        page = QWidget()
        outer = QHBoxLayout(page)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(16)

        left = QVBoxLayout()
        left.setSpacing(12)

        step_lbl = QLabel("STEP 2 OF 2  —  Denomination Entry")
        step_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: {FONTS['size_xs']}pt; font-weight: bold; letter-spacing: 1px;")
        left.addWidget(step_lbl)

        # Customer / Location / Bag / Expected Total display
        sel_card = _card()
        sel_layout = QVBoxLayout(sel_card)
        sel_layout.setContentsMargins(0, 0, 0, 0)
        sel_layout.setSpacing(0)
        sel_layout.addWidget(_section_header("TRANSACTION DETAILS"))
        sel_body = QWidget()
        sel_body_layout = QHBoxLayout(sel_body)
        sel_body_layout.setContentsMargins(16, 12, 16, 12)
        sel_body_layout.setSpacing(24)

        def _info_pair(label, value_widget):
            col = QVBoxLayout()
            col.setSpacing(4)
            lbl = QLabel(label)
            lbl.setObjectName("FieldLabel")
            col.addWidget(lbl)
            col.addWidget(value_widget)
            return col

        self.disp_customer = QLabel("—")
        self.disp_customer.setObjectName("ValueLabel")
        self.disp_customer.setMinimumWidth(160)

        self.disp_location = QLabel("—")
        self.disp_location.setObjectName("ValueLabel")
        self.disp_location.setMinimumWidth(160)

        self.disp_bag = QLabel("—")
        self.disp_bag.setObjectName("ValueLabel")
        self.disp_bag.setMinimumWidth(130)

        self.disp_expected = QLabel("—")
        self.disp_expected.setObjectName("ValueLabel")
        self.disp_expected.setMinimumWidth(110)
        self.disp_expected.setStyleSheet(
            f"font-weight: bold; color: {COLORS['primary']}; background-color: #EDF2F7; padding: 5px 10px; border-radius: 2px;"
        )

        sel_body_layout.addLayout(_info_pair("CUSTOMER", self.disp_customer))
        sel_body_layout.addLayout(_info_pair("LOCATION", self.disp_location))
        sel_body_layout.addLayout(_info_pair("BAG NUMBER", self.disp_bag))
        sel_body_layout.addLayout(_info_pair("EXPECTED TOTAL", self.disp_expected))
        sel_body_layout.addStretch()
        change_btn = QPushButton("◀  Change")
        change_btn.setObjectName("SecondaryBtn")
        change_btn.setFixedHeight(32)
        change_btn.clicked.connect(self._go_to_step1)
        sel_body_layout.addWidget(change_btn)
        sel_layout.addWidget(sel_body)
        left.addWidget(sel_card)

        # Denomination grid
        denom_card = _card()
        denom_layout = QVBoxLayout(denom_card)
        denom_layout.setContentsMargins(0, 0, 0, 0)
        denom_layout.addWidget(_section_header("DENOMINATION ENTRY"))

        machine_row = QHBoxLayout()
        machine_row.setContentsMargins(16, 10, 16, 4)
        self.machine_status = QLabel("Machine: connecting…")
        self.machine_status.setStyleSheet(
            f"color: {COLORS['text_muted']}; font-size: {FONTS['size_xs']}pt;"
        )
        machine_row.addWidget(self.machine_status)
        machine_row.addStretch()
        denom_layout.addLayout(machine_row)

        denom_body = QWidget()
        grid = QGridLayout(denom_body)
        grid.setContentsMargins(16, 12, 16, 16)
        grid.setSpacing(8)

        for col, text in enumerate(["DENOMINATION", "COUNT", "VALUE"]):
            h = QLabel(text)
            h.setObjectName("FieldLabel")
            h.setAlignment(Qt.AlignCenter if col > 0 else Qt.AlignLeft)
            grid.addWidget(h, 0, col)

        self.denom_inputs: dict[str, QLineEdit] = {}
        self.denom_value_labels: dict[str, QLabel] = {}

        for row, denom in enumerate(DENOMINATIONS, 1):
            denom_lbl = QLabel(denom)
            denom_lbl.setFont(QFont(FONTS["family"], FONTS["size_md"], QFont.Bold))
            denom_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            denom_lbl.setMinimumWidth(80)

            count_input = QLineEdit("0")
            count_input.setFixedWidth(100)
            count_input.setFixedHeight(34)
            count_input.setAlignment(Qt.AlignRight)
            count_input.textChanged.connect(self._update_totals)

            value_lbl = QLabel("€0.00")
            value_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            value_lbl.setMinimumWidth(110)
            value_lbl.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: {FONTS['size_sm']}pt;")

            grid.addWidget(denom_lbl, row, 0)
            grid.addWidget(count_input, row, 1)
            grid.addWidget(value_lbl, row, 2)

            self.denom_inputs[denom] = count_input
            self.denom_value_labels[denom] = value_lbl

        denom_layout.addWidget(denom_body)
        left.addWidget(denom_card)
        left.addStretch()

        outer.addLayout(left, 3)

        # Right: live total + confirm
        right = QVBoxLayout()
        right.setSpacing(12)

        total_card = _card()
        total_layout = QVBoxLayout(total_card)
        total_layout.setContentsMargins(0, 0, 0, 0)
        total_layout.addWidget(_section_header("LIVE TOTAL"))

        total_body = QWidget()
        tb_layout = QVBoxLayout(total_body)
        tb_layout.setContentsMargins(16, 16, 16, 16)
        tb_layout.setSpacing(12)
        tb_layout.setAlignment(Qt.AlignTop)

        total_hdr = QLabel("TOTAL CASH VALUE")
        total_hdr.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: {FONTS['size_xs']}pt; font-weight: bold; letter-spacing: 1px;")
        tb_layout.addWidget(total_hdr)

        self.total_display = QLabel("€0.00")
        self.total_display.setObjectName("TotalBox")
        self.total_display.setAlignment(Qt.AlignCenter)
        self.total_display.setMinimumHeight(70)
        tb_layout.addWidget(self.total_display)

        sep2 = QFrame()
        sep2.setObjectName("Separator")
        tb_layout.addWidget(sep2)

        self.total_breakdown_labels: dict[str, QLabel] = {}
        for denom in DENOMINATIONS:
            row_layout = QHBoxLayout()
            row_layout.setSpacing(4)
            dl = QLabel(denom)
            dl.setFixedWidth(60)
            dl.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: {FONTS['size_sm']}pt;")
            vl = QLabel("€0.00")
            vl.setAlignment(Qt.AlignRight)
            vl.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: {FONTS['size_sm']}pt;")
            row_layout.addWidget(dl)
            row_layout.addStretch()
            row_layout.addWidget(vl)
            tb_layout.addLayout(row_layout)
            self.total_breakdown_labels[denom] = vl

        total_layout.addWidget(total_body)
        right.addWidget(total_card)

        self.confirm_btn = QPushButton("CONFIRM TRANSACTION")
        self.confirm_btn.setObjectName("ConfirmBtn")
        self.confirm_btn.setFixedHeight(48)
        self.confirm_btn.clicked.connect(self._confirm_transaction)
        right.addWidget(self.confirm_btn)

        reset_btn = QPushButton("RESET FORM")
        reset_btn.setObjectName("SecondaryBtn")
        reset_btn.setFixedHeight(36)
        reset_btn.clicked.connect(self._reset_form)
        right.addWidget(reset_btn)
        right.addStretch()

        outer.addLayout(right, 1)
        return page

    # ── Handlers ───────────────────────────────────────────────────────────
    def _validate_customer(self):
        cid = self.customer_id_input.text().strip()
        if not cid:
            self.cid_error.setText("Customer ID is required.")
            return

        self.validate_btn.setEnabled(False)
        self.validate_btn.setText("…")
        self.cid_error.setText("")

        result = api.validate_customer(cid)
        self.validate_btn.setEnabled(True)
        self.validate_btn.setText("VALIDATE")

        if not result.ok:
            self.cid_error.setText(result.error or "Customer not found.")
            self.customer_name_display.setText("—")
            self.selected_customer = None
            self.location_id_input.setEnabled(False)
            self.location_combo.setEnabled(False)
            self.apply_btn.setEnabled(False)
            return

        data = result.data
        self.selected_customer = data
        self.customer_name_display.setText(f"{data['customer_name']}  ({data['customer_id']})")
        self.customer_name_display.setStyleSheet(
            f"color: {COLORS['success']}; font-weight: bold; background-color: {COLORS['success_bg']}; padding: 5px 10px; border-radius: 2px;"
        )

        self.location_combo.clear()
        self.location_combo.addItem("— Select location —", None)
        for loc in data.get("locations", []):
            self.location_combo.addItem(f"{loc['location_id']}  —  {loc['location_name']}", loc)

        self.location_id_input.setEnabled(True)
        self.location_combo.setEnabled(True)

    def _on_location_combo_changed(self, idx: int):
        loc = self.location_combo.currentData()
        if loc:
            self.location_id_input.setText(loc["location_id"])
            self.selected_location = loc
            self.apply_btn.setEnabled(True)
        else:
            self.selected_location = None
            self.apply_btn.setEnabled(False)

    def _apply_selection(self):
        loc_id = self.location_id_input.text().strip()
        if not loc_id:
            self.loc_error.setText("Please select or enter a location.")
            return

        locs = self.selected_customer.get("locations", [])
        matched = next((l for l in locs if l["location_id"] == loc_id), None)
        if not matched:
            self.loc_error.setText(f"Location '{loc_id}' not found for this customer.")
            return

        self.selected_location = matched
        self.loc_error.setText("")

        # Open bag + expected total dialog
        dlg = BagEntryDialog(
            customer_name=self.selected_customer["customer_name"],
            location_name=matched["location_name"],
            parent=self,
        )
        if dlg.exec() != QDialog.Accepted:
            return

        self.bag_number = dlg.bag_number
        self.expected_total = dlg.expected_total

        # Update step 2 displays
        self.disp_customer.setText(
            f"{self.selected_customer['customer_name']}  ({self.selected_customer['customer_id']})"
        )
        self.disp_location.setText(f"{matched['location_name']}  ({matched['location_id']})")
        self.disp_bag.setText(self.bag_number)
        self.disp_expected.setText(f"€{self.expected_total:,.2f}")

        self._reset_denomination_inputs()
        self.stack.setCurrentIndex(1)
        self._start_machine_count()

    def _go_to_step1(self):
        self.stack.setCurrentIndex(0)

    def _update_totals(self):
        grand_total = Decimal("0")
        for denom in DENOMINATIONS:
            text = self.denom_inputs[denom].text().strip()
            try:
                count = int(text) if text else 0
                if count < 0:
                    count = 0
            except ValueError:
                count = 0
            value = DENOM_VALUES[denom] * count
            grand_total += value
            self.denom_value_labels[denom].setText(f"€{value:,.2f}")
            self.total_breakdown_labels[denom].setText(f"€{value:,.2f}")

        self.total_display.setText(f"€{grand_total:,.2f}")

    def _confirm_transaction(self):
        if not self.selected_customer or not self.selected_location:
            QMessageBox.warning(self, "Error", "No customer/location selected.")
            return

        denoms = []
        grand_total = Decimal("0")
        for denom in DENOMINATIONS:
            text = self.denom_inputs[denom].text().strip()
            try:
                count = int(text) if text else 0
            except ValueError:
                count = 0
            if count > 0:
                value = DENOM_VALUES[denom] * count
                grand_total += value
                denoms.append({"denomination": denom, "count": count, "value": float(value)})

        if grand_total == 0:
            QMessageBox.warning(self, "Validation Error", "Total cash value cannot be zero.")
            return

        dlg = TransactionConfirmDialog(
            customer=self.selected_customer,
            location=self.selected_location,
            bag_number=self.bag_number,
            denominations=denoms,
            total=grand_total,
            expected_total=self.expected_total if self.expected_total else None,
            parent=self,
        )
        if dlg.exec():
            self._submit_transaction(denoms, grand_total)

    def _submit_transaction(self, denoms: list, total: Decimal):
        payload = {
            "customer_id": self.selected_customer["customer_id"],
            "location_id": self.selected_location["location_id"],
            "bag_number": self.bag_number,
            "total_value": str(total),
            "expected_total": str(self.expected_total) if self.expected_total else None,
            "denominations": denoms,
        }
        try:
            result = api.create_transaction(payload)
        except Exception as exc:
            print(f"[TX] create_transaction raised: {exc}")
            QMessageBox.critical(self, "Transaction Failed", str(exc))
            return

        print(f"[TX] status={result.status_code} ok={result.ok} error={result.error!r}")
        if not result.ok:
            QMessageBox.critical(
                self, "Transaction Failed",
                f"HTTP {result.status_code}\n{result.error}"
            )
            return

        try:
            dlg = TransactionResultDialog(result.data, expected_total=self.expected_total, parent=self)
            dlg.exec()
        except Exception as exc:
            print(f"[TX] result dialog error: {exc}")
            QMessageBox.information(self, "Transaction Saved", "Transaction was saved successfully.")
        self._full_reset()

    def _start_machine_count(self):
        if self._counter_worker and self._counter_worker.isRunning():
            return

        self.machine_status.setText("Machine: connecting…")
        self.machine_status.setStyleSheet(
            f"color: {COLORS['primary']}; font-size: {FONTS['size_xs']}pt; font-weight: bold;"
        )

        self._counter_worker = CounterWorker()
        self._counter_worker.count_received.connect(self._apply_machine_result)
        self._counter_worker.error_occurred.connect(self._on_machine_error)
        self._counter_worker.finished.connect(lambda: None)
        self._counter_worker.start()
        self.machine_status.setText("Machine: counting…")

    def _apply_machine_result(self, denominations: dict):
        for denom, count in denominations.items():
            if denom in self.denom_inputs:
                self.denom_inputs[denom].setText(str(count) if count else "0")
        self._update_totals()
        self.machine_status.setText("Machine: result received")
        self.machine_status.setStyleSheet(
            f"color: {COLORS['success']}; font-size: {FONTS['size_xs']}pt; font-weight: bold;"
        )

    def _on_machine_error(self, message: str):
        self.machine_status.setText(f"Machine unavailable — enter counts manually  ({message})")
        self.machine_status.setStyleSheet(
            f"color: {COLORS['error']}; font-size: {FONTS['size_xs']}pt; font-weight: bold;"
        )

    def _reset_denomination_inputs(self):
        for inp in self.denom_inputs.values():
            inp.setText("0")
        self._update_totals()

    def _reset_form(self):
        self._reset_denomination_inputs()

    def _full_reset(self):
        self.selected_customer = None
        self.selected_location = None
        self.bag_number = ""
        self.expected_total = Decimal("0")
        self.customer_id_input.clear()
        self.customer_name_display.setText("—")
        self.customer_name_display.setStyleSheet("")
        self.location_id_input.clear()
        self.location_id_input.setEnabled(False)
        self.location_combo.clear()
        self.location_combo.setEnabled(False)
        self.apply_btn.setEnabled(False)
        self.cid_error.setText("")
        self.loc_error.setText("")
        self._reset_denomination_inputs()
        self.stack.setCurrentIndex(0)
