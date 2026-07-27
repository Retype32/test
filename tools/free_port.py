"""
free_port — find and release whatever is holding a Windows serial port.

Standalone utility. Nothing in Brinks Nexus imports it; the counter driver
opens its port normally and fails with a plain error if the port is busy.

    python tools/free_port.py --port COM3              # who has it? (no changes)
    python tools/free_port.py --port COM3 --release    # stop them
    python tools/free_port.py --restore                # start them again

Finding the holder needs Administrator: identifying a handle in another
process requires duplicating it, which normal users may not do. Without
elevation the tool still reports whether the port is busy and lists likely
candidates by name, it just cannot prove which one holds it.

--release prefers stopping a Windows *service* over killing a process, because
a service stops cleanly and can be started again afterwards. What was stopped
is recorded so --restore can put it back. Use --hard only if a graceful stop
fails; killing a process mid-write can lose whatever it had not yet saved.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from pathlib import Path

STATE_FILE = Path(os.environ.get("LOCALAPPDATA", ".")) / "BrinksNexus" / "freed_ports.json"

# ── Windows plumbing ─────────────────────────────────────────────────────────

_ntdll = ctypes.WinDLL("ntdll")
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

SystemExtendedHandleInformation = 64
ObjectNameInformation = 1
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
PROCESS_DUP_HANDLE = 0x0040
DUPLICATE_SAME_ACCESS = 0x0002

# Explicit signatures matter here. Without them ctypes defaults every return
# value to a 32-bit int, which truncates 64-bit HANDLEs to garbage, and
# NTSTATUS values come back sign-extended so 0xC0000004 never compares equal.
_ntdll.NtQuerySystemInformation.restype = ctypes.c_long
_ntdll.NtQuerySystemInformation.argtypes = [
    ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
]
_ntdll.NtQueryObject.restype = ctypes.c_long
_ntdll.NtQueryObject.argtypes = [
    wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
    ctypes.POINTER(ctypes.c_ulong),
]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.GetCurrentProcess.argtypes = []
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.DuplicateHandle.restype = wintypes.BOOL
_kernel32.DuplicateHandle.argtypes = [
    wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
    ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
]


def _ntstatus(value: int) -> int:
    """NTSTATUS as unsigned, so it compares against the documented constants."""
    return value & 0xFFFFFFFF


class SYSTEM_HANDLE_ENTRY(ctypes.Structure):
    _fields_ = [
        ("Object", ctypes.c_void_p),
        ("UniqueProcessId", ctypes.c_size_t),
        ("HandleValue", ctypes.c_size_t),
        ("GrantedAccess", ctypes.c_ulong),
        ("CreatorBackTraceIndex", ctypes.c_ushort),
        ("ObjectTypeIndex", ctypes.c_ushort),
        ("HandleAttributes", ctypes.c_ulong),
        ("Reserved", ctypes.c_ulong),
    ]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_void_p),
    ]


@dataclass
class Holder:
    pid: int
    process: str = "?"
    exe: str = ""
    service_name: str = ""
    service_display: str = ""
    proven: bool = False   # True when we matched an actual open handle

    def label(self) -> str:
        if self.service_name:
            return f"service '{self.service_name}' ({self.service_display or self.process}, pid {self.pid})"
        return f"process {self.process} (pid {self.pid})"


@dataclass
class ReleaseRecord:
    port: str
    stopped_services: list[str] = field(default_factory=list)
    killed: list[dict] = field(default_factory=list)
    when: float = 0.0


def is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def powershell(script: str, timeout: int = 30) -> str:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
        )
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# ── port state ───────────────────────────────────────────────────────────────


def port_is_busy(port: str) -> bool | None:
    """True if held, False if free, None if the port does not exist."""
    try:
        import serial
    except ImportError:
        print("pyserial is required: pip install pyserial", file=sys.stderr)
        raise SystemExit(1)

    try:
        handle = serial.Serial(port=port, timeout=0.1)
    except serial.SerialException as exc:
        detail = str(exc).lower()
        if "access is denied" in detail or "permission" in detail:
            return True
        return None
    handle.close()
    return False


def nt_device_names(port: str) -> set[str]:
    """NT device paths for a COM port, from HKLM\\HARDWARE\\DEVICEMAP\\SERIALCOMM."""
    import winreg

    names: set[str] = set()
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"HARDWARE\DEVICEMAP\SERIALCOMM")
    except OSError:
        return names
    with key:
        index = 0
        while True:
            try:
                device, com, _ = winreg.EnumValue(key, index)
            except OSError:
                break
            index += 1
            if str(com).upper() == port.upper():
                names.add(device)
                names.add(device.replace("\\Device\\", ""))
    return names


# ── handle enumeration ───────────────────────────────────────────────────────


def _snapshot_handles() -> list[SYSTEM_HANDLE_ENTRY]:
    size = 1 << 20
    for _ in range(12):
        buf = ctypes.create_string_buffer(size)
        needed = ctypes.c_ulong(0)
        status = _ntstatus(_ntdll.NtQuerySystemInformation(
            SystemExtendedHandleInformation, buf, size, ctypes.byref(needed)
        ))
        if status == STATUS_INFO_LENGTH_MISMATCH:
            size = max(needed.value + (1 << 16), size * 2)
            continue
        if status != 0:
            return []
        break
    else:
        return []

    count = ctypes.c_size_t.from_buffer(buf, 0).value
    offset = ctypes.sizeof(ctypes.c_size_t) * 2
    entries = (SYSTEM_HANDLE_ENTRY * count).from_buffer(buf, offset)
    return list(entries)


def _handle_name(pid: int, handle_value: int) -> str | None:
    """Duplicate a handle from another process and read its object name."""
    source = _kernel32.OpenProcess(PROCESS_DUP_HANDLE, False, pid)
    if not source:
        return None
    duplicate = wintypes.HANDLE()
    ok = _kernel32.DuplicateHandle(
        source, wintypes.HANDLE(handle_value),
        _kernel32.GetCurrentProcess(), ctypes.byref(duplicate),
        0, False, DUPLICATE_SAME_ACCESS,
    )
    _kernel32.CloseHandle(source)
    if not ok:
        return None

    try:
        buf = ctypes.create_string_buffer(2048)
        needed = ctypes.c_ulong(0)
        status = _ntstatus(_ntdll.NtQueryObject(
            duplicate, ObjectNameInformation, buf, len(buf), ctypes.byref(needed)
        ))
        if status != 0:
            return None
        us = UNICODE_STRING.from_buffer(buf, 0)
        if not us.Buffer or not us.Length:
            return None
        return ctypes.wstring_at(us.Buffer, us.Length // 2)
    finally:
        _kernel32.CloseHandle(duplicate)


def _file_type_index() -> int | None:
    """Find the object-type index used for File handles.

    Opening a file we own, then locating that handle in the snapshot, tells us
    the index without hardcoding a Windows-version-specific number. Restricting
    the scan to File handles avoids querying handle types whose name lookup can
    block indefinitely.
    """
    import msvcrt

    me = os.getpid()
    with open(__file__, "rb") as fh:
        raw = msvcrt.get_osfhandle(fh.fileno())
        for entry in _snapshot_handles():
            if entry.UniqueProcessId == me and entry.HandleValue == raw:
                return entry.ObjectTypeIndex
    return None


def find_holders(port: str) -> list[Holder]:
    """Identify processes with an open handle to the port's device."""
    devices = {d.lower() for d in nt_device_names(port)}
    if not devices:
        return []

    type_index = _file_type_index()
    holders: dict[int, Holder] = {}
    me = os.getpid()

    for entry in _snapshot_handles():
        pid = int(entry.UniqueProcessId)
        if pid in (0, 4, me):
            continue
        if type_index is not None and entry.ObjectTypeIndex != type_index:
            continue
        name = _handle_name(pid, int(entry.HandleValue))
        if not name:
            continue
        lowered = name.lower()
        if any(lowered == d or lowered.endswith(d) for d in devices):
            holders.setdefault(pid, Holder(pid=pid, proven=True))

    if holders:
        _annotate(holders)
    return list(holders.values())


