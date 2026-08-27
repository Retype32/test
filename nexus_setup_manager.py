#!/usr/bin/env python3
"""
Nexus Setup Manager
====================

A standalone, dependency-free (stdlib only) Windows GUI utility that automates
setup and launch of the Nexus application (Brink's Nexus - a FastAPI +
SQLAlchemy-async web app) using a portable WinPython distribution, for a
non-technical user without administrator rights.

Run with the portable interpreter, e.g.:

    C:\\path\\to\\WPy64-3.13.12.0\\python\\python.exe nexus_setup_manager.py

This file intentionally uses ONLY the Python standard library.
"""

import os
import re
import sys
import json
import time
import queue
import shutil
import string
import ctypes
import platform
import threading
import traceback
import subprocess
import webbrowser
from pathlib import Path
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

APP_TITLE = "Nexus Setup Manager"
APP_FOLDER_NAME = "NexusSetupManager"
SETTINGS_FILENAME = "settings.json"
LOG_SUBFOLDER = "logs"

IS_WINDOWS = platform.system() == "Windows"

# Known-good minimum asyncpg version for Python 3.13 (per environment notes).
# This is a *default assumption*, not a hardcoded requirement - the actual
# repository's requirements.txt is always the source of truth, and this
# value is only used when explaining/recommending a fix.
DEFAULT_MIN_ASYNCPG_FOR_PY313 = "0.30.0"

# Subprocess creation flag to avoid flashing console windows on Windows.
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
CREATE_NEW_CONSOLE = 0x00000010 if IS_WINDOWS else 0


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------

