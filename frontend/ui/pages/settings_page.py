from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QLineEdit, QComboBox, QScrollArea, QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from ..theme import COLORS, FONTS
from ...services.api_client import api


class SettingsPage(QWidget):
    def __init__(self, user_data: dict, parent=None):
        super().__init__(parent)
        self.user_data = user_data
        self._setup_ui()
        self._load_staff()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Page header
        header = QFrame()
        header.setObjectName("PageHeader")
        hl = QVBoxLayout(header)
        hl.setContentsMargins(24, 16, 24, 16)
        title = QLabel("Settings")
        title.setObjectName("PageTitle")
        sub = QLabel("Administrator  ▶  Settings")
        sub.setObjectName("PageSubtitle")
        hl.addWidget(title)
        hl.addWidget(sub)
        layout.addWidget(header)

        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(24, 24, 24, 24)
        cl.setSpacing(16)

        # User management card
        user_card = QFrame()
        user_card.setObjectName("Card")
        uc_layout = QVBoxLayout(user_card)
        uc_layout.setContentsMargins(0, 0, 0, 0)
        uc_layout.setSpacing(0)

        hdr_frame = QFrame()
        hdr_frame.setObjectName("CardHeader")
        hf = QHBoxLayout(hdr_frame)
        hf.setContentsMargins(14, 8, 14, 8)
        hf.addWidget(QLabel("USER MANAGEMENT"))
        uc_layout.addWidget(hdr_frame)

        ub = QWidget()
        ub_layout = QVBoxLayout(ub)
        ub_layout.setContentsMargins(20, 16, 20, 16)
        ub_layout.setSpacing(12)

        form_row = QHBoxLayout()
        form_row.setSpacing(12)

        def _lbl(t):
            l = QLabel(t)
            l.setObjectName("FieldLabel")
            l.setFixedWidth(100)
            return l

        self.new_username = QLineEdit()
        self.new_username.setPlaceholderText("Username")
        self.new_username.setFixedHeight(34)
        self.new_username.setFixedWidth(160)

        self.new_password = QLineEdit()
        self.new_password.setPlaceholderText("Password")
        self.new_password.setEchoMode(QLineEdit.Password)
        self.new_password.setFixedHeight(34)
        self.new_password.setFixedWidth(160)

        self.new_role = QComboBox()
        self.new_role.addItems(["cashier", "supervisor", "administrator"])
        self.new_role.setFixedHeight(34)
        self.new_role.setFixedWidth(140)

        create_btn = QPushButton("CREATE USER")
        create_btn.setFixedHeight(34)
        create_btn.clicked.connect(self._create_user)

        self.user_msg = QLabel("")
        self.user_msg.setObjectName("ErrorMsg")

        for w, lbl_text in [(self.new_username, "Username"), (self.new_password, "Password"), (self.new_role, "Role")]:
            col = QVBoxLayout()
            col.setSpacing(4)
            col.addWidget(_lbl(lbl_text))
            col.addWidget(w)
            form_row.addLayout(col)

        form_row.addSpacing(8)
        form_row.addWidget(create_btn, 0, Qt.AlignBottom)
        form_row.addWidget(self.user_msg, 0, Qt.AlignBottom)
        form_row.addStretch()
        ub_layout.addLayout(form_row)
        uc_layout.addWidget(ub)

        # Staff list
        self.staff_table = QTableWidget(0, 4)
        self.staff_table.setHorizontalHeaderLabels(["Username", "Role", "Status", ""])
        self.staff_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.staff_table.horizontalHeader().setStretchLastSection(True)
        self.staff_table.verticalHeader().setVisible(False)
        self.staff_table.setAlternatingRowColors(True)
        self.staff_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.staff_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.staff_table.setColumnWidth(0, 180)
        self.staff_table.setColumnWidth(1, 140)
        self.staff_table.setColumnWidth(2, 100)
        self.staff_table.setFixedHeight(260)
        uc_layout.addWidget(self.staff_table)

        cl.addWidget(user_card)

        # System Configuration (placeholder)
        sys_card = QFrame()
        sys_card.setObjectName("Card")
        sc_layout = QVBoxLayout(sys_card)
        sc_layout.setContentsMargins(0, 0, 0, 0)
        sc_layout.setSpacing(0)

        sc_hdr = QFrame()
        sc_hdr.setObjectName("CardHeader")
        sch = QHBoxLayout(sc_hdr)
        sch.setContentsMargins(14, 8, 14, 8)
        sch.addWidget(QLabel("SYSTEM CONFIGURATION"))
        sc_layout.addWidget(sc_hdr)

        sc_body = QWidget()
        sc_bl = QVBoxLayout(sc_body)
        sc_bl.setContentsMargins(20, 16, 20, 16)

        placeholder_items = [
            ("Database Connection", "PostgreSQL  ·  localhost:5432/brinksdb"),
            ("API Endpoint", "http://127.0.0.1:8000"),
            ("C1 Integration", "Not configured  (Future Phase)"),
            ("Anomaly Detection", "Not configured  (Future Phase)"),
        ]
        for label, value in placeholder_items:
            row = QHBoxLayout()
            l = QLabel(label)
            l.setObjectName("FieldLabel")
            l.setFixedWidth(200)
            v = QLabel(value)
            v.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: {FONTS['size_sm']}pt;")
            row.addWidget(l)
            row.addWidget(v)
            row.addStretch()
            sc_bl.addLayout(row)

        sc_layout.addWidget(sc_body)
        cl.addWidget(sys_card)

        # Future Integrations (placeholder)
        future_card = QFrame()
        future_card.setObjectName("Card")
        fc_layout = QVBoxLayout(future_card)
        fc_layout.setContentsMargins(0, 0, 0, 0)
        fc_layout.setSpacing(0)

        fc_hdr = QFrame()
        fc_hdr.setObjectName("CardHeader")
        fch = QHBoxLayout(fc_hdr)
        fch.setContentsMargins(14, 8, 14, 8)
        fch.addWidget(QLabel("FUTURE INTEGRATIONS"))
        fc_layout.addWidget(fc_hdr)

        fc_body = QWidget()
        fc_bl = QVBoxLayout(fc_body)
        fc_bl.setContentsMargins(20, 16, 20, 16)
        fc_bl.setSpacing(6)

        future_items = [
            "Direct C1 Machine Integration",
            "Supervisor Approval Workflows",
            "Spectre Anomaly Detection Module",
            "Duplicate Transaction Detection",
            "AI-Assisted Investigation Tools",
            "Productivity Reporting Engine",
            "Reconciliation Engine",
        ]
        for item in future_items:
            row = QHBoxLayout()
            badge = QLabel("PLANNED")
            badge.setFixedWidth(70)
            badge.setAlignment(Qt.AlignCenter)
            badge.setStyleSheet(
                f"background-color: {COLORS['bg_main']}; color: {COLORS['text_muted']}; "
                f"font-size: {FONTS['size_xs']}pt; font-weight: bold; "
                f"border: 1px solid {COLORS['border']}; border-radius: 2px; padding: 2px;"
            )
            lbl = QLabel(item)
            lbl.setStyleSheet(f"color: {COLORS['text_secondary']};")
            row.addWidget(badge)
            row.addWidget(lbl)
            row.addStretch()
            fc_bl.addLayout(row)

        fc_layout.addWidget(fc_body)
        cl.addWidget(future_card)
        cl.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        layout.addWidget(scroll, 1)

    def _create_user(self):
        username = self.new_username.text().strip()
        password = self.new_password.text()
        role = self.new_role.currentText()

        if not username or not password:
            self.user_msg.setText("Username and password required.")
            return

        result = api.create_user({"username": username, "password": password, "role": role})
        if result.ok:
            self.user_msg.setStyleSheet(f"color: {COLORS['success']}; font-size: {FONTS['size_xs']}pt;")
            self.user_msg.setText(f"User '{username}' created.")
            self.new_username.clear()
            self.new_password.clear()
            self._load_staff()
        else:
            self.user_msg.setStyleSheet(f"color: {COLORS['error']}; font-size: {FONTS['size_xs']}pt;")
            self.user_msg.setText(result.error)

    def _load_staff(self):
        result = api.list_users()
        if not result.ok:
            return
        self._populate_staff_table(result.data or [])

    def _populate_staff_table(self, users: list[dict]):
        self.staff_table.setRowCount(0)
        for user in users:
            row = self.staff_table.rowCount()
            self.staff_table.insertRow(row)

            username_item = QTableWidgetItem(user.get("username", ""))
            username_item.setFlags(username_item.flags() & ~Qt.ItemIsEditable)
            self.staff_table.setItem(row, 0, username_item)

            role_item = QTableWidgetItem(user.get("role", ""))
            role_item.setFlags(role_item.flags() & ~Qt.ItemIsEditable)
            self.staff_table.setItem(row, 1, role_item)

            is_active = user.get("is_active", True)
            status_item = QTableWidgetItem("Active" if is_active else "Disabled")
            status_item.setForeground(QColor(COLORS["success"] if is_active else COLORS["error"]))
            status_item.setFlags(status_item.flags() & ~Qt.ItemIsEditable)
            self.staff_table.setItem(row, 2, status_item)

            is_self = user.get("username") == self.user_data.get("username")
            toggle_btn = QPushButton("Deactivate" if is_active else "Activate")
            toggle_btn.setCursor(Qt.PointingHandCursor)
            if is_self and is_active:
                toggle_btn.setEnabled(False)
                toggle_btn.setToolTip("You cannot deactivate your own account.")
            else:
                toggle_btn.clicked.connect(
                    lambda _checked=False, uid=user.get("id"), active=is_active: self._toggle_active(uid, active)
                )
            self.staff_table.setCellWidget(row, 3, toggle_btn)

    def _toggle_active(self, user_id: str, currently_active: bool):
        result = api.set_user_active(user_id, not currently_active)
        if not result.ok:
            QMessageBox.warning(self, "Error", f"Could not update user: {result.error}")
            return
        self._load_staff()
