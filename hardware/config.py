import os
from dotenv import load_dotenv

load_dotenv()

# COM port for the G+D BPS C1's serial report output.
#
# Defaults to COM1, which is both the port the standalone C1 Check.py
# defaults to and the one the (now removed) device profile used to supply
# when this setting was left blank. Leaving it unset therefore keeps
# working exactly as it did before, rather than silently disabling the
# counter on a machine that was never asked to name its port.
#
# Set COUNTER_COM_PORT=none to deliberately run with no counter at all --
# the wizard's machine-read then fails immediately with a clear message and
# cashiers enter counts manually.
COUNTER_COM_PORT: str = os.getenv("COUNTER_COM_PORT", "").strip() or "COM1"
if COUNTER_COM_PORT.lower() in {"none", "off", "disabled"}:
    COUNTER_COM_PORT = ""

# 115200 8N1, no handshake -- the only line settings the C1 uses.
COUNTER_BAUD_RATE: int = int(os.getenv("COUNTER_BAUD_RATE", "115200") or 115200)
