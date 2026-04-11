#!/usr/bin/env python3
"""
build_public.py — Injects Firebase config + Clarity ID into public/index.html
Run this before `firebase deploy` to embed environment variables.
"""
import os
import sys
from pathlib import Path

# Parse .env manually (no dotenv required)
env_file = Path(".env")
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

REQUIRED = ["FIREBASE_API_KEY", "FIREBASE_PROJECT_ID", "FIREBASE_APP_ID", "CLARITY_PROJECT_ID"]
missing = [k for k in REQUIRED if not os.environ.get(k)]
if missing:
    print(f"[build] ERROR: missing env vars: {', '.join(missing)}")
    sys.exit(1)

template = Path("public/index.html").read_text()

replacements = {
    "__FIREBASE_API_KEY__": os.environ["FIREBASE_API_KEY"],
    "__FIREBASE_PROJECT_ID__": os.environ["FIREBASE_PROJECT_ID"],
    "__FIREBASE_APP_ID__": os.environ["FIREBASE_APP_ID"],
    "__CLARITY_PROJECT_ID__": os.environ["CLARITY_PROJECT_ID"],
}

for placeholder, value in replacements.items():
    template = template.replace(placeholder, value)

Path("public/index.html").write_text(template)
print("[build] public/index.html built successfully \u2713")
