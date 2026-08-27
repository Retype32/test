import os
from dotenv import load_dotenv

load_dotenv()

# COM port for the G+D BPS C1's serial report output (e.g. "COM3"). Blank
# disables the counter entirely -- the wizard's count-read request fails
# immediately with a clear message and cashiers enter counts manually.
COUNTER_COM_PORT: str = os.getenv("COUNTER_COM_PORT", "").strip()

# 115200 8N1, no handshake -- the only line settings the C1 uses.
COUNTER_BAUD_RATE: int = int(os.getenv("COUNTER_BAUD_RATE", "115200") or 115200)
