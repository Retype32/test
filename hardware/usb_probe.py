"""
USB probe for the G+D BPS C1.

The C1's USB port is driven by G+D's MAUSB driver, which is a rebadged
libusb-win32: mausb.sys reports itself as "LibUSB-Win32 - Kernel Driver" and
creates \\Device\\libusb0, and mausb.dll exports the standard libusb-0.1 API
(usb_open, usb_bulk_read, usb_claim_interface, ...). The transport is therefore
a documented, open API that Python can drive through pyusb's libusb0 backend.

What the driver does NOT tell us is the application protocol carried on the
bulk endpoints. This tool exists to find that out: it enumerates the device,
dumps its descriptors, and reads raw bytes from each IN endpoint so the traffic
can be inspected.

The working hypothesis is that the endpoint carries the same ASCII batch report
as the RS232 printer port — the vendor ID (0x10C4, Silicon Labs) is a USB-UART
bridge maker, so the pipe may simply be a UART. If the dump is readable text,
hardware/report_parser.py already handles it and only the transport changes.

    python -m hardware.usb_probe --list                 # what's attached
    python -m hardware.usb_probe --descriptors          # endpoints and config
    python -m hardware.usb_probe --read --seconds 60    # dump traffic

Requires the G+D MAUSB driver to be installed and bound to the device, and
Compass must not be running — it claims the interface exclusively.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

# Giesecke & Devrient, per MAUSB.inf: USB\VID_10c4&PID_ea63
GD_VENDOR_ID = 0x10C4
GD_PRODUCT_ID = 0xEA63

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "reports" / "captures"

# Where the G+D driver DLL usually lands once MAUSB.inf has been installed.
_DLL_CANDIDATES = ("mausb.dll", "mausb_x64.dll", "libusb0.dll")


def _backend(dll: str | None = None):
    """Get a libusb-0.1 backend, preferring the G+D DLL if one is named."""
    import usb.backend.libusb0

    if dll:
        backend = usb.backend.libusb0.get_backend(find_library=lambda _: dll)
        if backend is None:
            raise RuntimeError(f"Could not load libusb-0.1 backend from {dll!r}.")
        return backend

    backend = usb.backend.libusb0.get_backend()
    if backend is None:
        for candidate in _DLL_CANDIDATES:
            backend = usb.backend.libusb0.get_backend(find_library=lambda _, c=candidate: c)
            if backend is not None:
                break
    if backend is None:
        raise RuntimeError(
            "No libusb-0.1 backend found. Install the G+D MAUSB driver "
            "(MAUSB.inf), or pass --dll with the path to mausb.dll."
        )
    return backend


def _describe(device) -> str:
    import usb.util

    def safe(getter, default="?"):
        try:
            return getter()
        except Exception:
            return default

    vendor = safe(lambda: usb.util.get_string(device, device.iManufacturer))
    product = safe(lambda: usb.util.get_string(device, device.iProduct))
    serial = safe(lambda: usb.util.get_string(device, device.iSerialNumber))
    tag = ""
    if (device.idVendor, device.idProduct) == (GD_VENDOR_ID, GD_PRODUCT_ID):
        tag = "   <-- G+D device from MAUSB.inf"
    return (
        f"  {device.idVendor:04X}:{device.idProduct:04X}  bus {device.bus} "
        f"addr {device.address}  {vendor or '?'} / {product or '?'} "
        f"[serial {serial or '?'}]{tag}"
    )


def list_devices(dll: str | None) -> int:
    import usb.core

    devices = list(usb.core.find(find_all=True, backend=_backend(dll)))
    if not devices:
        print("No USB devices visible through the libusb backend.\n")
        print("libusb-win32 only sees devices its driver is bound to. If the C1")
        print("is plugged in but absent here, MAUSB.inf has not been installed")
        print("for it. Check Device Manager for the device under 'GDUSB devices'.")
        return 1

    print(f"{len(devices)} device(s) visible to the libusb backend:\n")
    target = None
    for device in devices:
        print(_describe(device))
        if (device.idVendor, device.idProduct) == (GD_VENDOR_ID, GD_PRODUCT_ID):
            target = device
    print()
    if target is None:
        print(f"The G+D device ({GD_VENDOR_ID:04X}:{GD_PRODUCT_ID:04X}) is NOT attached.")
        print("Connect the C1 over USB and make sure the MAUSB driver is bound to it.")
        return 1
    print(f"Found the G+D device at {GD_VENDOR_ID:04X}:{GD_PRODUCT_ID:04X}.")
    return 0


def show_descriptors(vid: int, pid: int, dll: str | None) -> int:
    import usb.core

    device = usb.core.find(idVendor=vid, idProduct=pid, backend=_backend(dll))
    if device is None:
        print(f"Device {vid:04X}:{pid:04X} not found.")
        return 1

    print(_describe(device))
    print()
    for config in device:
        print(f"  Configuration {config.bConfigurationValue} "
              f"({config.bNumInterfaces} interface(s), "
              f"{config.bMaxPower * 2} mA)")
        for interface in config:
            print(f"    Interface {interface.bInterfaceNumber} "
                  f"alt {interface.bAlternateSetting}: "
                  f"class 0x{interface.bInterfaceClass:02X} "
                  f"subclass 0x{interface.bInterfaceSubClass:02X} "
                  f"protocol 0x{interface.bInterfaceProtocol:02X}")
            for endpoint in interface:
                import usb.util

                direction = usb.util.endpoint_direction(endpoint.bEndpointAddress)
                kind = usb.util.endpoint_type(endpoint.bmAttributes)
                names = {0: "control", 1: "isochronous", 2: "bulk", 3: "interrupt"}
                print(f"      Endpoint 0x{endpoint.bEndpointAddress:02X} "
                      f"{'IN ' if direction == usb.util.ENDPOINT_IN else 'OUT'} "
                      f"{names.get(kind, '?'):<10} "
                      f"max packet {endpoint.wMaxPacketSize}")
    print("\nA single bulk IN endpoint with no matching OUT is consistent with the")
    print("machine streaming its report one way, exactly as it does over RS232.")
    return 0


def read_endpoint(vid: int, pid: int, endpoint_addr: int | None,
                  seconds: int, dll: str | None, outdir: Path) -> int:
    import usb.core
    import usb.util

    device = usb.core.find(idVendor=vid, idProduct=pid, backend=_backend(dll))
    if device is None:
        print(f"Device {vid:04X}:{pid:04X} not found.")
        return 1

    try:
        device.set_configuration()
    except usb.core.USBError as exc:
        print(f"Could not configure the device: {exc}")
        print("Compass may be running and holding the interface — close it first.")
        return 1

    config = device.get_active_configuration()
    interface = config[(0, 0)]

    if endpoint_addr is None:
        candidates = [
            e for e in interface
            if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
        ]
        if not candidates:
            print("No IN endpoint to read from.")
            return 1
        endpoint = candidates[0]
    else:
        endpoint = usb.util.find_descriptor(
            interface, bEndpointAddress=endpoint_addr
        )
        if endpoint is None:
            print(f"Endpoint 0x{endpoint_addr:02X} not found.")
            return 1

    print(f"Reading endpoint 0x{endpoint.bEndpointAddress:02X} for {seconds}s.")
    print("Run a batch on the machine and end it so it reports.\n")

    import time

    buf = bytearray()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            chunk = endpoint.read(endpoint.wMaxPacketSize, timeout=500)
        except usb.core.USBError as exc:
            if "timeout" in str(exc).lower() or getattr(exc, "errno", None) == 110:
                continue
            print(f"Read error: {exc}")
            break
        if chunk:
            data = bytes(chunk)
            buf.extend(data)
            sys.stdout.write(data.decode("cp437", errors="replace"))
            sys.stdout.flush()

    if not buf:
        print("\nNothing received. The machine may not route reports to USB, or")
        print("the report may be on a different endpoint — check --descriptors.")
        return 1

    outdir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bin_path = outdir / f"c1-usb-{stamp}.bin"
    txt_path = outdir / f"c1-usb-{stamp}.txt"
    bin_path.write_bytes(bytes(buf))
    txt_path.write_text(bytes(buf).decode("cp437", errors="replace"), encoding="utf-8")

    printable = sum(1 for b in buf if 32 <= b < 127 or b in (9, 10, 13))
    ratio = printable / len(buf)
    print(f"\n\nCaptured {len(buf)} bytes -> {bin_path}")
    print(f"Printable characters: {ratio:.0%}")
    if ratio > 0.85:
        print("\nThis looks like text — very likely the same report the printer")
        print("port emits. Try parsing it directly:")
        print(f"  python -m hardware.capture --parse {txt_path}")
    else:
        print("\nThis looks like binary framing, not the plain-text report.")
        print("Reverse-engineering it would be a separate exercise; the RS232")
        print("printer port remains the practical integration path.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Probe the C1's USB interface.")
    ap.add_argument("--list", action="store_true", help="list visible USB devices")
    ap.add_argument("--descriptors", action="store_true", help="dump endpoints")
    ap.add_argument("--read", action="store_true", help="read from an IN endpoint")
    ap.add_argument("--vid", type=lambda s: int(s, 16), default=GD_VENDOR_ID)
    ap.add_argument("--pid", type=lambda s: int(s, 16), default=GD_PRODUCT_ID)
    ap.add_argument("--endpoint", type=lambda s: int(s, 16), default=None)
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--dll", help="path to mausb.dll / libusb0.dll")
    ap.add_argument("--outdir", type=Path, default=DEFAULT_DIR)
    args = ap.parse_args(argv)

    try:
        if args.descriptors:
            return show_descriptors(args.vid, args.pid, args.dll)
        if args.read:
            return read_endpoint(args.vid, args.pid, args.endpoint,
                                 args.seconds, args.dll, args.outdir)
        return list_devices(args.dll)
    except ImportError:
        print("pyusb is not installed. Run: pip install pyusb", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
