"""
Device profile for report-parsing cash counters.

A profile describes how one physical machine prints its end-of-batch report:
the serial line settings, the labels that introduce each section, and the
mapping from the machine's own denomination names to Nexus denomination keys.

Profiles live in hardware/profiles/*.json so a machine can be retuned on site
without touching code — the report layout differs between firmware versions,
currencies and operator-configured print templates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

PROFILE_DIR = Path(__file__).parent / "profiles"


@dataclass(frozen=True)
class Denom:
    device: str          # exactly as the machine prints it, e.g. "EUR 50"
    nexus: str           # Nexus denomination key, e.g. "€50"
    value: Decimal       # face value, used to cross-check printed amounts
    isacode: str | None = None   # kept for cross-referencing against ISA config


@dataclass(frozen=True)
class Comms:
    port: str = "COM1"
    baudrate: int = 115200
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    rtscts: bool = False
    dsrdtr: bool = False


@dataclass(frozen=True)
class DeviceProfile:
    name: str
    comms: Comms
    denoms: tuple[Denom, ...]
    labels: dict[str, str]
    encoding: str = "cp437"
    separator: str = "------"
    reject_keyword: str = "REJECT"
    decimal_separator: str = "auto"      # "." | "," | "auto"
    idle_timeout_seconds: float = 2.0    # quiet time that marks end of a report
    min_report_bytes: int = 32           # ignore stray keep-alive bytes

    def denom_by_device_name(self, text: str) -> Denom | None:
        """Longest-prefix match, so "EUR 5" never shadows "EUR 500"."""
        upper = text.upper()
        best: Denom | None = None
        for d in self.denoms:
            key = d.device.upper()
            if upper.startswith(key) and (best is None or len(key) > len(best.device)):
                best = d
        return best

    def label(self, key: str) -> str:
        return self.labels.get(key, "")


def load_profile(source: str | Path) -> DeviceProfile:
    """Load a profile by name (hardware/profiles/<name>.json) or by path."""
    path = Path(source)
    if not path.suffix:
        path = PROFILE_DIR / f"{path.name}.json"
    if not path.is_absolute() and not path.exists():
        candidate = PROFILE_DIR / path.name
        if candidate.exists():
            path = candidate
    if not path.exists():
        available = ", ".join(sorted(p.stem for p in PROFILE_DIR.glob("*.json"))) or "none"
        raise FileNotFoundError(
            f"Device profile {source!r} not found at {path}. Available profiles: {available}"
        )
    return profile_from_dict(json.loads(path.read_text(encoding="utf-8")))


def profile_from_dict(raw: dict) -> DeviceProfile:
    comms = Comms(**raw.get("comms", {}))
    denoms = tuple(
        Denom(
            device=d["device"],
            nexus=d["nexus"],
            value=Decimal(str(d["value"])),
            isacode=d.get("isacode"),
        )
        for d in raw["denoms"]
    )
    known = {f for f in DeviceProfile.__dataclass_fields__ if f not in {"comms", "denoms"}}
    extras = {k: v for k, v in raw.items() if k in known}
    extras.setdefault("labels", {})
    return DeviceProfile(comms=comms, denoms=denoms, **extras)
