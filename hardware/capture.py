"""
Raw serial capture tool — run this against a real machine before trusting any
profile.

Everything the machine sends is written verbatim to a .bin file (and, decoded,
to a .txt file) so the exact report layout can be inspected and the device
profile tuned to match. It makes no assumptions and sends nothing.

    python -m hardware.capture --list-ports        # which COM port?
    python -m hardware.capture --port COM3 --baud 115200

Then run a small test batch on the machine and end it so it prints. Press
Ctrl+C to stop. Attach the resulting .txt to any profile change.

To check a capture against a profile without any hardware attached:

    python -m hardware.capture --parse reports/captures/c1-20260727-143012.txt
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "reports" / "captures"


def list_ports() -> int:
    """Show the serial ports Windows can see, so the right one can be picked."""
    from serial.tools import list_ports as lp

    ports = sorted(lp.comports(), key=lambda p: p.device)
    if not ports:
        print("No serial ports found on this PC.")
        print()
        print("This machine has no RS232 port and no USB-to-serial adapter")
        print("attached. You need one of:")
        print("  - a built-in RS232 port (older desktops / industrial PCs), or")
        print("  - a USB-to-serial adapter (FTDI or Prolific chipset), plus")
        print("  - a null-modem cable from the machine's printer port.")
        return 1

    print(f"{len(ports)} serial port(s) found:\n")
    for p in ports:
        print(f"  {p.device:<8} {p.description}")
        if p.hwid and p.hwid != "n/a":
            print(f"           {p.hwid}")
    print("\nSet COUNTER_COM_PORT in .env to the port the machine is wired to.")
    return 0


def _hexdump(data: bytes, width: int = 16) -> str:
    lines = []
    for offset in range(0, len(data), width):
        chunk = data[offset : offset + width]
        hexpart = " ".join(f"{b:02x}" for b in chunk).ljust(width * 3 - 1)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{offset:08x}  {hexpart}  |{text}|")
    return "\n".join(lines)


def capture(port: str, baud: int, encoding: str, outdir: Path) -> int:
    import serial

    outdir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bin_path = outdir / f"c1-{stamp}.bin"
    txt_path = outdir / f"c1-{stamp}.txt"

    try:
        sp = serial.Serial(port=port, baudrate=baud, timeout=0.5)
    except serial.SerialException as exc:
        print(f"Cannot open {port}: {exc}", file=sys.stderr)
        return 1

    print(f"Capturing from {port} at {baud} baud. Run a batch on the machine.")
    print(f"Writing to {bin_path}\nPress Ctrl+C to stop.\n")

    buf = bytearray()
    try:
        while True:
            chunk = sp.read(4096)
            if chunk:
                buf.extend(chunk)
                sys.stdout.write(chunk.decode(encoding, errors="replace"))
                sys.stdout.flush()
    except KeyboardInterrupt:
        print("\n\nStopped.")
    finally:
        sp.close()

    if not buf:
        print("Nothing was received. Check the cable, the COM port, the baud "
              "rate, and that the machine's report output is set to serial.")
        return 1

    bin_path.write_bytes(bytes(buf))
    txt_path.write_text(bytes(buf).decode(encoding, errors="replace"), encoding="utf-8")
    print(f"\nCaptured {len(buf)} bytes.")
    print(f"  raw     : {bin_path}")
    print(f"  decoded : {txt_path}")
    print("\nHex dump of the first 512 bytes:\n")
    print(_hexdump(bytes(buf[:512])))
    return 0


def parse_file(path: Path, profile_name: str, encoding: str) -> int:
    from .report_parser import ReportParseError, parse_report
    from .report_profile import load_profile

    profile = load_profile(profile_name)
    data = path.read_bytes()
    text = data.decode(encoding, errors="replace") if path.suffix == ".bin" else path.read_text(
        encoding="utf-8", errors="replace"
    )

    try:
        report = parse_report(text, profile)
    except ReportParseError as exc:
        print(f"PARSE FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"Profile        : {profile.name}")
    print(f"Currency       : {report.currency}")
    print(f"Fitness mode   : {report.fitness_mode}")
    print(f"Denominations  : {report.denominations}")
    print(f"Counted qty    : {report.counted_quantity}   (report: {report.total_quantity})")
    print(f"Counted value  : {report.counted_value}   (report: {report.total_value})")
    print(f"Rejects        : {report.reject_quantity}")
    if report.unmatched_lines:
        print(f"Unmatched lines: {report.unmatched_lines}")
    if report.warnings:
        print("\nWARNINGS:")
        for w in report.warnings:
            print(f"  - {w}")
        return 2
    print("\nReport parsed cleanly and totals agree.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Capture or replay a cash counter serial report.")
    ap.add_argument("--port", default="COM1", help="serial port to listen on")
    ap.add_argument("--baud", type=int, default=115200, help="baud rate")
    ap.add_argument("--encoding", default="cp437", help="text encoding of the printout")
    ap.add_argument("--outdir", type=Path, default=DEFAULT_DIR, help="where to write captures")
    ap.add_argument("--parse", type=Path, help="parse an existing capture instead of listening")
    ap.add_argument("--profile", default="bps_c1_eur", help="device profile to parse with")
    ap.add_argument("--list-ports", action="store_true", help="list available serial ports and exit")
    args = ap.parse_args(argv)

    if args.list_ports:
        return list_ports()
    if args.parse:
        return parse_file(args.parse, args.profile, args.encoding)
    return capture(args.port, args.baud, args.encoding, args.outdir)


if __name__ == "__main__":
    raise SystemExit(main())
