import sys
import time
import threading
import uvicorn
import httpx
from PySide6.QtWidgets import QApplication, QDialog
from PySide6.QtCore import Qt
from frontend.ui.theme import get_app_stylesheet
from frontend.ui.dialogs.login_dialog import LoginDialog
from frontend.ui.dialogs.catalog_select_dialog import CatalogSelectDialog
from frontend.ui.main_window import MainWindow
from frontend.services.api_client import api

BACKEND_URL = "http://127.0.0.1:8000"
_server: uvicorn.Server = None


def start_backend():
    global _server
    config = uvicorn.Config(
        "backend.main:app",
        host="127.0.0.1",
        port=8000,
        log_level="info",
    )
    _server = uvicorn.Server(config)
    thread = threading.Thread(target=_server.run, daemon=True)
    thread.start()

    for _ in range(30):
        try:
            httpx.get(BACKEND_URL, timeout=1)
            return True
        except Exception:
            time.sleep(1)
    return False


def stop_backend():
    if _server:
        _server.should_exit = True


def main():
    print("Starting backend...")
    if not start_backend():
        print("ERROR: Backend failed to start.")
        sys.exit(1)
    print("Backend ready.")

    app = QApplication(sys.argv)
    app.setApplicationName("Brink's Nexus")
    app.setOrganizationName("Brink's")
    app.setStyleSheet(get_app_stylesheet())
    app.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app.aboutToQuit.connect(stop_backend)

    dlg = LoginDialog()
    user_data: dict = {}

    def on_login(data: dict):
        nonlocal user_data
        user_data = data

    dlg.login_success.connect(on_login)

    if dlg.exec() != QDialog.Accepted:
        stop_backend()
        sys.exit(0)

    catalog_dlg = CatalogSelectDialog()
    if catalog_dlg.exec() != QDialog.Accepted:
        api.clear_token()
        stop_backend()
        sys.exit(0)

    user_data["catalog_code"] = catalog_dlg.selected_catalog_code
    user_data["catalog_name"] = catalog_dlg.selected_catalog_name

    window = MainWindow(user_data)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