def _annotate(holders: dict[int, Holder]) -> None:
    """Fill in process names, executables and owning service for each pid."""
    pids = ",".join(str(p) for p in holders)
    out = powershell(
        f"$ids=@({pids}); "
        "Get-Process -Id $ids -ErrorAction SilentlyContinue | "
        "ForEach-Object { '{0}|{1}|{2}' -f $_.Id, $_.ProcessName, $_.Path }"
    )
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) >= 2 and parts[0].isdigit():
            holder = holders.get(int(parts[0]))
            if holder:
                holder.process = parts[1]
                holder.exe = parts[2] if len(parts) > 2 else ""

    out = powershell(
        f"$ids=@({pids}); "
        "Get-CimInstance Win32_Service | Where-Object { $ids -contains $_.ProcessId } | "
        "ForEach-Object { '{0}|{1}|{2}' -f $_.ProcessId, $_.Name, $_.DisplayName }"
    )
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) >= 3 and parts[0].isdigit():
            holder = holders.get(int(parts[0]))
            if holder:
                holder.service_name = parts[1]
                holder.service_display = parts[2]


def guess_candidates() -> list[str]:
    """Name-based guesses, for when handles cannot be inspected."""
    pattern = "ISA|CPS|DeLaRue|Compass|Giesecke|GDUSB|BPS|Cash|Currency|Banknote"
    out = powershell(
        "Get-CimInstance Win32_Service | Where-Object { "
        f"($_.Name -match '{pattern}' -or $_.DisplayName -match '{pattern}') "
        "-and $_.PathName -notmatch [regex]::Escape($env:SystemRoot) } | "
        "ForEach-Object { '{0,-9} {1,-24} {2}' -f $_.State, $_.Name, $_.DisplayName }"
    )
    return [line for line in out.splitlines() if line.strip()]


