import os
from dotenv import load_dotenv

load_dotenv()

# Which driver to use: "c1_report" | "mock" | "none"
COUNTER_MODE: str = os.getenv("COUNTER_MODE", "none").lower()

# Device profile describing the machine's report layout.
# Either a name under hardware/profiles/ or a path to a .json file.
COUNTER_PROFILE: str = os.getenv("COUNTER_PROFILE", "bps_c1_eur")

# RS232 settings. Blank falls back to the values in the device profile.
COUNTER_COM_PORT: str = os.getenv("COUNTER_COM_PORT", "")
COUNTER_BAUD_RATE: int = int(os.getenv("COUNTER_BAUD_RATE", "0") or 0)

# When true, a report whose own totals disagree with its denomination lines is
# rejected instead of accepted with a warning. Recommended in production.
COUNTER_STRICT: bool = os.getenv("COUNTER_STRICT", "true").lower() in {"1", "true", "yes"}

# If the configured profile cannot parse a report, try the other installed
# profiles and accept one only if its totals agree with its own rows.
COUNTER_AUTODETECT: bool = os.getenv("COUNTER_AUTODETECT", "true").lower() in {"1", "true", "yes"}

# Used when COUNTER_MODE=isa_log: directory where ISA's device plugin writes
# its log files (the folder containing DEVICE_LOG.LOG or similar).
ISA_LOG_DIR: str = os.getenv("ISA_LOG_DIR", "")
ISA_LOG_POLL_SECONDS: float = float(os.getenv("ISA_LOG_POLL_SECONDS", "0.25"))
