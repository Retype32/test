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
import time
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "reports" / "captures"


# USB-to-serial bridge chips commonly built into industrial equipment, and the
# generic CDC-ACM class used when a device presents its own virtual COM port.
_KNOWN_BRIDGES = {
    "0403": "FTDI",
    "067B": "Prolific",
    "10C4": "Silicon Labs CP210x",
    "1A86": "WCH CH340/CH341",
    "2341": "Arduino (CDC)",
}


def list_ports() -> int:
    """Show the serial ports Windows can see, so the right one can be picked.

    A machine connected over USB usually appears here too: industrial equipment
    with a USB Type B socket typically presents a virtual COM port (USB CDC, or
    an internal FTDI/Prolific bridge). If it shows up, it is usable exactly like
    a physical RS232 port and needs no special support.
    """
    from serial.tools import list_ports as lp

    ports = sorted(lp.comports(), key=lambda p: p.device)
    if not ports:
        print("No serial ports found on this PC.\n")
        print("If the machine is plugged in over USB, it is not presenting a")
        print("virtual COM port. Check Device Manager for an unrecognised device")
        print("under 'Other devices' — that means a vendor USB driver is missing.")
        print("Install the manufacturer's USB driver and run this again.\n")
        print("Otherwise you need either:")
        print("  - a USB cable to the machine's USB device port, or")
        print("  - a USB-to-serial adapter plus a null-modem cable to its")
        print("    RS232 printer port.")
        return 1

    print(f"{len(ports)} serial port(s) found:\n")
    usb_ports = []
    for p in ports:
        print(f"  {p.device:<8} {p.description}")
        if p.hwid and p.hwid != "n/a":
            print(f"           {p.hwid}")
        if p.vid is not None:
            vid = f"{p.vid:04X}"
            bridge = _KNOWN_BRIDGES.get(vid)
            kind = f"USB device, {bridge} bridge" if bridge else "USB device"
            maker = f" — {p.manufacturer}" if p.manufacturer else ""
            print(f"           {kind}: VID {vid} PID {p.pid:04X}{maker}")
            usb_ports.append(p.device)
        print()

    if usb_ports:
        print(f"USB-attached port(s): {', '.join(usb_ports)}")
        print("A machine on USB is read exactly like one on RS232 — no adapter,")
        print("no null-modem cable, and no code change. Point COUNTER_COM_PORT")
        print("at it and continue with --doctor.\n")
    print("Set COUNTER_COM_PORT in .env to the port the machine is wired to.")
    return 0


def _powershell(script: str) -> list[str]:
    """Run a short PowerShell query and return its output lines."""
    import subprocess

    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.rstrip() for line in out.stdout.splitlines() if line.strip()]


def port_status() -> int:
    """Show which serial ports are free, and what is likely holding the busy ones.

    ISA is a browser-based application, so there is no window to close: the
    process that owns the machine's port is an ISA service on this PC, and the
    browser is only its user interface. This locates that service.
    """
    import serial
    from serial.tools import list_ports as lp

    ports = sorted(lp.comports(), key=lambda p: p.device)
    if not ports:
        print("No serial ports on this PC — nothing can be holding one.\n")
    else:
        print("Serial ports:\n")
        busy = []
        for p in ports:
            try:
                handle = serial.Serial(port=p.device, timeout=0.1)
            except serial.SerialException as exc:
                detail = str(exc).lower()
                if "access is denied" in detail or "permission" in detail:
                    print(f"  {p.device:<8} IN USE by another program   ({p.description})")
                    busy.append(p.device)
                else:
                    print(f"  {p.device:<8} error: {exc}")
                continue
            handle.close()
            print(f"  {p.device:<8} free                       ({p.description})")
        print()
        if not busy:
            print("Every port is free. Nothing is in the way.\n")

    # Deliberately narrow. Generic words like "Device" match dozens of built-in
    # Windows services and drown the real answer in noise.
    pattern = "ISA|CPS|DeLaRue|De La Rue|Compass|Giesecke|GDUSB|BPS|Cash|Currency|Banknote"

    print("Looking for non-Microsoft services that may own a machine port...\n")
    services = _powershell(
        "Get-CimInstance Win32_Service | Where-Object { "
        "($_.Name -match '" + pattern + "' -or $_.DisplayName -match '" + pattern + "') "
        "-and $_.PathName -notmatch [regex]::Escape($env:SystemRoot) } | "
        "Select-Object -First 20 | "
        "ForEach-Object { '{0,-9} {1,-26} {2}' -f $_.State, $_.Name, $_.PathName }"
    )
    if services:
        print("  Candidate services (state, name, executable):")
        for line in services:
            print(f"    {line}")
    else:
        print("  No matching third-party service found on this PC.")

    print()
    processes = _powershell(
        "Get-Process | Where-Object { $_.ProcessName -match '" + pattern + "' -and "
        "$_.Path -notmatch [regex]::Escape($env:SystemRoot) } | "
        "Select-Object -First 20 | "
        "ForEach-Object { '{0,-8} {1,-24} {2}' -f $_.Id, $_.ProcessName, $_.Path }"
    )
    if processes:
        print("  Candidate running programs (pid, name, executable):")
        for line in processes:
            print(f"    {line}")
    else:
        print("  No matching third-party program running on this PC.")

    print(
        "\nISA's browser window is only its user interface. The component that\n"
        "holds the serial port is an ISA service or background process on the PC\n"
        "the machine is cabled to, so closing the browser frees nothing.\n"
        "\n"
        "Names above are only candidates — confirm with whoever administers ISA\n"
        "before stopping anything. Stopping the wrong service, or the right one\n"
        "at the wrong time, can take counting offline for other users."
    )
    return 0