def now_stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def now_ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def local_appdata_dir() -> Path:
    """Return a user-writable settings directory, without requiring admin
    rights and without depending on the current working directory."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        p = Path(base) / APP_FOLDER_NAME
    else:
        # Safe fallback inside the user's profile.
        p = Path.home() / "AppData" / "Local" / APP_FOLDER_NAME
        if not p.parent.parent.exists():
            p = Path.home() / f".{APP_FOLDER_NAME}"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        p = Path.home() / f".{APP_FOLDER_NAME}"
        p.mkdir(parents=True, exist_ok=True)
    return p


def log_dir() -> Path:
    d = local_appdata_dir() / LOG_SUBFOLDER
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_subprocess(args, cwd=None, timeout=None, env=None):
    """Run a subprocess with an argument list (never shell=True), capturing
    complete stdout/stderr. Returns (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode(errors="replace")
        err = e.stderr if isinstance(e.stderr, str) else (e.stderr or b"").decode(errors="replace")
        return -1, out, (err + "\n[Operation timed out]")
    except FileNotFoundError as e:
        return -1, "", f"Executable not found: {e}"
    except OSError as e:
        return -1, "", f"OS error while launching process: {e}"


def format_cmd(args):
    """Human readable command for logging, quoting args with spaces."""
    parts = []
    for a in args:
        s = str(a)
        if " " in s or "\t" in s:
            parts.append(f'"{s}"')
        else:
            parts.append(s)
    return " ".join(parts)


def read_text_safe(path: Path, max_bytes=400_000):
    try:
        if not path.is_file():
            return None
        data = path.read_bytes()[:max_bytes]
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


def is_admin_windows():
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# --------------------------------------------------------------------------
# Settings persistence
# --------------------------------------------------------------------------

class SettingsManager:
    """Loads and saves non-secret user settings to a JSON file in a
    user-writable location. Never stores secrets."""

    DEFAULTS = {
        "python_exe": "",
        "nexus_folder": "",
        "last_startup_command": [],
        "window_geometry": "",
        "open_logs_on_failure": True,
        "last_setup_success_date": "",
        "last_setup_success_summary": "",
        "install_mode": "normal",  # or "binary_only"
    }

    def __init__(self):
        self.path = local_appdata_dir() / SETTINGS_FILENAME
        self.data = dict(self.DEFAULTS)
        self.load()

    def load(self):
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    merged = dict(self.DEFAULTS)
                    merged.update(loaded)
                    self.data = merged
            except (OSError, json.JSONDecodeError):
                # Corrupt settings file - fall back to defaults rather than crash.
                self.data = dict(self.DEFAULTS)
        return self.data

    def save(self):
        try:
            self.path.write_text(
                json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            return True, ""
        except OSError as e:
            return False, str(e)

    def reset(self):
        self.data = dict(self.DEFAULTS)
        return self.save()

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


# --------------------------------------------------------------------------
# Portable Python discovery + validation
# --------------------------------------------------------------------------

class PythonInfo:
    def __init__(self):
        self.exe_path = ""
        self.exists = False
        self.version = ""
        self.version_tuple = None
        self.sys_executable = ""
        self.architecture = ""
        self.machine = ""
        self.is_64bit = False
        self.pip_ok = False
        self.pip_version = ""
        self.venv_ok = False
        self.errors = []

    def as_dict(self):
        return {
            "exe_path": self.exe_path,
            "exists": self.exists,
            "version": self.version,
            "sys_executable": self.sys_executable,
            "architecture": self.architecture,
            "machine": self.machine,
            "is_64bit": self.is_64bit,
            "pip_ok": self.pip_ok,
            "pip_version": self.pip_version,
            "venv_ok": self.venv_ok,
            "errors": list(self.errors),
        }


def find_python_candidates_in_folder(folder: Path):
    """Search sensible locations for a portable python.exe within a folder."""
    candidates = []
    relative_guesses = [
        "python.exe",
        "python\\python.exe",
        "python/python.exe",
        "Scripts\\python.exe",
        "Scripts/python.exe",
    ]
    for rel in relative_guesses:
        p = folder / rel
        if p.is_file():
            candidates.append(p)

    # Also do a shallow (max depth 2) scan for python.exe, in case the
    # portable distribution uses an unexpected layout (e.g. WPy64 releases
    # sometimes nest under a version-named subfolder).
    try:
        for child in folder.iterdir():
            if child.is_dir() and child.name.lower() not in ("scripts", "lib", "doc", "notebooks"):
                candidate = child / "python.exe"
                if candidate.is_file() and candidate not in candidates:
                    candidates.append(candidate)
    except OSError:
        pass

    # De-duplicate while preserving order.
    seen = set()
    unique = []
    for c in candidates:
        rp = str(c.resolve())
        if rp not in seen:
            seen.add(rp)
            unique.append(c)
    return unique


def validate_python(exe_path_str: str, log=lambda *a, **k: None) -> PythonInfo:
    info = PythonInfo()
    info.exe_path = exe_path_str
    p = Path(exe_path_str) if exe_path_str else None

    if not p or not p.is_file():
        info.errors.append("Python executable not found at the given path.")
        return info
    info.exists = True

    name_ok = p.name.lower() == "python.exe" if IS_WINDOWS else True
    if not name_ok:
        log("warn", f"Selected file is not named python.exe ({p.name}); will still attempt to run it.")

    # --version
    rc, out, err = run_subprocess([str(p), "--version"], timeout=20)
    combined = (out + err).strip()
    if rc != 0 or not combined:
        info.errors.append(f"Could not run '--version' on the selected executable. Details: {err.strip() or out.strip()}")
        return info
    info.version = combined
    m = re.search(r"Python\s+(\d+)\.(\d+)\.(\d+)", combined)
    if m:
        info.version_tuple = tuple(int(x) for x in m.groups())
    log("info", f"Python version: {combined}")

    # sys.executable
    rc, out, err = run_subprocess([str(p), "-c", "import sys; print(sys.executable)"], timeout=20)
    if rc == 0:
        info.sys_executable = out.strip()
        log("info", f"sys.executable: {info.sys_executable}")
    else:
        info.errors.append(f"Could not determine sys.executable: {err.strip()}")

    # architecture / machine
    rc, out, err = run_subprocess(
        [str(p), "-c", "import platform; print(platform.architecture()[0]); print(platform.machine())"],
        timeout=20,
    )
    if rc == 0:
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        if lines:
            info.architecture = lines[0]
            info.is_64bit = "64" in lines[0]
        if len(lines) > 1:
            info.machine = lines[1]
        log("info", f"Architecture: {info.architecture} ({info.machine})")
    else:
        info.errors.append(f"Could not determine architecture: {err.strip()}")

    # pip
    rc, out, err = run_subprocess([str(p), "-m", "pip", "--version"], timeout=30)
    if rc == 0:
        info.pip_ok = True
        info.pip_version = out.strip()
        log("info", f"pip: {info.pip_version}")
    else:
        info.errors.append(f"pip is not available for this interpreter: {err.strip() or out.strip()}")

    # venv
    rc, out, err = run_subprocess([str(p), "-c", "import venv; print('venv available')"], timeout=20)
    if rc == 0 and "venv available" in out:
        info.venv_ok = True
        log("info", "venv module: available")
    else:
        info.errors.append(f"The 'venv' standard-library module is not available: {err.strip() or out.strip()}")

    # No-admin-rights confirmation: a portable python that we can execute and
    # that can write next to itself (or to the settings dir) does not need
    # administrator rights. We do not attempt to elevate or modify PATH.
    return info


# --------------------------------------------------------------------------
# Nexus project inspection + validation
# --------------------------------------------------------------------------

class RequirementSpec:
    """A single parsed line from a requirements file."""
    def __init__(self, name, specifier, raw_line, source_file):
        self.name = name
        self.specifier = specifier  # e.g. "==0.29.0", ">=0.30.0", "" if unpinned
        self.raw_line = raw_line
        self.source_file = source_file

    def __repr__(self):
        return f"{self.name}{self.specifier}"


REQUIREMENT_LINE_RE = re.compile(
    r"^\s*([A-Za-z0-9_.\-]+)\s*(\[[^\]]*\])?\s*([<>=!~].*)?\s*(?:#.*)?$"
)

ALEMBIC_SECTION_RE = re.compile(r"^\[alembic\]", re.MULTILINE)
STARTUP_HINT_RE = re.compile(
    r"(?:^|\s)(?:python(?:\.exe)?\s+([A-Za-z0-9_./\\\-]+\.py))", re.IGNORECASE
)
UVICORN_RUN_RE = re.compile(
    r"uvicorn\.run\(\s*[\"']([\w.]+):(\w+)[\"'].*?host\s*=\s*[\"']([^\"']+)[\"'].*?port\s*=\s*(\d+)",
    re.DOTALL,
)
FRAMEWORK_HINTS = {
    "fastapi": "FastAPI",
    "flask": "Flask",
    "django": "Django",
    "uvicorn": "Uvicorn (ASGI server)",
    "gunicorn": "Gunicorn (WSGI server)",
}


class NexusInfo:
    def __init__(self):
        self.folder = ""
        self.exists = False
        self.requirements_files = []          # list[Path] (relative display)
        self.requirements = []                # list[RequirementSpec]
        self.pyproject_exists = False
        self.database_folder_exists = False
        self.backend_folder_exists = False
        self.seed_script_relpath = None        # e.g. "database.seed" module path
        self.seed_script_path = None
        self.migration_configs = []            # list of dicts: {ini_path, script_location, needs_catalog_flag, catalogs}
        self.env_exists = False
        self.env_example_exists = False
        self.env_path = None
        self.env_example_path = None
        self.readme_path = None
        self.setup_md_path = None
        self.frameworks_detected = set()
        self.entry_point_candidates = []       # list[Path] likely startup scripts
        self.startup_command = None            # list[str] relative args, e.g. ["run_backend.py"]
        self.startup_evidence = []             # list[str] human-readable evidence
        self.startup_host = None
        self.startup_port = None
        self.startup_url_hint = None
        self.asyncpg_requirement = None        # RequirementSpec or None
        self.greenlet_requirement = None       # RequirementSpec or None
        self.greenlet_needed_transitively = False
        self.duplicate_requirement_names = []
        self.errors = []
        self.warnings = []
        self.ambiguous_roots = []              # candidate project roots if root unclear

    def requirement_summary_lines(self):
        lines = []
        for r in self.requirements:
            lines.append(f"{r.name}{r.specifier}  (from {r.source_file})")
        return lines


def _parse_requirements_file(path: Path, display_name: str):
    specs = []
    text = read_text_safe(path)
    if text is None:
        return specs
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        m = REQUIREMENT_LINE_RE.match(line)
        if not m:
            continue
        name = m.group(1)
        specifier = (m.group(3) or "").strip()
        specs.append(RequirementSpec(name.lower(), specifier, line, display_name))
    return specs


def _detect_migration_configs(folder: Path):
    """Find alembic-style ini files anywhere at the top level of the project,
    and inspect their paired env.py for a required '-x catalog=' flag."""
    configs = []
    try:
        candidates = list(folder.glob("*.ini"))
    except OSError:
        candidates = []
    for ini in candidates:
        text = read_text_safe(ini) or ""
        if not ALEMBIC_SECTION_RE.search(text):
            continue
        script_location = None
        m = re.search(r"script_location\s*=\s*(.+)", text)
        if m:
            script_location = m.group(1).strip()
        needs_catalog_flag = False
        catalogs = []
        env_py = None
        if script_location:
            # script_location may use %(here)s style tokens.
            loc = script_location.replace("%(here)s", str(folder)).strip()
            candidate_dir = Path(loc) if os.path.isabs(loc) else folder / loc.lstrip("/\\ ")
            env_py_path = candidate_dir / "env.py"
            if env_py_path.is_file():
                env_py = env_py_path
                env_text = read_text_safe(env_py_path) or ""
                if "-x catalog" in env_text or "_x_args" in env_text or "get_x_argument" in env_text:
                    needs_catalog_flag = True
                    cat_match = re.search(r"CatalogCode\)\s*:", env_text)
                    enum_match = re.findall(r"catalog[:=]\s*<?(\w+)>?", env_text, re.IGNORECASE)
                    # Best-effort: look for an enum-like class named CatalogCode elsewhere.
        configs.append({
            "ini_path": ini,
            "script_location": script_location,
            "env_py": env_py,
            "needs_catalog_flag": needs_catalog_flag,
            "catalogs": catalogs,
        })
    return configs


def _discover_catalog_codes(folder: Path):
    """Best-effort discovery of catalog codes (e.g. vms/dayshift/complete/esnf)
    by scanning for a CatalogCode-like enum in the backend source. Falls back
    to an empty list if nothing is found (the user will then be prompted to
    type the catalog name shown in the migration evidence)."""
    codes = []
    try:
        for py_file in folder.rglob("*.py"):
            if ".venv" in py_file.parts or "__pycache__" in py_file.parts:
                continue
            text = read_text_safe(py_file, max_bytes=20_000) or ""
            if "class CatalogCode" in text:
                found = re.findall(r"^\s*(\w+)\s*=\s*[\"'](\w+)[\"']", text, re.MULTILINE)
                if found:
                    codes = [v for _, v in found]
                    return codes
    except OSError:
        pass
    return codes


def inspect_nexus_folder(folder_str: str, log=lambda *a, **k: None) -> NexusInfo:
    info = NexusInfo()
    info.folder = folder_str
    folder = Path(folder_str) if folder_str else None

    if not folder or not folder.is_dir():
        info.errors.append("Selected Nexus folder does not exist.")
        return info
    info.exists = True

    # --- requirements.txt (search root, then up to 2 child levels if absent) ---
    root_req = folder / "requirements.txt"
    if root_req.is_file():
        info.requirements_files.append(root_req)
    else:
        found_roots = set()
        try:
            for depth1 in folder.iterdir():
                if depth1.is_dir() and (depth1 / "requirements.txt").is_file():
                    found_roots.add(depth1)
                elif depth1.is_dir():
                    try:
                        for depth2 in depth1.iterdir():
                            if depth2.is_dir() and (depth2 / "requirements.txt").is_file():
                                found_roots.add(depth2)
                    except OSError:
                        pass
        except OSError:
            pass
        if len(found_roots) == 1:
            nested = next(iter(found_roots))
            info.warnings.append(
                f"requirements.txt not found in the selected folder; found a likely project "
                f"root one level down at: {nested}"
            )
            info.ambiguous_roots = [nested]
        elif len(found_roots) > 1:
            info.errors.append(
                "requirements.txt not found directly in the selected folder, and multiple "
                "possible project roots were found nearby. Please select the exact project "
                "folder."
            )
            info.ambiguous_roots = sorted(found_roots)
        else:
            info.errors.append("requirements.txt was not found in the selected folder.")

    if info.requirements_files:
        for rf in info.requirements_files:
            info.requirements.extend(_parse_requirements_file(rf, rf.name))
        # look for other *requirements*.txt style files alongside it
        try:
            for extra in folder.glob("*requirements*.txt"):
                if extra not in info.requirements_files:
                    info.requirements_files.append(extra)
                    info.requirements.extend(_parse_requirements_file(extra, extra.name))
        except OSError:
            pass

    # duplicate requirement detection
    seen_names = {}
    for r in info.requirements:
        seen_names.setdefault(r.name, []).append(r)
    for name, specs in seen_names.items():
        if len(specs) > 1:
            unique_specs = {s.specifier for s in specs}
            if len(unique_specs) > 1:
                info.duplicate_requirement_names.append(name)
                info.warnings.append(
                    f"'{name}' appears multiple times with different version constraints: "
                    + ", ".join(f"{s.specifier or '(unpinned)'} [{s.source_file}]" for s in specs)
                )

    for r in info.requirements:
        if r.name == "asyncpg":
            info.asyncpg_requirement = r
        if r.name == "greenlet":
            info.greenlet_requirement = r

    # --- pyproject.toml ---
    info.pyproject_exists = (folder / "pyproject.toml").is_file()

    # --- database / backend folders ---
    info.database_folder_exists = (folder / "database").is_dir()
    info.backend_folder_exists = (folder / "backend").is_dir()

    # --- seed script ---
    seed_candidates = [
        folder / "database" / "seed.py",
        folder / "backend" / "database" / "seed.py",
    ]
    for c in seed_candidates:
        if c.is_file():
            info.seed_script_path = c
            try:
                rel = c.relative_to(folder).with_suffix("")
                info.seed_script_relpath = str(rel).replace(os.sep, ".")
            except ValueError:
                info.seed_script_relpath = "database.seed"
            break

    # --- migrations ---
    info.migration_configs = _detect_migration_configs(folder)
    if info.migration_configs:
        codes = _discover_catalog_codes(folder)
        for cfg in info.migration_configs:
            if cfg["needs_catalog_flag"] and codes:
                cfg["catalogs"] = codes

    # --- .env / .env.example ---
    env_p = folder / ".env"
    env_ex_p = folder / ".env.example"
    info.env_exists = env_p.is_file()
    info.env_example_exists = env_ex_p.is_file()
    if info.env_exists:
        info.env_path = env_p
    if info.env_example_exists:
        info.env_example_path = env_ex_p

    # --- README / SETUP docs ---
    for candidate in ["README.md", "SETUP.md", "Readme.md", "readme.md"]:
        p = folder / candidate
        if p.is_file():
            if "readme" in candidate.lower():
                info.readme_path = info.readme_path or p
            else:
                info.setup_md_path = info.setup_md_path or p

    doc_text = ""
    for p in [info.readme_path, info.setup_md_path]:
        if p:
            doc_text += (read_text_safe(p) or "") + "\n"

    # --- framework detection (requirements + a light source scan) ---
    for r in info.requirements:
        if r.name in FRAMEWORK_HINTS:
            info.frameworks_detected.add(FRAMEWORK_HINTS[r.name])

    # --- entry point candidates ---
    likely_names = ["run_backend.py", "main.py", "app.py", "manage.py", "run.py", "server.py", "wsgi.py", "asgi.py"]
    for name in likely_names:
        p = folder / name
        if p.is_file():
            info.entry_point_candidates.append(p)

    # --- startup command determination, in order of evidence strength ---
    evidence = []
    chosen = None

    # 1. A Windows launcher batch file (start.bat / run.bat / launch.bat).
    bat_candidates = list(folder.glob("*.bat"))
    bat_script_ref = None
    for bat in bat_candidates:
        text = read_text_safe(bat) or ""
        m = re.search(r"\.venv\\Scripts\\python(?:\.exe)?\s+([A-Za-z0-9_.\\/\-]+\.py)", text)
        if m:
            bat_script_ref = m.group(1)
            evidence.append(f"Found launcher script '{bat.name}' invoking: .venv\\Scripts\\python {bat_script_ref}")
            break
    if bat_script_ref:
        script_path = folder / bat_script_ref
        if script_path.is_file():
            chosen = [bat_script_ref]

    # 2. IDE/editor launch configuration (e.g. .claude/launch.json, .vscode/launch.json).
    if not chosen:
        for cfg_glob in [".claude/launch.json", ".vscode/launch.json"]:
            p = folder / cfg_glob
            if p.is_file():
                text = read_text_safe(p) or ""
                try:
                    parsed = json.loads(text)
                    configs = parsed.get("configurations", []) if isinstance(parsed, dict) else []
                    for c in configs:
                        args = c.get("runtimeArgs") or c.get("args") or []
                        for a in args:
                            if isinstance(a, str) and a.endswith(".py") and (folder / a).is_file():
                                chosen = [a]
                                evidence.append(f"Found launch configuration '{cfg_glob}' referencing: {a}")
                                break
                        if chosen:
                            break
                except json.JSONDecodeError:
                    pass
            if chosen:
                break

    # 3. Documentation (README/SETUP.md) code fences mentioning `python X.py`.
    if not chosen and doc_text:
        for m in STARTUP_HINT_RE.finditer(doc_text):
            candidate_rel = m.group(1)
            candidate_path = folder / candidate_rel
            if candidate_path.is_file():
                chosen = [candidate_rel]
                evidence.append(f"Found documentation reference to: python {candidate_rel}")
                break

    # 4. A single unambiguous entry point file among common names.
    if not chosen and len(info.entry_point_candidates) == 1:
        rel = info.entry_point_candidates[0].name
        chosen = [rel]
        evidence.append(f"Found a single likely entry point script: {rel}")

    if chosen:
        info.startup_command = chosen
        info.startup_evidence = evidence
        # Try to pull host/port from the chosen script for display purposes only.
        target_path = folder / chosen[0]
        script_text = read_text_safe(target_path) or ""
        um = UVICORN_RUN_RE.search(script_text)
        if um:
            info.startup_host = um.group(3)
            info.startup_port = um.group(4)
    else:
        info.warnings.append(
            "Could not determine a startup command with confidence from repository evidence. "
            "The user must specify it manually before 'Start Nexus' is enabled."
        )

    # portal / app URL hint from docs (first http://127.0.0.1 or http://localhost reference)
    url_m = re.search(r"https?://(?:127\.0\.0\.1|localhost)(?::\d+)?(?:/[^\s`\"')]*)?", doc_text)
    if url_m:
        info.startup_url_hint = url_m.group(0)
    elif info.startup_host and info.startup_port:
        info.startup_url_hint = f"http://{info.startup_host}:{info.startup_port}"

    # --- greenlet transitive need detection ---
    if info.backend_folder_exists or True:
        try:
            for py_file in folder.rglob("*.py"):
                if ".venv" in py_file.parts or "__pycache__" in py_file.parts:
                    continue
                text = read_text_safe(py_file, max_bytes=20_000) or ""
                if "create_async_engine" in text or "AsyncSession" in text or "async_sessionmaker" in text:
                    info.greenlet_needed_transitively = True
                    break
        except OSError:
            pass

    return info


# --------------------------------------------------------------------------
# Virtual environment management
# --------------------------------------------------------------------------

def venv_python_path(nexus_folder: Path) -> Path:
    if IS_WINDOWS:
        return nexus_folder / ".venv" / "Scripts" / "python.exe"
    return nexus_folder / ".venv" / "bin" / "python"


def inspect_existing_venv(nexus_folder: Path, portable_python_info: PythonInfo, log):
    """Returns dict describing venv health/compatibility, or None if absent."""
    venv_dir = nexus_folder / ".venv"
    vpy = venv_python_path(nexus_folder)
    if not venv_dir.is_dir():
        return None

    result = {
        "exists": True,
        "python_exe_exists": vpy.is_file(),
        "version": "",
        "base_prefix": "",
        "is_64bit": False,
        "pip_ok": False,
        "belongs_to_folder": False,
        "compatible": False,
        "issues": [],
    }

    if not vpy.is_file():
        result["issues"].append(".venv exists but .venv\\Scripts\\python.exe is missing.")
        return result

    rc, out, err = run_subprocess([str(vpy), "--version"], timeout=20)
    if rc != 0:
        result["issues"].append(f"Could not run the existing venv's python.exe: {err.strip()}")
        return result
    result["version"] = (out + err).strip()

    rc, out, err = run_subprocess(
        [str(vpy), "-c", "import sys; print(sys.executable); import platform; print(platform.architecture()[0])"],
        timeout=20,
    )
    if rc == 0:
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        if lines:
            sys_exec = lines[0]
            try:
                result["belongs_to_folder"] = str(nexus_folder.resolve()) in str(Path(sys_exec).resolve())
            except OSError:
                result["belongs_to_folder"] = str(nexus_folder) in sys_exec
        if len(lines) > 1:
            result["is_64bit"] = "64" in lines[1]

    # pyvenv.cfg comparison
    cfg_path = venv_dir / "pyvenv.cfg"
    base_exec = ""
    if cfg_path.is_file():
        cfg_text = read_text_safe(cfg_path) or ""
        m = re.search(r"^\s*(?:base-executable|home)\s*=\s*(.+)$", cfg_text, re.MULTILINE)
        if m:
            base_exec = m.group(1).strip()
        result["base_prefix"] = base_exec

    rc, out, err = run_subprocess([str(vpy), "-m", "pip", "--version"], timeout=30)
    result["pip_ok"] = rc == 0

    version_matches = True
    if portable_python_info.version and result["version"]:
        version_matches = portable_python_info.version.split()[-1] == result["version"].split()[-1]
        if not version_matches:
            result["issues"].append(
                f"Existing venv Python version ({result['version']}) does not match the "
                f"selected portable Python ({portable_python_info.version})."
            )

    if portable_python_info.is_64bit and not result["is_64bit"]:
        result["issues"].append("Existing venv is not 64-bit but the selected portable Python is.")

    if not result["pip_ok"]:
        result["issues"].append("pip is not usable inside the existing venv.")

    if not result["belongs_to_folder"]:
        result["issues"].append(
            "The existing venv's sys.executable does not appear to point inside the selected Nexus folder "
            "(it may have been copied from elsewhere)."
        )

    result["compatible"] = not result["issues"]
    return result


def backup_existing_venv(nexus_folder: Path, log):
    venv_dir = nexus_folder / ".venv"
    if not venv_dir.exists():
        return True, None
    backup_name = f".venv_backup_{now_stamp()}"
    backup_path = nexus_folder / backup_name
    try:
        venv_dir.rename(backup_path)
        log("info", f"Renamed existing .venv to {backup_name}")
        return True, backup_path
    except OSError as e:
        log("error", f"Could not rename existing .venv: {e}")
        return False, None


def delete_existing_venv(nexus_folder: Path, log):
    venv_dir = nexus_folder / ".venv"
    if not venv_dir.exists():
        return True
    try:
        shutil.rmtree(venv_dir)
        log("info", "Deleted existing .venv.")
        return True
    except OSError as e:
        log("error", f"Could not delete existing .venv: {e}")
        return False


def backup_requirements_file(req_file: Path, log):
    """Timestamped backup before any modification, per spec: never touch
    requirements.txt without a backup the user can restore from."""
    backup_path = req_file.with_name(f"{req_file.name}.backup_{now_stamp()}")
    try:
        shutil.copyfile(req_file, backup_path)
        log("info", f"Backed up {req_file.name} to {backup_path.name}")
        return backup_path
    except OSError as e:
        log("error", f"Could not back up {req_file.name}: {e}")
        return None


def preview_requirement_line_change(req_file: Path, package_name: str, new_specifier: str):
    """Return (old_line, new_line, line_index) for the single matching
    requirement line, without writing anything. Preserves whatever the
    original line's trailing comment/extras looked like as much as possible
    by only touching the version-specifier portion."""
    raw_bytes = req_file.read_bytes()
    # Detect line ending style to preserve it exactly.
    newline = "\r\n" if b"\r\n" in raw_bytes else "\n"
    text = raw_bytes.decode("utf-8", errors="replace")
    lines = text.split(newline)
    pattern = re.compile(
        r"^(\s*" + re.escape(package_name) + r")(\[[^\]]*\])?\s*[<>=!~][^#\n]*(\s*#.*)?$",
        re.IGNORECASE,
    )
    for i, line in enumerate(lines):
        if pattern.match(line.strip()) and line.strip().lower().startswith(package_name.lower()):
            m = pattern.match(line.strip())
            extras = m.group(2) or ""
            comment = m.group(3) or ""
            new_line = f"{package_name}{extras}{new_specifier}{comment}"
            return line, new_line, i, newline
    return None, None, -1, newline


def apply_requirement_line_change(req_file: Path, line_index: int, new_line: str, newline: str, log):
    """Modify only the single targeted line, preserving encoding/line endings."""
    raw_bytes = req_file.read_bytes()
    text = raw_bytes.decode("utf-8", errors="replace")
    lines = text.split(newline)
    if line_index < 0 or line_index >= len(lines):
        log("error", "Could not locate the line to modify (file may have changed).")
        return False
    lines[line_index] = new_line
    try:
        req_file.write_text(newline.join(lines), encoding="utf-8", newline="")
        log("success", f"Updated {req_file.name}: {new_line.strip()}")
        return True
    except OSError as e:
        log("error", f"Could not write {req_file.name}: {e}")
        return False


def create_modified_requirements_copy(req_file: Path, package_name: str, new_specifier: str, log):
    """Full guarded workflow: backup -> preview -> caller confirms -> apply.
    This function only backs up and previews; the caller applies after the
    user explicitly approves the exact change shown."""
    old_line, new_line, idx, newline = preview_requirement_line_change(req_file, package_name, new_specifier)
    if old_line is None:
        log("error", f"Could not find a '{package_name}' line in {req_file.name} to modify.")
        return None
    backup_path = backup_requirements_file(req_file, log)
    if backup_path is None:
        return None
    return {
        "req_file": req_file,
        "backup_path": backup_path,
        "old_line": old_line,
        "new_line": new_line,
        "line_index": idx,
        "newline": newline,
    }


def create_venv(portable_python: str, nexus_folder: Path, log):
    log("info", f"Creating virtual environment: {nexus_folder / '.venv'}")
    args = [portable_python, "-m", "venv", str(nexus_folder / ".venv")]
    log("cmd", format_cmd(args))
    rc, out, err = run_subprocess(args, cwd=nexus_folder, timeout=300)
    if out.strip():
        log("out", out.strip())
    if err.strip():
        log("out", err.strip())
    if rc != 0:
        log("error", f"venv creation failed (exit code {rc}).")
        return False
    vpy = venv_python_path(nexus_folder)
    if not vpy.is_file():
        log("error", "venv creation reported success but python.exe was not found afterwards.")
        return False
    rc, out, err = run_subprocess([str(vpy), "--version"], timeout=20)
    if rc != 0:
        log("error", f"Could not verify the new venv's Python: {err.strip()}")
        return False
    log("success", f"Virtual environment created and verified: {out.strip() or err.strip()}")
    rc, out, err = run_subprocess([str(vpy), "-m", "pip", "--version"], timeout=30)
    if rc != 0:
        log("error", f"pip is not available in the new venv: {err.strip()}")
        return False
    log("success", f"pip available in venv: {out.strip()}")
    return True


# --------------------------------------------------------------------------
# Package installation
# --------------------------------------------------------------------------

def upgrade_pip_tools(vpy: Path, log):
    args = [str(vpy), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"]
    log("cmd", format_cmd(args))
    rc, out, err = run_subprocess(args, timeout=300)
    if out.strip():
        log("out", out.strip())
    if err.strip():
        log("out", err.strip())
    if rc != 0:
        log("error", f"Failed to upgrade pip/setuptools/wheel (exit code {rc}).")
        return False
    log("success", "pip, setuptools, and wheel upgraded.")
    return True


def install_critical_binary_deps(vpy: Path, nexus_info: NexusInfo, log):
    """Install asyncpg / greenlet from binary wheels only, using whatever
    version constraint the repository specifies (falling back to a safe
    default for asyncpg only when the repo leaves it unpinned)."""
    targets = []

    if nexus_info.asyncpg_requirement:
        spec = nexus_info.asyncpg_requirement.specifier or f">={DEFAULT_MIN_ASYNCPG_FOR_PY313}"
        targets.append(f"asyncpg{spec}")
    # If asyncpg isn't required at all, skip it entirely - do not force it on
    # a project that doesn't use PostgreSQL.

    if nexus_info.greenlet_requirement:
        spec = nexus_info.greenlet_requirement.specifier
        targets.append(f"greenlet{spec}")
    elif nexus_info.greenlet_needed_transitively:
        targets.append("greenlet")

    if not targets:
        log("info", "No compiled asyncpg/greenlet dependency detected as required; skipping binary pre-install step.")
        return True

    args = [str(vpy), "-m", "pip", "install", "--only-binary=:all:"] + targets
    log("cmd", format_cmd(args))
    rc, out, err = run_subprocess(args, timeout=300)
    if out.strip():
        log("out", out.strip())
    if err.strip():
        log("out", err.strip())
    if rc != 0:
        log("error", f"Could not install {', '.join(targets)} from precompiled wheels (exit code {rc}).")
        log("error",
            "This usually means no compatible wheel exists for this Python version/architecture, "
            "and pip refused to compile from source (Visual C++ Build Tools are not available/required "
            "for this setup). Consider selecting a different portable Python version, or check whether "
            "the pinned version in requirements.txt supports this Python version.")
        return False
    log("success", f"Installed from binary wheels: {', '.join(targets)}")
    return True


def install_requirements(vpy: Path, nexus_info: NexusInfo, nexus_folder: Path, binary_only: bool, log):
    if not nexus_info.requirements_files:
        log("error", "No requirements.txt found; cannot install dependencies.")
        return False
    req_file = nexus_info.requirements_files[0]
    args = [str(vpy), "-m", "pip", "install"]
    if binary_only:
        args.append("--only-binary=:all:")
    args += ["-r", str(req_file)]
    log("cmd", format_cmd(args))
    rc, out, err = run_subprocess(args, cwd=nexus_folder, timeout=1800)
    if out.strip():
        log("out", out.strip())
    if err.strip():
        log("out", err.strip())
    if rc != 0:
        log("error", f"pip install -r {req_file.name} failed (exit code {rc}).")
        if binary_only:
            log("error", "Binary-only mode failed - at least one package has no precompiled wheel for this "
                          "Python version/architecture.")
        return False
    log("success", f"Installed dependencies from {req_file.name}.")
    return True


def run_pip_check(vpy: Path, log):
    args = [str(vpy), "-m", "pip", "check"]
    log("cmd", format_cmd(args))
    rc, out, err = run_subprocess(args, timeout=120)
    text = (out + err).strip()
    if text:
        log("out", text)
    if rc != 0:
        log("error", "pip check reported broken/incompatible dependencies (see above).")
        return False
    log("success", "pip check passed: no broken dependencies.")
    return True


def import_check(vpy: Path, module_name: str, log):
    args = [str(vpy), "-c", f"import {module_name}; print(getattr({module_name}, '__version__', 'unknown'))"]
    rc, out, err = run_subprocess(args, timeout=30)
    if rc == 0:
        ver = out.strip()
        log("success", f"import {module_name}: OK (version {ver})")
        return True, ver
    else:
        log("error", f"import {module_name}: FAILED - {err.strip()}")
        return False, None


# --------------------------------------------------------------------------
# Database seed / migrations
# --------------------------------------------------------------------------

def detect_database_kind(nexus_folder: Path, nexus_info: NexusInfo):
    """Best-effort, evidence-based DB engine detection from requirements and
    any config/env-example content. Never guesses beyond what's found."""
    kinds = set()
    req_names = {r.name for r in nexus_info.requirements}
    if "asyncpg" in req_names or "psycopg2" in req_names or "psycopg2-binary" in req_names or "psycopg" in req_names:
        kinds.add("PostgreSQL")
    if "aiosqlite" in req_names or "sqlite3" in req_names:
        kinds.add("SQLite")
    if "pyodbc" in req_names or "pymssql" in req_names:
        kinds.add("SQL Server")
    if "pymysql" in req_names or "mysqlclient" in req_names or "aiomysql" in req_names:
        kinds.add("MySQL")
    # scan .env.example / .env for DATABASE_URL hints
    for p in [nexus_info.env_path, nexus_info.env_example_path]:
        if p:
            text = read_text_safe(p) or ""
            if "sqlite" in text.lower():
                kinds.add("SQLite")
            if "postgres" in text.lower():
                kinds.add("PostgreSQL")
            if "mysql" in text.lower():
                kinds.add("MySQL")
            if "mssql" in text.lower() or "sqlserver" in text.lower():
                kinds.add("SQL Server")
    return sorted(kinds) if kinds else ["Unknown (no clear evidence found)"]


def run_seed(vpy: Path, nexus_info: NexusInfo, nexus_folder: Path, log):
    if not nexus_info.seed_script_relpath:
        log("error", "No seed script detected; cannot initialize the database.")
        return False
    args = [str(vpy), "-m", nexus_info.seed_script_relpath]
    log("cmd", format_cmd(args) + f"   (cwd={nexus_folder})")
    rc, out, err = run_subprocess(args, cwd=nexus_folder, timeout=600)
    if out.strip():
        log("out", out.strip())
    if err.strip():
        log("out", err.strip())
    if rc != 0:
        log("error", f"Database seed failed (exit code {rc}). Logs preserved above; not retrying automatically.")
        return False
    log("success", "Database seed completed.")
    return True


def run_migration(vpy_ignored, alembic_python: Path, ini_cfg: dict, catalog: str, nexus_folder: Path, log):
    """Run alembic upgrade head for a given ini config, using the venv's
    python -m alembic invocation (avoids depending on an 'alembic.exe' shim
    being on PATH, which portable environments often lack)."""
    ini_path = ini_cfg["ini_path"]
    args = [str(alembic_python), "-m", "alembic", "-c", str(ini_path)]
    if ini_cfg.get("needs_catalog_flag"):
        if not catalog:
            log("error", "This migration configuration requires a catalog name (-x catalog=<name>) but none was provided.")
            return False
        args += ["-x", f"catalog={catalog}"]
    args += ["upgrade", "head"]
    log("cmd", format_cmd(args) + f"   (cwd={nexus_folder})")
    rc, out, err = run_subprocess(args, cwd=nexus_folder, timeout=300)
    if out.strip():
        log("out", out.strip())
    if err.strip():
        log("out", err.strip())
    if rc != 0:
        log("error", f"Migration failed for {ini_path.name}"
                      + (f" (catalog={catalog})" if catalog else "") + f" (exit code {rc}).")
        return False
    log("success", f"Migration applied for {ini_path.name}" + (f" (catalog={catalog})" if catalog else "") + ".")
    return True


# --------------------------------------------------------------------------
# .env handling
# --------------------------------------------------------------------------

def create_env_from_example(nexus_info: NexusInfo, log):
    if not nexus_info.env_example_path:
        log("error", "No .env.example found to copy from.")
        return False
    dest = Path(nexus_info.folder) / ".env"
    if dest.exists():
        log("warn", ".env already exists; not overwriting.")
        return False
    try:
        shutil.copyfile(nexus_info.env_example_path, dest)
        log("success", f"Created .env from {nexus_info.env_example_path.name}.")
        # Identify which fields look empty (KEY= with nothing after it)
        text = read_text_safe(dest) or ""
        empty_fields = re.findall(r"^([A-Za-z0-9_]+)\s*=\s*$", text, re.MULTILINE)
        if empty_fields:
            log("warn", "The following fields in .env are empty and may need to be filled in manually: "
                         + ", ".join(empty_fields))
        return True
    except OSError as e:
        log("error", f"Could not create .env: {e}")
        return False


# --------------------------------------------------------------------------
# Configuration copy tool (between Nexus versions)
# --------------------------------------------------------------------------

COPY_CANDIDATE_NAMES = [".env", "config", "certs", "certificates"]
NEVER_COPY_NAMES = {".venv", "__pycache__", ".pytest_cache"}
NEVER_COPY_SUFFIXES = {".pyc"}


def plan_config_copy(source_folder: Path, dest_folder: Path):
    """Return a plan (list of dicts) describing what *could* be copied, for
    the user to review and approve - nothing is copied here."""
    plan = []
    for name in COPY_CANDIDATE_NAMES:
        src = source_folder / name
        if not src.exists():
            continue
        if src.name in NEVER_COPY_NAMES:
            continue
        dst = dest_folder / name
        plan.append({
            "name": name,
            "source": src,
            "destination": dst,
            "dest_exists": dst.exists(),
        })
    return plan


def execute_config_copy(plan_item, log):
    src, dst = plan_item["source"], plan_item["destination"]
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True,
                             ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
        else:
            shutil.copyfile(src, dst)
        log("success", f"Copied {plan_item['name']} -> {dst}")
        return True
    except OSError as e:
        log("error", f"Failed to copy {plan_item['name']}: {e}")
        return False


# --------------------------------------------------------------------------
# GUI application
# --------------------------------------------------------------------------

LOG_TAGS = {
    "info": ("INFO", "#1a1a1a"),
    "cmd": ("CMD ", "#5b3fa0"),
    "out": ("     ", "#444444"),
    "warn": ("WARN", "#a86a00"),
    "error": ("ERROR", "#b00020"),
    "success": ("OK  ", "#0a7a2f"),
}


class BackgroundTask:
    """Runs a function in a background thread and funnels its log messages
    and completion result back to the Tk main loop via a thread-safe queue."""

    def __init__(self, app, name, fn, on_done=None):
        self.app = app
        self.name = name
        self.fn = fn
        self.on_done = on_done
        self.q = queue.Queue()
        self.thread = None

    def log(self, level, text):
        self.q.put(("log", level, text))

    def progress(self, value):
        self.q.put(("progress", value))

    def status(self, text):
        self.q.put(("status", text))

    def start(self):
        self.app.begin_task(self.name)

        def runner():
            result = None
            try:
                result = self.fn(self.log)
            except Exception:
                self.log("error", "Unexpected error:\n" + traceback.format_exc())
                result = False
            finally:
                self.q.put(("done", result))

        self.thread = threading.Thread(target=runner, daemon=True)
        self.thread.start()
        self.app.root.after(80, self._poll)

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    _, level, text = item
                    self.app.append_log(level, text)
                elif kind == "progress":
                    self.app.set_progress(item[1])
                elif kind == "status":
                    self.app.set_status(item[1])
                elif kind == "done":
                    self.app.end_task(self.name, item[1], self.on_done)
                    return
        except queue.Empty:
            pass
        self.app.root.after(80, self._poll)


class NexusSetupManagerApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.minsize(980, 760)

        self.settings = SettingsManager()
        self.python_info = PythonInfo()
        self.nexus_info = NexusInfo()
        self.venv_status = None
        self.deps_installed_ok = False
        self.task_running = False
        self.current_migration_ini = None

        self.python_path_var = tk.StringVar(value=self.settings.get("python_exe", ""))
        self.nexus_path_var = tk.StringVar(value=self.settings.get("nexus_folder", ""))
        self.install_mode_var = tk.StringVar(value=self.settings.get("install_mode", "normal"))
        self.status_var = tk.StringVar(value="Ready.")
        self.operation_var = tk.StringVar(value="No operation running.")
        self.python_status_var = tk.StringVar(value="Not validated.")
        self.nexus_status_var = tk.StringVar(value="Not validated.")

        geometry = self.settings.get("window_geometry", "")
        if geometry:
            try:
                self.root.geometry(geometry)
            except tk.TclError:
                pass

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_exit)

        # Auto-load and re-validate any saved paths, without assuming they
        # still exist.
        self.root.after(200, self._auto_validate_saved_paths)

    # ---------------------------------------------------------- UI building

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        nb = ttk.Notebook(outer)
        nb.pack(fill="both", expand=True)

        setup_tab = ttk.Frame(nb, padding=8)
        nb.add(setup_tab, text="Setup")
        self._build_setup_tab(setup_tab)

        env_tab = ttk.Frame(nb, padding=8)
        nb.add(env_tab, text="Environment Info")
        self._build_env_info_tab(env_tab)

        log_tab = ttk.Frame(nb, padding=8)
        nb.add(log_tab, text="Progress / Log")
        self._build_log_tab(log_tab)

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(8, 0))
        ttk.Label(bottom, textvariable=self.status_var, relief="sunken", anchor="w").pack(fill="x")

    def _build_setup_tab(self, parent):
        # --- A. Portable Python section ---
        py_frame = ttk.LabelFrame(parent, text="Portable Python", padding=8)
        py_frame.pack(fill="x", pady=4)

        ttk.Label(py_frame, text="Python executable:").grid(row=0, column=0, sticky="w")
        ttk.Entry(py_frame, textvariable=self.python_path_var, width=70).grid(row=0, column=1, sticky="we", padx=4)
        py_frame.columnconfigure(1, weight=1)

        btns = ttk.Frame(py_frame)
        btns.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(btns, text="Browse for Python...", command=self.browse_python_exe).pack(side="left", padx=2)
        ttk.Button(btns, text="Browse for Python Folder...", command=self.browse_python_folder).pack(side="left", padx=2)
        self.btn_validate_python = ttk.Button(btns, text="Validate Python", command=self.action_validate_python)
        self.btn_validate_python.pack(side="left", padx=2)

        ttk.Label(py_frame, textvariable=self.python_status_var, foreground="#555555").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )

        # --- B. Nexus project section ---
        nx_frame = ttk.LabelFrame(parent, text="Nexus Project Folder", padding=8)
        nx_frame.pack(fill="x", pady=4)

        ttk.Label(nx_frame, text="Nexus folder:").grid(row=0, column=0, sticky="w")
        ttk.Entry(nx_frame, textvariable=self.nexus_path_var, width=70).grid(row=0, column=1, sticky="we", padx=4)
        nx_frame.columnconfigure(1, weight=1)

        nx_btns = ttk.Frame(nx_frame)
        nx_btns.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(nx_btns, text="Browse for Nexus...", command=self.browse_nexus_folder).pack(side="left", padx=2)
        self.btn_validate_nexus = ttk.Button(nx_btns, text="Validate Nexus Folder", command=self.action_validate_nexus)
        self.btn_validate_nexus.pack(side="left", padx=2)

        ttk.Label(nx_frame, textvariable=self.nexus_status_var, foreground="#555555").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )

        # --- D. Action buttons ---
        act_frame = ttk.LabelFrame(parent, text="Actions", padding=8)
        act_frame.pack(fill="x", pady=4)

        row1 = ttk.Frame(act_frame)
        row1.pack(fill="x", pady=2)
        self.btn_validate_all = ttk.Button(row1, text="Validate Selections", command=self.action_validate_all)
        self.btn_validate_all.pack(side="left", padx=2)
        self.btn_env_setup = ttk.Button(row1, text="Create / Repair Environment", command=self.action_create_env)
        self.btn_env_setup.pack(side="left", padx=2)
        self.btn_install_deps = ttk.Button(row1, text="Install / Update Dependencies", command=self.action_install_deps)
        self.btn_install_deps.pack(side="left", padx=2)
        self.btn_full_setup = ttk.Button(row1, text="Full Setup", command=self.action_full_setup)
        self.btn_full_setup.pack(side="left", padx=2)

        row2 = ttk.Frame(act_frame)
        row2.pack(fill="x", pady=2)
        self.btn_init_db = ttk.Button(row2, text="Initialize Database", command=self.action_init_database)
        self.btn_init_db.pack(side="left", padx=2)
        self.btn_migrations = ttk.Button(row2, text="Run Database Migrations", command=self.action_run_migrations)
        self.btn_migrations.pack(side="left", padx=2)
        self.btn_start_nexus = ttk.Button(row2, text="Start Nexus", command=self.action_start_nexus)
        self.btn_start_nexus.pack(side="left", padx=2)

        row3 = ttk.Frame(act_frame)
        row3.pack(fill="x", pady=2)
        ttk.Button(row3, text="Open Nexus Folder", command=self.action_open_nexus_folder).pack(side="left", padx=2)
        ttk.Button(row3, text="Open Log Folder", command=self.action_open_log_folder).pack(side="left", padx=2)
        ttk.Button(row3, text="Reset Saved Paths", command=self.action_reset_paths).pack(side="left", padx=2)
        ttk.Button(row3, text="Exit", command=self.on_exit).pack(side="left", padx=2)

        mode_frame = ttk.Frame(act_frame)
        mode_frame.pack(fill="x", pady=(6, 0))
        ttk.Label(mode_frame, text="Dependency install mode:").pack(side="left")
        ttk.Radiobutton(mode_frame, text="Normal (compiled-only-when-needed)", value="normal",
                         variable=self.install_mode_var, command=self._save_paths).pack(side="left", padx=4)
        ttk.Radiobutton(mode_frame, text="Binary-only (safest, may fail if no wheel exists)", value="binary_only",
                         variable=self.install_mode_var, command=self._save_paths).pack(side="left", padx=4)

        # --- E. Progress ---
        prog_frame = ttk.LabelFrame(parent, text="Progress", padding=8)
        prog_frame.pack(fill="x", pady=4)
        ttk.Label(prog_frame, textvariable=self.operation_var).pack(anchor="w")
        self.progress_bar = ttk.Progressbar(prog_frame, mode="determinate", maximum=100)
        self.progress_bar.pack(fill="x", pady=(4, 0))

        self._refresh_action_states()

    def _build_env_info_tab(self, parent):
        self.env_info_text = scrolledtext.ScrolledText(parent, wrap="word", height=30, state="disabled")
        self.env_info_text.pack(fill="both", expand=True)
        ttk.Button(parent, text="Refresh", command=self._render_env_info).pack(anchor="e", pady=(6, 0))

    def _build_log_tab(self, parent):
        self.log_text = scrolledtext.ScrolledText(parent, wrap="word", height=30, state="disabled")
        self.log_text.pack(fill="both", expand=True)
        for level, (_, color) in LOG_TAGS.items():
            self.log_text.tag_configure(level, foreground=color)

        btns = ttk.Frame(parent)
        btns.pack(fill="x", pady=(6, 0))
        ttk.Button(btns, text="Copy Log", command=self.action_copy_log).pack(side="left", padx=2)
        ttk.Button(btns, text="Save Log As...", command=self.action_save_log_as).pack(side="left", padx=2)
        ttk.Button(btns, text="Clear Log", command=self.action_clear_log).pack(side="left", padx=2)

    # ---------------------------------------------------------- logging / task plumbing

    def append_log(self, level, text):
        prefix, _color = LOG_TAGS.get(level, ("    ", "#000000"))
        ts = now_ts()
        self.log_text.configure(state="normal")
        for line in str(text).splitlines() or [""]:
            self.log_text.insert("end", f"[{ts}] {prefix} {line}\n", level)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        # Mirror to a persistent log file so nothing is lost between runs.
        try:
            with open(log_dir() / "session.log", "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {prefix} {text}\n")
        except OSError:
            pass

    def set_status(self, text):
        self.status_var.set(text)

    def set_progress(self, value):
        self.progress_bar["value"] = value

    def begin_task(self, name):
        self.task_running = True
        self.operation_var.set(f"Running: {name}...")
        self.progress_bar["value"] = 5
        self._refresh_action_states()

    def end_task(self, name, result, on_done):
        self.task_running = False
        self.operation_var.set(f"Finished: {name}")
        self.progress_bar["value"] = 100 if result else 0
        if result:
            self.append_log("success", f"'{name}' completed successfully.")
        else:
            self.append_log("error", f"'{name}' did not complete successfully.")
            if self.settings.get("open_logs_on_failure", True):
                pass  # log tab is already in the same window; nothing extra to open
        self._refresh_action_states()
        if on_done:
            on_done(result)

    def run_task(self, name, fn, on_done=None):
        if self.task_running:
            messagebox.showwarning(APP_TITLE, "Another operation is already running. Please wait for it to finish.")
            return
        BackgroundTask(self, name, fn, on_done).start()

    # ---------------------------------------------------------- state / enable-disable logic

    def _refresh_action_states(self):
        running = self.task_running
        python_ok = self.python_info.exists and not self.python_info.errors
        nexus_ok = self.nexus_info.exists and not self.nexus_info.errors
        venv_ok = bool(self.venv_status and self.venv_status.get("python_exe_exists"))
        deps_ok = self.deps_installed_ok

        def state(widget, enabled):
            widget.configure(state=("disabled" if (running or not enabled) else "normal"))

        state(self.btn_validate_python, True)
        state(self.btn_validate_nexus, True)
        state(self.btn_validate_all, True)
        state(self.btn_env_setup, python_ok and nexus_ok)
        state(self.btn_install_deps, python_ok and nexus_ok and venv_ok)
        state(self.btn_full_setup, python_ok and nexus_ok)
        state(self.btn_init_db, nexus_ok and venv_ok and deps_ok and bool(self.nexus_info.seed_script_relpath))
        state(self.btn_migrations, nexus_ok and venv_ok and deps_ok and bool(self.nexus_info.migration_configs))
        state(self.btn_start_nexus, nexus_ok and venv_ok and deps_ok and bool(self.nexus_info.startup_command))

    def _save_paths(self):
        self.settings.set("python_exe", self.python_path_var.get())
        self.settings.set("nexus_folder", self.nexus_path_var.get())
        self.settings.set("install_mode", self.install_mode_var.get())
        try:
            self.settings.set("window_geometry", self.root.geometry())
        except tk.TclError:
            pass
        self.settings.save()

    # ---------------------------------------------------------- browse handlers

    def browse_python_exe(self):
        initial = str(Path(self.python_path_var.get()).parent) if self.python_path_var.get() else str(Path.home())
        path = filedialog.askopenfilename(
            title="Select python.exe",
            initialdir=initial,
            filetypes=[("python.exe", "python.exe"), ("Executable files", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self.python_path_var.set(path)
            self._save_paths()

    def browse_python_folder(self):
        initial = self.python_path_var.get() or str(Path.home())
        folder = filedialog.askdirectory(title="Select portable Python folder", initialdir=initial)
        if not folder:
            return
        candidates = find_python_candidates_in_folder(Path(folder))
        if not candidates:
            messagebox.showerror(
                APP_TITLE,
                "No python.exe was found in that folder (checked python.exe, "
                "python\\python.exe, and Scripts\\python.exe, plus one level of subfolders).",
            )
            return
        if len(candidates) == 1:
            self.python_path_var.set(str(candidates[0]))
            self._save_paths()
            return
        self._show_candidate_picker(
            "Multiple Python executables found",
            [str(c) for c in candidates],
            self.python_path_var,
        )

    def _show_candidate_picker(self, title, options, target_var):
        top = tk.Toplevel(self.root)
        top.title(title)
        top.geometry("560x260")
        ttk.Label(top, text=title, padding=8).pack(anchor="w")
        listbox = tk.Listbox(top, width=80, height=10)
        for o in options:
            listbox.insert("end", o)
        listbox.pack(fill="both", expand=True, padx=8, pady=4)

        def choose():
            sel = listbox.curselection()
            if sel:
                target_var.set(options[sel[0]])
                self._save_paths()
                top.destroy()

        btn_frame = ttk.Frame(top)
        btn_frame.pack(fill="x", pady=6)
        ttk.Button(btn_frame, text="Select", command=choose).pack(side="right", padx=8)
        ttk.Button(btn_frame, text="Cancel", command=top.destroy).pack(side="right")

    def browse_nexus_folder(self):
        initial = self.nexus_path_var.get() or str(Path.home())
        folder = filedialog.askdirectory(title="Select Nexus project folder", initialdir=initial)
        if folder:
            self.nexus_path_var.set(folder)
            self._save_paths()

    # ---------------------------------------------------------- auto-validate saved paths on startup

    def _auto_validate_saved_paths(self):
        py = self.python_path_var.get()
        nx = self.nexus_path_var.get()
        if py and not Path(py).is_file():
            self.append_log("warn", f"Saved Python path no longer exists: {py}")
        elif py:
            self.action_validate_python()
        if nx and not Path(nx).is_dir():
            self.append_log("warn", f"Saved Nexus folder no longer exists: {nx}")
        elif nx:
            self.action_validate_nexus()

    # ---------------------------------------------------------- validation actions

    def action_validate_python(self):
        exe = self.python_path_var.get().strip()
        if not exe:
            messagebox.showwarning(APP_TITLE, "Select a Python executable first.")
            return

        def task(log):
            log("info", f"Validating portable Python: {exe}")
            info = validate_python(exe, log)
            return info

        def done(info):
            self.python_info = info
            if info.exists and not info.errors:
                self.python_status_var.set(f"OK - {info.version}, {info.architecture}, pip {info.pip_version.split()[1] if info.pip_version else '?'}")
            elif info.exists:
                self.python_status_var.set("Validated with problems - see log.")
            else:
                self.python_status_var.set("Not found / invalid.")
            for e in info.errors:
                self.append_log("error", e)
            self._save_paths()
            self._render_env_info()
            self._refresh_action_states()

        self.run_task("Validate Python", task, done)

    def action_validate_nexus(self):
        folder = self.nexus_path_var.get().strip()
        if not folder:
            messagebox.showwarning(APP_TITLE, "Select a Nexus project folder first.")
            return

        def task(log):
            log("info", f"Inspecting Nexus folder: {folder}")
            info = inspect_nexus_folder(folder, log)
            return info

        def done(info):
            self.nexus_info = info
            if info.ambiguous_roots and len(info.ambiguous_roots) > 1:
                options = [str(p) for p in info.ambiguous_roots]
                self._show_candidate_picker(
                    "Multiple possible Nexus project roots found - select the correct one",
                    options, self.nexus_path_var,
                )
            for w in info.warnings:
                self.append_log("warn", w)
            for e in info.errors:
                self.append_log("error", e)
            if info.exists and not info.errors:
                fw = ", ".join(sorted(info.frameworks_detected)) or "unknown"
                self.nexus_status_var.set(f"OK - framework: {fw}; startup: {' '.join(info.startup_command) if info.startup_command else 'UNDETERMINED'}")
            elif info.exists:
                self.nexus_status_var.set("Validated with problems - see log.")
            else:
                self.nexus_status_var.set("Not found / invalid.")
            self._save_paths()
            self._render_env_info()
            self._refresh_action_states()

        self.run_task("Validate Nexus Folder", task, done)

    def action_validate_all(self):
        self.action_validate_python()
        self.root.after(300, self.action_validate_nexus)

    # ---------------------------------------------------------- environment / dependency actions

    def action_create_env(self):
        nexus_folder = Path(self.nexus_path_var.get())
        py_exe = self.python_path_var.get()
        if not self.python_info.exists or not self.nexus_info.exists:
            messagebox.showwarning(APP_TITLE, "Validate Python and Nexus folder first.")
            return

        existing = inspect_existing_venv(nexus_folder, self.python_info, lambda *a: None)
        proceed_mode = "create"
        if existing:
            if existing.get("compatible"):
                choice = messagebox.askyesnocancel(
                    APP_TITLE,
                    "An existing, compatible virtual environment was found.\n\n"
                    "Yes = update it (install/refresh dependencies)\n"
                    "No = back it up and create a fresh one\n"
                    "Cancel = abort",
                )
                if choice is None:
                    return
                proceed_mode = "update" if choice else "recreate"
            else:
                issues = "\n - ".join(existing.get("issues", []))
                messagebox.showwarning(
                    APP_TITLE,
                    f"The existing .venv is missing or incompatible:\n\n - {issues}\n\n"
                    "It will be renamed with a timestamp and a fresh environment will be created.",
                )
                proceed_mode = "recreate"

        def task(log):
            if proceed_mode == "recreate":
                ok, backup_path = backup_existing_venv(nexus_folder, log)
                if not ok:
                    return False
            elif proceed_mode == "update":
                log("info", "Reusing existing compatible virtual environment.")
                return True
            ok = create_venv(py_exe, nexus_folder, log)
            return ok

        def done(result):
            self.venv_status = inspect_existing_venv(nexus_folder, self.python_info, lambda *a: None)
            self._render_env_info()
            self._refresh_action_states()

        self.run_task("Create / Repair Environment", task, done)

    def action_install_deps(self):
        nexus_folder = Path(self.nexus_path_var.get())
        vpy = venv_python_path(nexus_folder)
        if not vpy.is_file():
            messagebox.showwarning(APP_TITLE, "Create the virtual environment first.")
            return

        binary_only = self.install_mode_var.get() == "binary_only"
        nexus_info = self.nexus_info

        # Warn about a known-problematic asyncpg pin before installing.
        if nexus_info.asyncpg_requirement and nexus_info.asyncpg_requirement.specifier:
            spec = nexus_info.asyncpg_requirement.specifier
            m = re.search(r"==\s*([\d.]+)", spec)
            if m:
                pinned = tuple(int(x) for x in m.group(1).split("."))
                py_ver = self.python_info.version_tuple
                if py_ver and py_ver >= (3, 13) and pinned < (0, 30, 0):
                    choice = self._prompt_asyncpg_conflict(spec)
                    if choice == "cancel":
                        return
                    elif choice == "modify":
                        if not self._offer_requirements_fix(nexus_info, "asyncpg", f">={DEFAULT_MIN_ASYNCPG_FOR_PY313}"):
                            return  # user declined the preview or it failed
                        # Re-inspect so the in-memory requirement reflects the edited file.
                        self.nexus_info = inspect_nexus_folder(str(nexus_folder), lambda *a: None)
                        nexus_info = self.nexus_info
                        self._render_env_info()
                    # choice == "proceed": fall through and install the pinned version as-is

        def task(log):
            if not upgrade_pip_tools(vpy, log):
                return False
            if not install_critical_binary_deps(vpy, nexus_info, log):
                return False
            if not install_requirements(vpy, nexus_info, nexus_folder, binary_only, log):
                return False
            if not run_pip_check(vpy, log):
                return False
            all_ok = True
            if nexus_info.asyncpg_requirement or self._db_kind_needs("PostgreSQL"):
                ok, _ = import_check(vpy, "asyncpg", log)
                all_ok = all_ok and ok
            if nexus_info.greenlet_requirement or nexus_info.greenlet_needed_transitively:
                ok, _ = import_check(vpy, "greenlet", log)
                all_ok = all_ok and ok
            if any(r.name == "sqlalchemy" for r in nexus_info.requirements):
                ok, _ = import_check(vpy, "sqlalchemy", log)
                all_ok = all_ok and ok
            return all_ok

        def done(result):
            self.deps_installed_ok = bool(result)
            if result:
                self.settings.set("last_setup_success_date", now_ts())
                self.settings.set("last_setup_success_summary", f"Dependencies installed for {nexus_folder}")
                self.settings.save()
            self._render_env_info()
            self._refresh_action_states()

        self.run_task("Install / Update Dependencies", task, done)

    def _prompt_asyncpg_conflict(self, spec):
        """Three real choices, matching what the message actually promises:
        proceed with the pinned version, modify a backed-up copy, or cancel."""
        top = tk.Toplevel(self.root)
        top.title(APP_TITLE)
        top.geometry("560x260")
        top.transient(self.root)
        top.grab_set()
        msg = (
            f"requirements.txt pins asyncpg{spec}, which predates the versions known to ship "
            f"precompiled wheels for Python 3.13 (0.30.0+). Installing it as-is is likely to "
            f"trigger a source build, which will fail without Visual C++ Build Tools.\n\n"
            f"How would you like to proceed?"
        )
        ttk.Label(top, text=msg, wraplength=520, padding=10, justify="left").pack()
        result = {"choice": "cancel"}

        def pick(choice):
            result["choice"] = choice
            top.destroy()

        btns = ttk.Frame(top)
        btns.pack(pady=10)
        ttk.Button(btns, text=f"Create a modified requirements copy (asyncpg>={DEFAULT_MIN_ASYNCPG_FOR_PY313})",
                   command=lambda: pick("modify")).pack(fill="x", pady=2)
        ttk.Button(btns, text=f"Continue anyway with the pinned version ({spec})",
                   command=lambda: pick("proceed")).pack(fill="x", pady=2)
        ttk.Button(btns, text="Cancel", command=lambda: pick("cancel")).pack(fill="x", pady=2)
        self.root.wait_window(top)
        return result["choice"]

    def _offer_requirements_fix(self, nexus_info, package_name, new_specifier):
        """Backs up requirements.txt, shows the exact proposed line change,
        and only writes it after explicit confirmation - per the
        backup-first / show-the-change / ask-first / single-line-only rules."""
        if not nexus_info.requirements_files:
            messagebox.showerror(APP_TITLE, "No requirements.txt found.")
            return False
        req_file = nexus_info.requirements_files[0]

        old_line, new_line, idx, newline = preview_requirement_line_change(req_file, package_name, new_specifier)
        if old_line is None:
            messagebox.showerror(APP_TITLE, f"Could not find a '{package_name}' line to modify.")
            return False

        proceed = messagebox.askyesno(
            APP_TITLE,
            f"A timestamped backup of {req_file.name} will be created first.\n\n"
            f"Proposed change (this line only):\n\n"
            f"  - {old_line.strip()}\n"
            f"  + {new_line.strip()}\n\n"
            f"Apply this change?",
        )
        if not proceed:
            self.append_log("info", "Requirements modification cancelled by user.")
            return False

        backup_path = backup_requirements_file(req_file, self.append_log)
        if backup_path is None:
            messagebox.showerror(APP_TITLE, "Could not create a backup; aborting the change.")
            return False

        ok = apply_requirement_line_change(req_file, idx, new_line, newline, self.append_log)
        if ok:
            self.append_log("success", f"Backup saved at: {backup_path}")
        return ok

    def _db_kind_needs(self, kind):
        return kind in detect_database_kind(Path(self.nexus_path_var.get()), self.nexus_info)

    def action_full_setup(self):
        # Chains: validate -> create/repair env -> install deps.
        self.append_log("info", "=== Full Setup started ===")

        def after_nexus_validate(_):
            self.action_create_env()
            self.root.after(500, chain_to_install)

        def chain_to_install(*_):
            if self.task_running:
                self.root.after(500, chain_to_install)
                return
            self.action_install_deps()

        def after_python_validate(_):
            self.action_validate_nexus()
            self.root.after(500, lambda: after_nexus_validate(None))

        self.action_validate_python()
        self.root.after(500, lambda: after_python_validate(None))

    # ---------------------------------------------------------- database actions

    def action_init_database(self):
        nexus_folder = Path(self.nexus_path_var.get())
        vpy = venv_python_path(nexus_folder)
        info = self.nexus_info
        if not info.seed_script_relpath:
            messagebox.showerror(APP_TITLE, "No database seed script was detected in this Nexus folder.")
            return

        db_kinds = detect_database_kind(nexus_folder, info)
        proceed = messagebox.askyesno(
            APP_TITLE,
            "You are about to run the Nexus database seed procedure. Do not continue for an "
            "existing database unless the seed process is known to be safe to repeat.\n\n"
            f"Detected database engine(s): {', '.join(db_kinds)}\n"
            f"Seed module: {info.seed_script_relpath}\n\n"
            "Continue?",
        )
        if not proceed:
            self.append_log("info", "Database seed cancelled by user.")
            return

        def task(log):
            return run_seed(vpy, info, nexus_folder, log)

        self.run_task("Initialize Database", task)

    def action_run_migrations(self):
        info = self.nexus_info
        nexus_folder = Path(self.nexus_path_var.get())
        vpy = venv_python_path(nexus_folder)
        if not info.migration_configs:
            messagebox.showinfo(APP_TITLE, "No supported migration mechanism (Alembic) was detected.")
            return

        cfg = info.migration_configs[0]
        if len(info.migration_configs) > 1:
            names = [c["ini_path"].name for c in info.migration_configs]
            top = tk.Toplevel(self.root)
            top.title("Select migration configuration")
            top.geometry("480x220")
            ttk.Label(top, text="Multiple migration configurations were detected:", padding=8).pack(anchor="w")
            listbox = tk.Listbox(top, width=60, height=6)
            for n in names:
                listbox.insert("end", n)
            listbox.pack(fill="both", expand=True, padx=8)

            def go():
                sel = listbox.curselection()
                if sel:
                    top.destroy()
                    self._run_migration_for(info.migration_configs[sel[0]])

            ttk.Button(top, text="Continue", command=go).pack(pady=6)
            return
        self._run_migration_for(cfg)

    def _run_migration_for(self, cfg):
        nexus_folder = Path(self.nexus_path_var.get())
        vpy = venv_python_path(nexus_folder)
        catalog = ""
        if cfg.get("needs_catalog_flag"):
            catalogs = cfg.get("catalogs") or []
            if catalogs:
                top = tk.Toplevel(self.root)
                top.title("Select catalog")
                top.geometry("360x200")
                ttk.Label(top, text=f"{cfg['ini_path'].name} requires a catalog (-x catalog=<name>).\n"
                                     f"Migrations must be applied once per catalog.", padding=8, wraplength=340).pack()
                listbox = tk.Listbox(top, height=6)
                for c in catalogs:
                    listbox.insert("end", c)
                listbox.pack(fill="both", expand=True, padx=8)

                def go():
                    sel = listbox.curselection()
                    if sel:
                        top.destroy()
                        self._confirm_and_run_migration(cfg, catalogs[sel[0]])

                ttk.Button(top, text="Run for selected catalog", command=go).pack(pady=6)
                return
            else:
                catalog = simple_text_prompt(
                    self.root, "Catalog required",
                    f"{cfg['ini_path'].name} requires a catalog name (-x catalog=<name>).\n"
                    f"Enter the catalog name shown in the app's documentation:",
                )
                if not catalog:
                    return
        self._confirm_and_run_migration(cfg, catalog)

    def _confirm_and_run_migration(self, cfg, catalog):
        nexus_folder = Path(self.nexus_path_var.get())
        vpy = venv_python_path(nexus_folder)
        desc = f"alembic -c {cfg['ini_path'].name}"
        if catalog:
            desc += f" -x catalog={catalog}"
        desc += " upgrade head"
        proceed = messagebox.askyesno(
            APP_TITLE,
            f"About to run the following migration command:\n\n{desc}\n\n"
            f"(cwd: {nexus_folder})\n\nContinue?",
        )
        if not proceed:
            return

        def task(log):
            return run_migration(None, vpy, cfg, catalog, nexus_folder, log)

        self.run_task("Run Database Migrations", task)

    # ---------------------------------------------------------- start Nexus

    def action_start_nexus(self):
        info = self.nexus_info
        nexus_folder = Path(self.nexus_path_var.get())
        vpy = venv_python_path(nexus_folder)
        if not info.startup_command:
            messagebox.showerror(APP_TITLE, "No startup command could be determined for this Nexus folder.")
            return
        args = [str(vpy)] + info.startup_command
        self.append_log("info", f"Starting Nexus: {format_cmd(args)}  (cwd={nexus_folder})")
        try:
            creationflags = CREATE_NEW_CONSOLE if IS_WINDOWS else 0
            subprocess.Popen(args, cwd=str(nexus_folder), creationflags=creationflags)
        except OSError as e:
            self.append_log("error", f"Failed to start Nexus: {e}")
            messagebox.showerror(APP_TITLE, f"Failed to start Nexus:\n{e}")
            return
        self.append_log("success", "Nexus process launched in its own console window.")
        self.settings.set("last_startup_command", info.startup_command)
        self.settings.save()
        if info.startup_url_hint:
            if messagebox.askyesno(APP_TITLE, f"Nexus was launched. Open {info.startup_url_hint} in your browser now?"):
                self.root.after(1500, lambda: webbrowser.open(info.startup_url_hint))

    # ---------------------------------------------------------- misc actions

    def action_open_nexus_folder(self):
        folder = self.nexus_path_var.get()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning(APP_TITLE, "No valid Nexus folder selected.")
            return
        try:
            if IS_WINDOWS:
                os.startfile(folder)  # noqa: S606 - explicit user-initiated folder open
            else:
                subprocess.Popen(["xdg-open", folder])
        except OSError as e:
            messagebox.showerror(APP_TITLE, str(e))

    def action_open_log_folder(self):
        d = log_dir()
        try:
            if IS_WINDOWS:
                os.startfile(str(d))
            else:
                subprocess.Popen(["xdg-open", str(d)])
        except OSError as e:
            messagebox.showerror(APP_TITLE, str(e))

    def action_reset_paths(self):
        if messagebox.askyesno(APP_TITLE, "Reset all saved paths and preferences?"):
            self.settings.reset()
            self.python_path_var.set("")
            self.nexus_path_var.set("")
            self.python_info = PythonInfo()
            self.nexus_info = NexusInfo()
            self.venv_status = None
            self.deps_installed_ok = False
            self.python_status_var.set("Not validated.")
            self.nexus_status_var.set("Not validated.")
            self._render_env_info()
            self._refresh_action_states()
            self.append_log("info", "Saved paths reset.")

    def action_copy_log(self):
        content = self.log_text.get("1.0", "end")
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self.append_log("info", "Log copied to clipboard.")

    def action_save_log_as(self):
        path = filedialog.asksaveasfilename(
            title="Save Log As", defaultextension=".txt",
            initialfile=f"nexus_setup_log_{now_stamp()}.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text(self.log_text.get("1.0", "end"), encoding="utf-8")
            self.append_log("success", f"Log saved to {path}")
        except OSError as e:
            messagebox.showerror(APP_TITLE, str(e))

    def action_clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ---------------------------------------------------------- environment info rendering

    def _render_env_info(self):
        lines = []
        p, n = self.python_info, self.nexus_info

        lines.append("PORTABLE PYTHON")
        lines.append(f"  Executable      : {p.exe_path or '(not selected)'}")
        lines.append(f"  Version         : {p.version or '?'}")
        lines.append(f"  sys.executable  : {p.sys_executable or '?'}")
        lines.append(f"  Architecture    : {p.architecture or '?'} ({p.machine or '?'})")
        lines.append(f"  pip             : {'OK - ' + p.pip_version if p.pip_ok else 'NOT AVAILABLE'}")
        lines.append(f"  venv module     : {'OK' if p.venv_ok else 'NOT AVAILABLE'}")
        if p.errors:
            lines.append("  Issues:")
            for e in p.errors:
                lines.append(f"    - {e}")
        lines.append("")

        lines.append("NEXUS PROJECT")
        lines.append(f"  Folder                : {n.folder or '(not selected)'}")
        lines.append(f"  requirements.txt      : {'FOUND' if n.requirements_files else 'NOT FOUND'}")
        if n.requirements_files:
            lines.append(f"    Files: {', '.join(str(f) for f in n.requirements_files)}")
        lines.append(f"  pyproject.toml        : {'present' if n.pyproject_exists else 'not present'}")
        venv_dir = Path(n.folder) / ".venv" if n.folder else None
        lines.append(f"  .venv                 : {'present' if venv_dir and venv_dir.is_dir() else 'not created yet'}")
        asyncpg_line = "not required"
        if n.asyncpg_requirement:
            asyncpg_line = f"{n.asyncpg_requirement.name}{n.asyncpg_requirement.specifier or ' (unpinned)'}"
        lines.append(f"  asyncpg requirement   : {asyncpg_line}")
        greenlet_line = "not required"
        if n.greenlet_requirement:
            greenlet_line = f"{n.greenlet_requirement.name}{n.greenlet_requirement.specifier or ' (unpinned)'}"
        elif n.greenlet_needed_transitively:
            greenlet_line = "required transitively (SQLAlchemy async engine detected in source)"
        lines.append(f"  greenlet requirement  : {greenlet_line}")
        lines.append(f"  Database seed         : {'available (' + n.seed_script_relpath + ')' if n.seed_script_relpath else 'not detected'}")
        if n.migration_configs:
            for cfg in n.migration_configs:
                extra = " [needs -x catalog=<name>]" if cfg.get("needs_catalog_flag") else ""
                lines.append(f"  Migration config      : {cfg['ini_path'].name}{extra}")
        else:
            lines.append("  Migration tool        : not detected")
        lines.append(f"  Detected framework(s) : {', '.join(sorted(n.frameworks_detected)) or 'unknown'}")
        if n.startup_command:
            lines.append(f"  Detected startup cmd  : <venv python> {' '.join(n.startup_command)}")
            for ev in n.startup_evidence:
                lines.append(f"    evidence: {ev}")
            if n.startup_url_hint:
                lines.append(f"  App URL (from docs)   : {n.startup_url_hint}")
        else:
            lines.append("  Detected startup cmd  : UNDETERMINED - specify manually")
        lines.append(f"  .env                  : {'present' if n.env_exists else 'not present'}")
        lines.append(f"  .env.example          : {'present' if n.env_example_exists else 'not present'}")
        db_kinds = detect_database_kind(Path(n.folder), n) if n.folder else ["?"]
        lines.append(f"  Database engine(s)    : {', '.join(db_kinds)}")

        if n.warnings:
            lines.append("  Warnings:")
            for w in n.warnings:
                lines.append(f"    - {w}")
        if n.errors:
            lines.append("  Errors:")
            for e in n.errors:
                lines.append(f"    - {e}")
        if n.duplicate_requirement_names:
            lines.append(f"  Duplicate/conflicting requirement names: {', '.join(n.duplicate_requirement_names)}")

        lines.append("")
        python_ok = p.exists and not p.errors
        nexus_ok = n.exists and not n.errors
        venv_ok = bool(self.venv_status and self.venv_status.get("python_exe_exists"))
        ready = python_ok and nexus_ok and venv_ok and self.deps_installed_ok
        lines.append(f"SETUP READINESS: {'READY' if ready else 'NOT READY YET'}")

        self.env_info_text.configure(state="normal")
        self.env_info_text.delete("1.0", "end")
        self.env_info_text.insert("1.0", "\n".join(lines))
        self.env_info_text.configure(state="disabled")

    # ---------------------------------------------------------- exit

    def on_exit(self):
        self._save_paths()
        self.root.destroy()


def simple_text_prompt(root, title, message):
    top = tk.Toplevel(root)
    top.title(title)
    top.geometry("420x160")
    top.transient(root)
    top.grab_set()
    ttk.Label(top, text=message, wraplength=380, padding=8).pack()
    var = tk.StringVar()
    entry = ttk.Entry(top, textvariable=var, width=40)
    entry.pack(padx=8, pady=4)
    entry.focus_set()
    result = {"value": None}

    def ok():
        result["value"] = var.get().strip()
        top.destroy()

    def cancel():
        result["value"] = None
        top.destroy()

    btns = ttk.Frame(top)
    btns.pack(pady=8)
    ttk.Button(btns, text="OK", command=ok).pack(side="left", padx=6)
    ttk.Button(btns, text="Cancel", command=cancel).pack(side="left", padx=6)
    root.wait_window(top)
    return result["value"]


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if IS_WINDOWS and "vista" in style.theme_names():
            style.theme_use("vista")
    except tk.TclError:
        pass
    app = NexusSetupManagerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
