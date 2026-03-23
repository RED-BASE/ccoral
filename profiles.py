"""
CCORAL v2 — Profile Manager
=============================

Loads YAML profiles and watches for changes.
"""

import yaml
from pathlib import Path
from typing import Optional


PROFILES_DIR = Path(__file__).resolve().parent / "profiles"
USER_PROFILES_DIR = Path.home() / ".ccoral" / "profiles"


def _search_dirs() -> list[Path]:
    """Profile search directories, user first."""
    dirs = []
    if USER_PROFILES_DIR.exists():
        dirs.append(USER_PROFILES_DIR)
    if PROFILES_DIR.exists():
        dirs.append(PROFILES_DIR)
    return dirs


def list_profiles() -> list[dict]:
    """List all available profiles."""
    seen = set()
    profiles = []
    for d in _search_dirs():
        for f in sorted(d.glob("*.yaml")):
            name = f.stem
            if name in seen:
                continue
            seen.add(name)
            try:
                data = yaml.safe_load(f.read_text())
                profiles.append({
                    "name": name,
                    "description": data.get("description", ""),
                    "path": str(f),
                })
            except Exception:
                profiles.append({
                    "name": name,
                    "description": "(error loading)",
                    "path": str(f),
                })
    return profiles


def load_profile(name: str) -> Optional[dict]:
    """Load a profile by name."""
    for d in _search_dirs():
        path = d / f"{name}.yaml"
        if path.exists():
            data = yaml.safe_load(path.read_text())
            data["_path"] = str(path)
            data["_name"] = name
            return data
    return None


def get_active_profile() -> Optional[str]:
    """Get the currently active profile name."""
    active_file = Path.home() / ".ccoral" / "active_profile"
    if active_file.exists():
        name = active_file.read_text().strip()
        return name if name else None
    return None


def set_active_profile(name: Optional[str]):
    """Set the active profile."""
    active_file = Path.home() / ".ccoral" / "active_profile"
    active_file.parent.mkdir(parents=True, exist_ok=True)
    if name:
        active_file.write_text(name)
    elif active_file.exists():
        active_file.unlink()


def load_active_profile() -> Optional[dict]:
    """Load the currently active profile."""
    name = get_active_profile()
    if name:
        return load_profile(name)
    return None
