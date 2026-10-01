"""Resolve repository assets independently of the backend process working directory."""

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DOCUMENTATION_DIR = REPOSITORY_ROOT / "Documentation"
FRONTEND_DIST = REPOSITORY_ROOT / "frontend" / "dist"
