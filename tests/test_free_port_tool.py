"""
Tests for tools/free_port.py.

These verify the Windows handle-enumeration machinery works and that the
tool's decision logic is safe: it must never stop anything unless explicitly
asked, and it must record what it stopped so it can be restored.

Nothing here stops or kills a real process.
"""

import json
import os
import sys
from pathlib import Path

import pytest

if sys.platform != "win32":
    pytest.skip("Windows only (free_port.py loads ntdll/kernel32 at import time)", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import free_port as fp  # noqa: E402


# ── the ctypes machinery ─────────────────────────────────────────────────────


def test_handle_snapshot_returns_entries():
    handles = fp._snapshot_handles()
    assert len(handles) > 100, "system-wide handle table should not be tiny"


def test_file_type_index_is_discovered():
    """The File type index must be found without hardcoding a Windows build."""
    index = fp._file_type_index()
    assert index is not None
    assert 0 < index < 100


def test_our_own_file_handles_are_visible_and_named():
    """Proves duplication + name lookup work on a handle we definitely own."""
    index = fp._file_type_index()
    assert index is not None

    with open(__file__, "rb") as fh:
        import msvcrt

        raw = msvcrt.get_osfhandle(fh.fileno())
        name = fp._handle_name(os.getpid(), raw)

    assert name, "should resolve a name for our own open file handle"
    assert "test_free_port_tool" in name.lower()


def test_serialcomm_lookup_does_not_crash_for_absent_port():
    assert fp.nt_device_names("COM255") == set()


def test_port_is_busy_returns_none_for_missing_port():
    assert fp.port_is_busy("COM255") is None


# ── safety behaviour ─────────────────────────────────────────────────────────


def test_reporting_only_by_default(monkeypatch, capsys):
    """Without --release the tool must not stop anything."""
    monkeypatch.setattr(fp, "port_is_busy", lambda _p: True)
    monkeypatch.setattr(fp, "find_holders",
                        lambda _p: [fp.Holder(pid=999, process="ISA.Host",
                                              service_name="IsaDevice",
                                              service_display="ISA Device Service")])

    def explode(*_a, **_kw):
        raise AssertionError("must not stop anything without --release")

    monkeypatch.setattr(fp, "release", explode)
    assert fp.main(["--port", "COM3"]) == 0
    assert "--release" in capsys.readouterr().out


def test_non_service_process_is_not_killed_without_hard(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(fp, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(fp, "port_is_busy", lambda _p: True)
    calls = []
    monkeypatch.setattr(fp.subprocess, "run",
                        lambda *a, **k: calls.append(a) or _Completed())

    holder = fp.Holder(pid=1234, process="SomeApp")
    fp.release("COM3", [holder], hard=False)

    assert not any("taskkill" in str(c) for c in calls)
    assert "--hard" in capsys.readouterr().out


def test_stopped_service_is_recorded_for_restore(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    monkeypatch.setattr(fp, "STATE_FILE", state)
    monkeypatch.setattr(fp, "port_is_busy", lambda _p: False)
    monkeypatch.setattr(fp, "powershell", lambda *_a, **_kw: "")

    holder = fp.Holder(pid=999, process="IsaHost", service_name="IsaDevice")
    assert fp.release("COM3", [holder], hard=False) == 0

    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved[0]["stopped_services"] == ["IsaDevice"]
    assert saved[0]["port"] == "COM3"


def test_restore_starts_recorded_services_and_clears_state(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps([
        {"port": "COM3", "stopped_services": ["IsaDevice"], "killed": [], "when": 0}
    ]), encoding="utf-8")
    monkeypatch.setattr(fp, "STATE_FILE", state)

    started = []
    monkeypatch.setattr(fp, "powershell", lambda script, **_kw: started.append(script) or "")

    assert fp.restore() == 0
    assert any("Start-Service" in s and "IsaDevice" in s for s in started)
    assert not state.exists()


def test_free_port_reports_nothing_to_do(monkeypatch, capsys):
    monkeypatch.setattr(fp, "port_is_busy", lambda _p: False)
    assert fp.main(["--port", "COM3"]) == 0
    assert "free" in capsys.readouterr().out.lower()


class _Completed:
    returncode = 0
    stdout = ""
    stderr = ""