# ── release / restore ────────────────────────────────────────────────────────


def _save(record: ReleaseRecord) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_all()
    existing.append(asdict(record))
    STATE_FILE.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def _load_all() -> list[dict]:
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def release(port: str, holders: list[Holder], hard: bool) -> int:
    record = ReleaseRecord(port=port, when=time.time())

    for holder in holders:
        if holder.service_name:
            print(f"  stopping {holder.label()}...")
            powershell(f"Stop-Service -Name '{holder.service_name}' -Force "
                       "-ErrorAction SilentlyContinue")
            record.stopped_services.append(holder.service_name)
        elif hard:
            print(f"  killing {holder.label()}...")
            subprocess.run(["taskkill", "/PID", str(holder.pid), "/F"],
                           capture_output=True)
            record.killed.append({"pid": holder.pid, "process": holder.process,
                                  "exe": holder.exe})
        else:
            print(f"  {holder.label()} is not a service — needs --hard to kill it.")

    if record.stopped_services or record.killed:
        _save(record)

    for _ in range(20):
        time.sleep(0.25)
        if port_is_busy(port) is False:
            print(f"\n{port} is now free.")
            if record.stopped_services:
                print("Run --restore when you are done to start those services again.")
            return 0
    print(f"\n{port} is still busy. Something else holds it, or a stopped "
          "service was restarted automatically by Windows recovery settings.")
    return 1


def restore() -> int:
    records = _load_all()
    if not records:
        print("Nothing recorded to restore.")
        return 0

    for record in records:
        for name in record.get("stopped_services", []):
            print(f"  starting service '{name}'...")
            powershell(f"Start-Service -Name '{name}' -ErrorAction SilentlyContinue")
        for killed in record.get("killed", []):
            exe = killed.get("exe")
            print(f"  {killed.get('process')} (pid {killed.get('pid')}) was killed"
                  + (f" — relaunch manually: {exe}" if exe else ""))

    STATE_FILE.unlink(missing_ok=True)
    print("\nDone. Services restarted; killed programs must be relaunched by hand.")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Find and release whatever is holding a serial port.")
    ap.add_argument("--port", help="serial port, e.g. COM3")
    ap.add_argument("--release", action="store_true", help="stop the holders")
    ap.add_argument("--hard", action="store_true",
                    help="also kill non-service processes (may lose unsaved data)")
    ap.add_argument("--restore", action="store_true",
                    help="restart services stopped earlier")
    args = ap.parse_args(argv)

    if args.restore:
        return restore()
    if not args.port:
        ap.error("--port is required (or use --restore)")

    busy = port_is_busy(args.port)
    if busy is None:
        print(f"{args.port} does not exist on this PC.")
        return 1
    if busy is False:
        print(f"{args.port} is free — nothing to release.")
        return 0

    print(f"{args.port} is in use.\n")
    holders = find_holders(args.port)

    if not holders:
        if not is_elevated():
            print("Could not identify the holder: this needs Administrator.")
            print("Re-run from an elevated terminal to see exactly which program")
            print("has the port.\n")
        else:
            print("Could not match an open handle to this port.\n")
        candidates = guess_candidates()
        if candidates:
            print("Services whose names suggest cash-machine software:")
            for line in candidates:
                print(f"  {line}")
            print("\nThese are name guesses, not proof.")
        return 1

    print("Held by:")
    for holder in holders:
        print(f"  {holder.label()}")
        if holder.exe:
            print(f"      {holder.exe}")
    print()

    if not args.release:
        print("Re-run with --release to stop them.")
        return 0

    return release(args.port, holders, args.hard)


if __name__ == "__main__":
    raise SystemExit(main())
