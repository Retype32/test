"""
BPS C1 simulator — exercises the full driver stack with no machine attached.

The exact print template of a C1 depends on its firmware and on how the
operator configured the report, so this generates several plausible layouts
rather than one. If the parser handles all of them, an unfamiliar real
printout is far more likely to work on the first try.

    python -m hardware.simulate                     # show every layout
    python -m hardware.simulate --layout compact    # one layout
    python -m hardware.simulate --check             # parse them all, report
    python -m hardware.simulate --port COM5         # emit to a serial port

--port is for testing against a real driver over a virtual null-modem pair
(com0com on Windows): point the simulator at one end and COUNTER_COM_PORT at
the other, and Nexus behaves exactly as it will with a real machine.
"""

from __future__ import annotations

import argparse
import sys
import time
from decimal import Decimal

DEFAULT_BATCH: dict[str, int] = {
    "EUR 500": 2,
    "EUR 200": 5,
    "EUR 100": 11,
    "EUR 50": 12,
    "EUR 20": 30,
    "EUR 10": 7,
    "EUR 5": 3,
}
FACE = {
    "EUR 500": 500, "EUR 200": 200, "EUR 100": 100, "EUR 50": 50,
    "EUR 20": 20, "EUR 10": 10, "EUR 5": 5,
}


def _money(amount: Decimal, european: bool) -> str:
    s = f"{amount:,.2f}"
    if european:
        s = s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return s


def build_report(batch: dict[str, int], layout: str = "standard", rejects: int = 2) -> str:
    """Render a batch as one of several plausible C1 print templates."""
    rows = [(name, qty, Decimal(FACE[name]) * qty) for name, qty in batch.items() if qty]
    total_qty = sum(q for _, q, _ in rows)
    total_val = sum((v for _, _, v in rows), Decimal(0))
    european = layout in {"european", "compact"}
    m = lambda v: _money(v, european)  # noqa: E731

    if layout == "standard":
        lines = [
            "", "        BPS C1  BATCH REPORT", "",
            "Currency        EUR",
            "Fitness Mode    ATM", "",
            "-" * 34,
            "Denom          Pcs        Amount",
            "-" * 34,
        ]
        lines += [f"{n:<12} {q:>5} {m(v):>13}" for n, q, v in rows]
        lines += [
            "-" * 34,
            f"{'SubTotal':<12} {total_qty:>5} {m(total_val):>13}",
            f"{'Total':<12} {total_qty:>5} {m(total_val):>13}",
        ]
        if rejects:
            lines += ["", f"REJECT         {rejects}"]

    elif layout == "compact":
        lines = ["Currency EUR", "Fitness Mode MIX", "Denom Pcs Amount"]
        lines += [f"{n} {q} {m(v)}" for n, q, v in rows]
        lines += [f"Total {total_qty} {m(total_val)}"]
        if rejects:
            lines += [f"REJECT {rejects}"]

    elif layout == "european":
        lines = [
            "Currency     : EUR",
            "Fitness Mode : FIT",
            "=" * 30,
            "Denom        Pcs      Amount",
        ]
        lines += [f"{n.replace('EUR ', 'EUR'):<10} {q:>6} {m(v):>12}" for n, q, v in rows]
        lines += ["=" * 30, f"{'Total':<10} {total_qty:>6} {m(total_val):>12}"]
        if rejects:
            lines += [f"REJECT {rejects}"]

    elif layout == "symbol":
        # Some templates print the currency symbol instead of the ISO code.
        lines = ["Currency  EUR", "Fitness Mode  ATM", "Denom   Pcs   Amount"]
        lines += [f"€{FACE[n]:<8} {q:>5} {m(v):>12}" for n, q, v in rows]
        lines += ["-" * 30, f"Total     {total_qty}   {m(total_val)}"]
        if rejects:
            lines += [f"REJECT  {rejects}"]

    elif layout == "bare":
        # Minimal template: face value only, no currency on the row.
        lines = ["Currency  EUR", "Denom   Pcs   Amount"]
        lines += [f"{FACE[n]:<6} {q:>5} {m(v):>12}" for n, q, v in rows]
        lines += [f"Total   {total_qty}   {m(total_val)}"]

    elif layout == "quantity_only":
        # Some templates omit the amount column entirely.
        lines = ["Currency  EUR", "Denom      Pcs"]
        lines += [f"{n:<10} {q:>5}" for n, q, _ in rows]
        lines += [f"{'Total':<10} {total_qty:>5}"]

    else:
        raise ValueError(f"Unknown layout {layout!r}. Choose from: {', '.join(LAYOUTS)}")

    # Real printers end with CRLF and a form feed.
    return "\r\n".join(lines) + "\r\n\x0c"


LAYOUTS = ("standard", "compact", "european", "symbol", "bare", "quantity_only")


def check_all() -> int:
    from .report_parser import ReportParseError, parse_report
    from .report_profile import load_profile

    profile = load_profile("bps_c1_eur")
    expected_qty = sum(DEFAULT_BATCH.values())
    failures = 0

    for layout in LAYOUTS:
        text = build_report(DEFAULT_BATCH, layout)
        try:
            report = parse_report(text, profile)
        except ReportParseError as exc:
            print(f"  {layout:<14} FAILED TO PARSE — {str(exc).splitlines()[0]}")
            failures += 1
            continue

        problems = list(report.warnings)
        if report.counted_quantity != expected_qty:
            problems.append(f"counted {report.counted_quantity}, expected {expected_qty}")
        if problems:
            print(f"  {layout:<14} PARSED WITH PROBLEMS")
            for p in problems:
                print(f"                   - {p}")
            failures += 1
        else:
            print(f"  {layout:<14} ok — {report.counted_quantity} notes, "
                  f"{report.counted_value} {report.currency or ''}".rstrip())

    print()
    if failures:
        print(f"{failures} of {len(LAYOUTS)} layouts need profile work.")
    else:
        print(f"All {len(LAYOUTS)} layouts parsed cleanly with totals agreeing.")
    return 1 if failures else 0


def emit_to_port(port: str, baud: int, layout: str, encoding: str, repeat: int) -> int:
    import serial

    try:
        sp = serial.Serial(port=port, baudrate=baud, timeout=1)
    except serial.SerialException as exc:
        print(f"Cannot open {port}: {exc}", file=sys.stderr)
        return 1

    try:
        for i in range(repeat):
            text = build_report(DEFAULT_BATCH, layout)
            sp.write(text.encode(encoding, errors="replace"))
            sp.flush()
            print(f"Sent {layout} report ({len(text)} chars) to {port}.")
            if i + 1 < repeat:
                time.sleep(5)
    finally:
        sp.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Simulate a BPS C1 batch report.")
    ap.add_argument("--layout", choices=LAYOUTS, help="print template to render")
    ap.add_argument("--check", action="store_true", help="parse every layout and report")
    ap.add_argument("--port", help="serial port to emit the report on")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--encoding", default="cp437")
    ap.add_argument("--repeat", type=int, default=1, help="number of batches to send")
    args = ap.parse_args(argv)

    if args.check:
        print("Parsing every simulated layout with profile 'bps_c1_eur':\n")
        return check_all()
    if args.port:
        return emit_to_port(args.port, args.baud, args.layout or "standard", args.encoding, args.repeat)

    for layout in ([args.layout] if args.layout else LAYOUTS):
        print(f"───── layout: {layout} " + "─" * 30)
        print(build_report(DEFAULT_BATCH, layout).replace("\x0c", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
