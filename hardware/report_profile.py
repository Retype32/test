"""
Device profile for report-parsing cash counters.

A profile describes how one physical machine prints its end-of-batch report:
the serial line settings, the labels that introduce each section, and the
mapping from the machine's own denomination names to Nexus denomination keys.

Profiles live in hardware/profiles/*.json so a machine can be retuned on site
without touching code — the report layout differs between firmware versions,
currencies and operator-configured print templates.

Denomination matching is deliberately forgiving. A machine may print the same
denomination as "EUR 50", "EUR50", "50 EUR", "€50", "EUR 50.00" or plain "50"
depending on its print template, so names are compared after normalisation
rather than literally.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

PROFILE_DIR = Path(__file__).parent / "profiles"

_CURRENCY_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "¥": "JPY"}
_TRAILING_ZERO_DECIMALS = re.compile(r"[.,]-?0*$")
_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def normalize(text: str) -> str:
    """Reduce a printed denomination label to a comparable key.

    "EUR 50" / "eur50" / "€50" / "EUR 50.00" / "EUR 50,-"  ->  "EUR50"
    """
    s = text.upper().strip()
    for symbol, code in _CURRENCY_SYMBOLS.items():
        s = s.replace(symbol, code)
    s = _TRAILING_ZERO_DECIMALS.sub("", s)
    return _NON_ALNUM.sub("", s)


@dataclass(frozen=True)
class Denom:
    device: str          # as the machine prints it, e.g. "EUR 50"
    nexus: str           # Nexus denomination key, e.g. "€50"
    value: Decimal       # face value, used to cross-check printed amounts
    isacode: str | None = None       # for cross-referencing against ISA config
    aliases: tuple[str, ...] = ()    # extra spellings seen on real printouts

    def match_keys(self) -> set[str]:
        """Every normalised spelling that should resolve to this denomination."""
        keys = {normalize(self.device), normalize(self.nexus)}
        keys.update(normalize(a) for a in self.aliases)

        # Currency letters from the device name, e.g. "EUR 50" -> "EUR".
        letters = _NON_ALNUM.sub("", "".join(c for c in self.device.upper() if not c.isdigit()))
        # Plain face value: 500 -> "500", 0.50 -> "0.5". Never .normalize() here,
        # which renders 500 as "5E+2" and would alias EUR 500 onto EUR 5.
        amount = format(self.value, "f")
        if "." in amount:
            amount = amount.rstrip("0").rstrip(".")
        if letters:
            keys.add(f"{letters}{amount}")
            keys.add(f"{amount}{letters}")
        keys.add(amount)  # bare "50"; dropped later if ambiguous
        return {k for k in keys if k}


@dataclass(frozen=True)
class Comms:
    port: str = "COM1"
    baudrate: int = 115200
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    rtscts: bool = False
    dsrdtr: bool = False


@dataclass
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
    max_label_tokens: int = 3            # how many leading tokens may form a name

    _index: dict[str, Denom] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        claims: dict[str, list[Denom]] = {}
        for denom in self.denoms:
            for key in denom.match_keys():
                claims.setdefault(key, []).append(denom)
        # A spelling claimed by two denominations (e.g. bare "50" when both
        # EUR 50 and USD 50 are configured) is ambiguous and must not match.
        self._index = {k: v[0] for k, v in claims.items() if len(v) == 1}

    def match_denomination(self, tokens: list[str]) -> tuple[Denom, list[str]] | None:
        """Resolve the leading tokens of a line to a denomination.

        Returns the denomination and the tokens after it, or None. Candidate
        lengths are tried shortest-first so that "EUR 50 0 0.00" (a zero row)
        resolves to EUR 50 rather than colliding with EUR 500.
        """
        for count in range(1, min(self.max_label_tokens, len(tokens)) + 1):
            denom = self._index.get(normalize(" ".join(tokens[:count])))
            if denom is not None:
                return denom, tokens[count:]
        return None

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


def load_all_profiles() -> list[DeviceProfile]:
    return [load_profile(p) for p in sorted(PROFILE_DIR.glob("*.json"))]


def profile_from_dict(raw: dict) -> DeviceProfile:
    comms = Comms(**raw.get("comms", {}))
    denoms = tuple(
        Denom(
            device=d["device"],
            nexus=d["nexus"],
            value=Decimal(str(d["value"])),
            isacode=d.get("isacode"),
            aliases=tuple(d.get("aliases", ())),
        )
        for d in raw["denoms"]
    )
    skip = {"comms", "denoms", "_index"}
    known = {f for f in DeviceProfile.__dataclass_fields__ if f not in skip}
    extras = {k: v for k, v in raw.items() if k in known}
    extras.setdefault("labels", {})
    return DeviceProfile(comms=comms, denoms=denoms, **extras)
