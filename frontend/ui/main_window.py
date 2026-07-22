from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QPushButton, QFrame, QStackedWidget, QSizePolicy, QStatusBar, QApplication, QDialog
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QFont
from .theme import COLORS, FONTS
from .dialogs.catalog_select_dialog import CatalogSelectDialog
from .dialogs.notification_detail_dialog import NotificationCenterDialog
from .pages.new_transaction_page import NewTransactionPage
from .pages.transaction_viewer_page import TransactionViewerPage
from .pages.reports_page import ReportsPage
from .pages.stats_page import StatsPage
from .pages.settings_page import SettingsPage
from ..services.api_client import api

NOTIFICATION_POLL_INTERVAL_MS = 30_000


class NotificationPollWorker(QThread):
    """Background thread that polls the unread-notification count off the UI thread."""
    count_received = Signal(int)
    error_occurred = Signal(str)

    def run(self):
        result = api.unread_notification_count()
        if result.ok:
            self.count_received.emit(result.data.get("count", 0))
        else:
            self.error_occurred.emit(result.error)


class NavButton(QPushButton):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName("NavButton")
        self.setCheckable(False)
        self.setFixedHeight(42)
        self.setProperty("active", False)
        self.setCursor(Qt.PointingHandCursor)

    def set_active(self, active: bool):
        self.setProperty("active", active)
        self.style().unpolish(self)
        self.style().polish(self)


class SidebarSection(QLabel):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName("SidebarSection")
        self.setFixedHeight(30)


