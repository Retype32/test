"""
Single launcher: starts the backend server, waits for it to be ready, then opens the frontend.
Run:  python start.py
"""
import subprocess
import sys
import time
import httpx
import os

BACKEND_URL = "http://127.0.0.1:8000"
MAX_WAIT = 15


def wait_for_backend():
    print("Starting backend...")
    for _ in range(MAX_WAIT):
        try:
            httpx.get(BACKEND_URL, timeout=1)
            return True
        except Exception:
            time.sleep(1)
    return False


if __name__ == "__main__":
    backend = subprocess.Popen(
        [sys.executable, "run_backend.py"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )

    if not wait_for_backend():
        print("ERROR: Backend did not start in time.")
        backend.terminate()
        sys.exit(1)

    print("Backend ready. Launching frontend...")

    try:
        subprocess.run([sys.executable, "main.py"])
    finally:
        backend.terminate()
        backend.wait()