def doctor(port: str, baud: int, profile_name: str, listen_seconds: int = 20) -> int:
    """Check everything that can be checked before a real batch is run."""
    ok = True
    print("Brinks Nexus — cash counter preflight\n")

    # 1. Configuration loads.
    try:
        from .report_profile import load_profile

        profile = load_profile(profile_name)
        print(f"[ok]   Profile '{profile_name}' loaded: {profile.name}, "
              f"{len(profile.denoms)} denominations")
    except Exception as exc:
        print(f"[FAIL] Profile '{profile_name}' could not be loaded: {exc}")
        return 1

    # 2. Parser works end to end against simulated reports.
    from .simulate import DEFAULT_BATCH, LAYOUTS, build_report
    from .report_parser import ReportParseError, parse_report

    bad = []
    for layout in LAYOUTS:
        try:
            r = parse_report(build_report(DEFAULT_BATCH, layout), profile)
            if r.warnings or r.counted_quantity != sum(DEFAULT_BATCH.values()):
                bad.append(layout)
        except ReportParseError:
            bad.append(layout)
    if bad:
        print(f"[warn] Parser failed on simulated layouts: {', '.join(bad)}")
        ok = False
    else:
        print(f"[ok]   Parser handled all {len(LAYOUTS)} simulated print layouts")

    # 3. Serial port exists.
    from serial.tools import list_ports as lp

    available = {p.device.upper(): p for p in lp.comports()}
    if not available:
        print("[FAIL] No serial ports on this PC.")
        print("       Connect the machine over USB (it should appear as a")
        print("       virtual COM port), or attach a USB-to-serial adapter and")
        print("       a null-modem cable to its RS232 printer port.")
        print("       Run 'python -m hardware.capture --list-ports' for detail.")
        return 1
    if port.upper() not in available:
        print(f"[FAIL] {port} not found. Available: {', '.join(sorted(available))}")
        return 1
    info = available[port.upper()]
    over_usb = " over USB" if info.vid is not None else ""
    print(f"[ok]   {port} exists{over_usb} ({info.description})")

    # 4. Serial port opens.
    import serial

    try:
        sp = serial.Serial(port=port, baudrate=baud, timeout=0.5)
    except serial.SerialException as exc:
        print(f"[FAIL] {port} could not be opened: {exc}")
        print("       Another program may own it — ISA holds the printer port "
              "if it is running on this PC.")
        return 1
    print(f"[ok]   {port} opened at {baud} baud")

    # 5. Listen for traffic.
    print(f"\nListening on {port} for {listen_seconds}s — run a batch on the "
          "machine now and end it so it prints.\n")
    buf = bytearray()
    deadline = time.monotonic() + listen_seconds
    try:
        while time.monotonic() < deadline:
            chunk = sp.read(4096)
            if chunk:
                buf.extend(chunk)
    finally:
        sp.close()

    if not buf:
        print("[warn] Nothing received. If you did run a batch, check:")
        print("       - the cable is null-modem, not straight-through")
        print("       - the machine's report output is routed to serial")
        print(f"       - the machine's baud rate matches {baud}")
        return 1

    text = bytes(buf).decode(profile.encoding, errors="replace")
    print(f"[ok]   Received {len(buf)} bytes")
    try:
        report = parse_report(text, profile)
    except ReportParseError as exc:
        print(f"[FAIL] Received data did not parse: {exc}")
        print(f"\nRaw:\n{text}")
        return 1

    print(f"[ok]   Parsed: {report.denominations}")
    print(f"       counted {report.counted_quantity} notes, "
          f"{report.counted_value} {report.currency or ''}".rstrip())
    if report.warnings:
        print("[warn] Totals disagree:")
        for w in report.warnings:
            print(f"       - {w}")
        ok = False

    print("\nReady to switch COUNTER_MODE=c1_report." if ok else
          "\nFix the warnings above before switching COUNTER_MODE=c1_report.")
    return 0 if ok else 1


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
    ap.add_argument("--doctor", action="store_true", help="run the full preflight check")
    ap.add_argument("--port-status", action="store_true",
                    help="show which ports are busy and what may be holding them")
    ap.add_argument("--listen-seconds", type=int, default=20, help="doctor listen window")
    args = ap.parse_args(argv)

    if args.list_ports:
        return list_ports()
    if args.port_status:
        return port_status()
    if args.doctor:
        return doctor(args.port, args.baud, args.profile, args.listen_seconds)
    if args.parse:
        return parse_file(args.parse, args.profile, args.encoding)
    return capture(args.port, args.baud, args.encoding, args.outdir)


if __name__ == "__main__":
    raise SystemExit(main())