class MainWindow(QMainWindow):
    def __init__(self, user_data: dict):
        super().__init__()
        self.user_data = user_data
        self._notification_worker = None
        self._setup_window()
        self._setup_ui()
        self._navigate_to(0)

        if self.notification_btn:
            self._notification_timer = QTimer(self)
            self._notification_timer.timeout.connect(self._poll_notifications)
            self._notification_timer.start(NOTIFICATION_POLL_INTERVAL_MS)
            self._poll_notifications()

    def _setup_window(self):
        self.setWindowTitle("BRINK'S NEXUS  |  Cash Operations Platform")
        self.setMinimumSize(900, 600)
        screen = QApplication.primaryScreen()
        if screen:
            available = screen.availableGeometry()
            w = min(1400, available.width())
            h = min(900, available.height())
            self.resize(w, h)
        else:
            self.resize(1400, 900)

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self._build_sidebar(root_layout)
        self._build_content(root_layout)
        self._build_statusbar()

    def _build_sidebar(self, parent_layout: QHBoxLayout):
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(220)
        s_layout = QVBoxLayout(sidebar)
        s_layout.setContentsMargins(0, 0, 0, 0)
        s_layout.setSpacing(0)

        # Logo block
        logo_frame = QFrame()
        logo_frame.setObjectName("SidebarLogo")
        logo_frame.setFixedHeight(80)
        ll = QVBoxLayout(logo_frame)
        ll.setContentsMargins(16, 12, 16, 12)
        ll.setSpacing(2)
        brand = QLabel("BRINK'S NEXUS")
        brand.setFont(QFont(FONTS["family"], FONTS["size_lg"], QFont.Bold))
        brand.setStyleSheet(f"color: {COLORS['secondary']}; letter-spacing: 2px; background: transparent;")
        sub = QLabel("Cash Operations Platform")
        sub.setStyleSheet(f"color: rgba(255,255,255,0.6); font-size: {FONTS['size_xs']}pt; background: transparent;")
        ll.addWidget(brand)
        ll.addWidget(sub)
        s_layout.addWidget(logo_frame)

        # User info strip
        user_frame = QFrame()
        user_frame.setFixedHeight(44)
        user_frame.setStyleSheet("background-color: rgba(0,0,0,0.2); border-bottom: 1px solid rgba(255,255,255,0.1);")
        ul = QHBoxLayout(user_frame)
        ul.setContentsMargins(16, 6, 16, 6)
        self.user_lbl = QLabel()
        self.user_lbl.setStyleSheet(f"color: rgba(255,255,255,0.75); font-size: {FONTS['size_xs']}pt;")
        self._update_identity_label()
        ul.addWidget(self.user_lbl)
        s_layout.addWidget(user_frame)

        # Switch catalog
        switch_btn = QPushButton("  ⇄  Switch Catalog")
        switch_btn.setObjectName("NavButton")
        switch_btn.setFixedHeight(38)
        switch_btn.setCursor(Qt.PointingHandCursor)
        switch_btn.setStyleSheet(f"color: {COLORS['secondary']}; text-align: left; padding: 8px 20px;")
        switch_btn.clicked.connect(self._switch_catalog)
        s_layout.addWidget(switch_btn)

        self._nav_buttons: list[tuple[NavButton, int]] = []

        role = self.user_data.get("role", "cashier")

        # Notifications (supervisor+)
        self.notification_btn = None
        if role in ("supervisor", "administrator"):
            self.notification_btn = QPushButton("  🔔  Notifications")
            self.notification_btn.setObjectName("NavButton")
            self.notification_btn.setFixedHeight(38)
            self.notification_btn.setCursor(Qt.PointingHandCursor)
            self.notification_btn.clicked.connect(self._open_notification_center)
            s_layout.addWidget(self.notification_btn)

        # ── CASHIER section ─────────────────
        s_layout.addWidget(SidebarSection("CASHIER"))
        btn_new = NavButton("  ▶  New Transaction")
        btn_new.clicked.connect(lambda: self._navigate_to(0))
        s_layout.addWidget(btn_new)
        self._nav_buttons.append((btn_new, 0))

        # ── SUPERVISOR section ───────────────
        if role in ("supervisor", "administrator"):
            s_layout.addWidget(SidebarSection("SUPERVISOR"))
            btn_txns = NavButton("  ≡  Transaction Viewer")
            btn_txns.clicked.connect(lambda: self._navigate_to(1))
            s_layout.addWidget(btn_txns)
            self._nav_buttons.append((btn_txns, 1))

            btn_reports = NavButton("  ⬡  Reports")
            btn_reports.clicked.connect(lambda: self._navigate_to(2))
            s_layout.addWidget(btn_reports)
            self._nav_buttons.append((btn_reports, 2))

            btn_stats = NavButton("  📊  Statistics")
            btn_stats.clicked.connect(lambda: self._navigate_to(4))
            s_layout.addWidget(btn_stats)
            self._nav_buttons.append((btn_stats, 4))

        # ── SETTINGS section ─────────────────
        if role == "administrator":
            s_layout.addWidget(SidebarSection("SETTINGS"))
            btn_settings = NavButton("  ⚙  Settings")
            btn_settings.clicked.connect(lambda: self._navigate_to(3))
            s_layout.addWidget(btn_settings)
            self._nav_buttons.append((btn_settings, 3))

        s_layout.addStretch()

        # Logout
        logout_btn = QPushButton("  ⏻  Sign Out")
        logout_btn.setObjectName("NavButton")
        logout_btn.setFixedHeight(42)
        logout_btn.setCursor(Qt.PointingHandCursor)
        logout_btn.setStyleSheet("color: rgba(255,100,100,0.85); text-align: left; padding: 10px 20px;")
        logout_btn.clicked.connect(self._logout)
        s_layout.addWidget(logout_btn)

        parent_layout.addWidget(sidebar)

    def _build_content(self, parent_layout: QHBoxLayout):
        content = QFrame()
        content.setObjectName("ContentArea")
        content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        c_layout = QVBoxLayout(content)
        c_layout.setContentsMargins(0, 0, 0, 0)
        c_layout.setSpacing(0)

        self.stack = QStackedWidget()
        self._populate_stack()

        c_layout.addWidget(self.stack)
        parent_layout.addWidget(content)

    def _populate_stack(self):
        self.page_new_txn = NewTransactionPage(self.user_data)
        self.stack.addWidget(self.page_new_txn)   # index 0

        self.page_txn_viewer = TransactionViewerPage(self.user_data)
        self.stack.addWidget(self.page_txn_viewer)  # index 1

        self.page_reports = ReportsPage(self.user_data)
        self.stack.addWidget(self.page_reports)     # index 2

        self.page_settings = SettingsPage(self.user_data)
        self.stack.addWidget(self.page_settings)    # index 3

        self.page_stats = StatsPage(self.user_data)
        self.stack.addWidget(self.page_stats)       # index 4

    def _rebuild_stack(self):
        """Tear down and recreate every page, discarding any catalog-scoped state
        (loaded customers, cached transaction rows, etc.) after a catalog switch."""
        for page in (self.page_new_txn, self.page_txn_viewer, self.page_reports, self.page_settings, self.page_stats):
            self.stack.removeWidget(page)
            page.deleteLater()
        self._populate_stack()

    def _build_statusbar(self):
        sb = QStatusBar()
        self.setStatusBar(sb)
        self._update_statusbar_message()

    def _update_identity_label(self):
        role_str = self.user_data.get("role", "cashier").upper()
        catalog_name = self.user_data.get("catalog_name", "")
        self.user_lbl.setText(f"  {self.user_data.get('username', 'User')}  ·  {role_str}  ·  {catalog_name}")

    def _update_statusbar_message(self):
        role = self.user_data.get("role", "cashier")
        catalog_name = self.user_data.get("catalog_name", "—")
        self.statusBar().showMessage(
            f"  Brink's Nexus v1.0.0   |   Logged in as: {self.user_data.get('username', '')} ({role.upper()})"
            f"   |   Catalog: {catalog_name}   |   Ready"
        )

    def _navigate_to(self, index: int):
        self.stack.setCurrentIndex(index)
        for btn, idx in self._nav_buttons:
            btn.set_active(idx == index)

        # Refresh data when switching to viewer/reports/stats
        if index == 1:
            self.page_txn_viewer.load_transactions()
        elif index == 2:
            self.page_reports.refresh_filters()
        elif index == 4:
            self.page_stats.refresh()

    def _switch_catalog(self):
        dlg = CatalogSelectDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return

        self.user_data["catalog_code"] = dlg.selected_catalog_code
        self.user_data["catalog_name"] = dlg.selected_catalog_name
        self._update_identity_label()
        self._update_statusbar_message()
        self._rebuild_stack()
        self._navigate_to(0)
        if self.notification_btn:
            self._poll_notifications()

    def _poll_notifications(self):
        if self._notification_worker and self._notification_worker.isRunning():
            return
        self._notification_worker = NotificationPollWorker()
        self._notification_worker.count_received.connect(self._on_notification_count)
        self._notification_worker.start()

    def _on_notification_count(self, count: int):
        if not self.notification_btn:
            return
        if count > 0:
            self.notification_btn.setText(f"  🔔  Notifications ({count})")
            self.notification_btn.setStyleSheet(f"color: {COLORS['secondary']}; font-weight: bold; text-align: left; padding: 8px 20px;")
        else:
            self.notification_btn.setText("  🔔  Notifications")
            self.notification_btn.setStyleSheet("")

    def _open_notification_center(self):
        dlg = NotificationCenterDialog(self)
        dlg.exec()
        self._poll_notifications()

    def _logout(self):
        if self.notification_btn:
            self._notification_timer.stop()
        api.clear_token()
        api.clear_catalog()
        self.close()
