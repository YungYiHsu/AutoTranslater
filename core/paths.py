"""Application and bundled-resource path helpers."""

from __future__ import annotations

import sys
from pathlib import Path


def get_app_dir() -> Path:
    """Return the writable directory beside the source entry point or executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def get_resource_path(relative_path: str | Path) -> Path:
    """Resolve a read-only bundled resource in source and PyInstaller modes."""
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Resource path must be relative and cannot contain '..'.")

    bundle_dir = getattr(sys, "_MEIPASS", None)
    base_dir = Path(bundle_dir) if bundle_dir else Path(__file__).resolve().parents[1]
    return (base_dir / relative).resolve()


def resolve_runtime_path(path_value: str | Path, *, app_dir: Path | None = None) -> Path:
    """Resolve an external writable path relative to the application directory."""
    path = Path(path_value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return ((app_dir or get_app_dir()) / path).resolve()


def ensure_runtime_directories(
    output_directory: str | Path,
    *,
    app_dir: Path | None = None,
) -> tuple[Path, Path, Path]:
    """Create and return the output, checkpoint, and log directories."""
    base_dir = app_dir or get_app_dir()
    output_dir = resolve_runtime_path(output_directory, app_dir=base_dir)
    checkpoint_dir = base_dir / "checkpoints"
    log_dir = base_dir / "logs"

    for directory in (output_dir, checkpoint_dir, log_dir):
        directory.mkdir(parents=True, exist_ok=True)

    return output_dir, checkpoint_dir, log_dir
