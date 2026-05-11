import os
import shutil
from datetime import datetime
from pathlib import Path

import tomlkit


def get_config_path() -> Path:
    """Detect and return the Codex config.toml path."""
    base = os.environ.get("CODEX_CONFIG_DIR", "")
    if base and Path(base).is_dir():
        return Path(base) / "config.toml"

    if os.name == "nt":
        userprofile = os.environ.get("USERPROFILE", "")
        if userprofile:
            return Path(userprofile) / ".codex" / "config.toml"
    else:
        home = Path.home()
        return home / ".codex" / "config.toml"

    return Path.home() / ".codex" / "config.toml"


def ensure_config_dir(config_path: Path) -> None:
    """Create the .codex directory if it doesn't exist."""
    config_path.parent.mkdir(parents=True, exist_ok=True)


def backup(config_path: Path) -> Path | None:
    """Create a timestamped backup of the config file. Returns backup path or None."""
    if not config_path.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = config_path.with_suffix(f".toml.backup.{timestamp}")
    shutil.copy2(config_path, backup_path)
    return backup_path


def list_backups(config_path: Path) -> list[Path]:
    """List all backup files for the config, newest first."""
    pattern = config_path.name.replace(".toml", ".toml.backup.*")
    backups = sorted(
        config_path.parent.glob(pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return backups


def read_config(config_path: Path) -> str:
    """Read the current config file content. Returns empty string if not found."""
    if config_path.exists():
        return config_path.read_text(encoding="utf-8")
    return ""


def apply(port: int, config_path: Path) -> bool:
    """Set Codex proxy URL to http://localhost:{port}/v1 in config.toml.

    Uses tomlkit to preserve formatting, comments, and section order.
    Returns True on success.
    """
    new_url = f"http://localhost:{port}/v1"
    ensure_config_dir(config_path)

    content = read_config(config_path)
    doc = tomlkit.parse(content) if content else tomlkit.document()

    doc["openai_base_url"] = new_url
    doc["allow_insecure"] = True

    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return True


def restore(backup_path: Path, config_path: Path) -> bool:
    """Restore config from a backup file. Returns True on success."""
    if not backup_path.exists():
        return False
    shutil.copy2(backup_path, config_path)
    return True


def restore_latest(config_path: Path) -> Path | None:
    """Restore from the most recent backup. Returns the backup path used, or None."""
    backups = list_backups(config_path)
    if not backups:
        return None
    restore(backups[0], config_path)
    return backups[0]
