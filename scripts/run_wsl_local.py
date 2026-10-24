#!/usr/bin/env python3
"""Launch marking-bm in WSL2 over loopback, using a protected systemd-style env file."""
from __future__ import annotations

import os
from pathlib import Path
import sys

CONFIG = Path.home() / ".config" / "marking-bm" / "local.env"
ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "BM_DATA_DIR", "BM_PUBLIC_URL", "BM_COOKIE_SECURE",
    "BM_ADMIN_PASSWORD_HASH", "BM_SIGNER_TOKEN", "BM_SIGNER_THUMBPRINT",
    "BM_ALLOW_EXTERNAL_WRITES",
)

if sys.platform != "linux" or sys.version_info < (3, 12):
    raise SystemExit("Run inside Ubuntu WSL2 with Python 3.12+.")
if not CONFIG.is_file() or CONFIG.is_symlink():
    raise SystemExit(f"Missing or unexpected config: {CONFIG}. Run setup_wsl_local.py first.")
if CONFIG.stat().st_mode & 0o077:
    raise SystemExit("Config permissions are too broad; run: chmod 600 ~/.config/marking-bm/local.env")
settings = {}
for raw in CONFIG.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if "=" not in line:
        raise SystemExit("Bad environment line; use KEY=value without shell quoting.")
    key, value = line.split("=", 1)
    if not key.startswith("BM_") or key in settings:
        raise SystemExit(f"Bad or duplicate environment variable: {key}")
    settings[key] = value
if any(not settings.get(k) for k in REQUIRED):
    raise SystemExit("Incomplete local config.")
if settings["BM_PUBLIC_URL"] != "http://localhost:8010" or settings["BM_COOKIE_SECURE"] != "0":
    raise SystemExit("Local-only runner requires BM_PUBLIC_URL=http://localhost:8010 and BM_COOKIE_SECURE=0.")
if settings["BM_ALLOW_EXTERNAL_WRITES"] not in ("0", "1"):
    raise SystemExit("BM_ALLOW_EXTERNAL_WRITES must be 0 or 1.")

data_dir = Path(settings["BM_DATA_DIR"]).resolve()
if not data_dir.is_dir() or not data_dir.is_relative_to(Path.home().resolve()):
    raise SystemExit("Use a data directory inside your WSL Linux home.")
os.environ.update(settings)
os.chdir(ROOT)
# When launched as "python scripts/run_wsl_local.py", sys.path[0] is scripts/.
# Uvicorn's programmatic runner does not add the current directory automatically.
# Ensure both main.py and marking_bm/ can be imported from the project root.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
print("marking-bm local: http://localhost:8010 (Windows and WSL only)")
print("Real external writes enabled:", settings["BM_ALLOW_EXTERNAL_WRITES"] == "1")
uvicorn.run("main:app", host="127.0.0.1", port=8010, workers=1, proxy_headers=False)
