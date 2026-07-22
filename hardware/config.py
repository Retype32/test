import os
from dotenv import load_dotenv

load_dotenv()

# Which driver to use: "tcp" | "serial" | "mock"
COUNTER_MODE: str = os.getenv("COUNTER_MODE", "none").lower()

# TCP / LAN settings (used when COUNTER_MODE=tcp)
COUNTER_HOST: str = os.getenv("COUNTER_HOST", "192.168.1.100")
COUNTER_PORT: int = int(os.getenv("COUNTER_PORT", "4000"))

# RS232 serial settings (used when COUNTER_MODE=serial)
COUNTER_COM_PORT: str = os.getenv("COUNTER_COM_PORT", "COM3")
COUNTER_BAUD_RATE: int = int(os.getenv("COUNTER_BAUD_RATE", "9600"))
