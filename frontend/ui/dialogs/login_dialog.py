from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFrame
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from ..theme import COLORS, FONTS
from ...services.api_client import api


class LoginDialog(QDialog):
    login_success = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Brink's Nexus — Login")
        self.setFixedSize(420, 540)
        self.setWindowFlags(Qt.Dialog)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header = QFrame()
        header.setFixedHeight(140)
        header.setStyleSheet(f"background-color: {COLORS['primary']};")
        h_layout = QVBoxLayout(header)
        h_layout.setAlignment(Qt.AlignCenter)
        h_layout.setSpacing(4)

        brand = QLabel("BRINK'S NEXUS")
        brand.setAlignment(Qt.AlignCenter)
        brand.setFont(QFont(FONTS["family"], FONTS["size_2xl"], QFont.Bold))
        brand.setStyleSheet(f"color: {COLORS['secondary']}; letter-spacing: 3px;")

        tagline = QLabel("Cash Operations Platform")
        tagline.setAlignment(Qt.AlignCenter)
        tagline.setStyleSheet(f"color: rgba(255,255,255,0.75); font-size: {FONTS['size_sm']}pt; letter-spacing: 1px;")

        sep = QFrame()
        sep.setFixedHeight(2)
        sep.setStyleSheet(f"background-color: {COLORS['secondary']};")

        h_layout.addWidget(brand)
        h_layout.addWidget(tagline)
        h_layout.addSpacing(8)
        h_layout.addWidget(sep)

        # Body
        body = QFrame()
        body.setStyleSheet(f"background-color: {COLORS['bg_white']};")
        b_layout = QVBoxLayout(body)
        b_layout.setContentsMargins(40, 20, 40, 20)
        b_layout.setSpacing(8)

        sign_in_lbl = QLabel("SIGN IN")
        sign_in_lbl.setFont(QFont(FONTS["family"], FONTS["size_md"], QFont.Bold))
        sign_in_lbl.setStyleSheet(f"color: {COLORS['text_secondary']}; letter-spacing: 1.5px;")

        hint = QLabel("Press Enter to continue")
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: {FONTS['size_xs']}pt;")

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Username")
        self.username_input.setFixedHeight(40)
        self.username_input.returnPressed.connect(lambda: self.password_input.setFocus())

        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Password")
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setFixedHeight(40)
        self.password_input.returnPressed.connect(self._on_login)

        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorMsg")
        self.error_label.setAlignment(Qt.AlignCenter)
        self.error_label.setWordWrap(True)

        self.login_btn = QPushButton("SIGN IN")
        self.login_btn.setFixedHeight(44)
        self.login_btn.setStyleSheet(
            f"background-color: {COLORS['primary']}; color: white; font-size: 11pt; font-weight: bold; border-radius: 3px;"
        )
        self.login_btn.clicked.connect(self._on_login)

        b_layout.addWidget(sign_in_lbl)
        b_layout.addWidget(hint)
        b_layout.addSpacing(4)
        b_layout.addWidget(QLabel("Username"))
        b_layout.addWidget(self.username_input)
        b_layout.addWidget(QLabel("Password"))
        b_layout.addWidget(self.password_input)
        b_layout.addSpacing(4)
        b_layout.addWidget(self.error_label)
        b_layout.addWidget(self.login_btn)
        b_layout.addStretch()

        # Footer
        footer = QFrame()
        footer.setFixedHeight(36)
        footer.setStyleSheet(f"background-color: {COLORS['bg_main']}; border-top: 1px solid {COLORS['border']};")
        f_layout = QHBoxLayout(footer)
        f_layout.setContentsMargins(16, 0, 16, 0)
        version_lbl = QLabel("v1.0.0  ·  Internal Use Only")
        version_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: {FONTS['size_xs']}pt;")
        f_layout.addStretch()
        f_layout.addWidget(version_lbl)

        layout.addWidget(header)
        layout.addWidget(body, 1)
        layout.addWidget(footer)

    def _on_login(self):
        username = self.username_input.text().strip()
        password = self.password_input.text()

        if not username or not password:
            self.error_label.setText("Username and password are required.")
            return

        self.error_label.setText("")
        result = api.login(username, password)

        if result.ok:
            api.set_token(result.data["access_token"])
            self.login_success.emit(result.data["user"])
            self.accept()
        else:
            self.error_label.setText(result.error or "Login failed. Check your credentials.")
            self.password_input.clear()
            self.password_input.setFocus()
