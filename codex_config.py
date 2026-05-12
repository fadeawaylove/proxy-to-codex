import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import tomlkit


def _run_hidden(args, timeout=10):
    """Run a subprocess without creating a visible console window on Windows."""
    kwargs = {"text": True, "timeout": timeout}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.check_output(args, **kwargs)


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


def _prune_backups(config_path: Path, keep: int) -> None:
    """Keep only the `keep` most recent backup files."""
    all_backups = list_backups(config_path)
    for stale in all_backups[keep:]:
        stale.unlink(missing_ok=True)


def backup(config_path: Path) -> Path | None:
    """Create a timestamped backup of the config file. Returns backup path or None."""
    if not config_path.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = config_path.with_suffix(f".toml.backup.{timestamp}")
    shutil.copy2(config_path, backup_path)
    _prune_backups(config_path, keep=10)
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


def apply(port: int, config_path: Path, host: str = "localhost") -> bool:
    """Set Codex proxy URL to http://{host}:{port}/v1 in config.toml.

    Uses tomlkit to preserve formatting, comments, and section order.
    Returns True on success.
    """
    new_url = f"http://{host}:{port}/v1"
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


def get_wsl_host_ip(distro: str) -> str | None:
    """Return the Windows host IP visible from inside a WSL distro."""
    try:
        output = _run_hidden(
            ["wsl", "-d", distro, "-e", "sh", "-c",
             "ip route show default | awk '{print $3}'"])
        ip = output.strip()
        if ip:
            return ip
    except (subprocess.CalledProcessError, OSError):
        pass
    return None


def find_wsl_configs() -> list[dict]:
    """Discover Codex config.toml files inside WSL distributions.

    Returns a list of dicts with keys: config_path (Path), distro (str),
    label (str).
    """
    results: list[dict] = []

    # 1. List WSL distros
    try:
        output = _run_hidden(["wsl", "--list", "--quiet"])
        output = output.replace("\x00", "")
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return results

    distros = [d.strip() for d in output.splitlines() if d.strip()]

    # 2. For each distro, locate ~/.codex/config.toml
    for distro in distros:
        try:
            home_output = _run_hidden(
                ["wsl", "-d", distro, "-e", "sh", "-c", "echo $HOME"])
            home = home_output.strip()
            if not home or not home.startswith("/"):
                continue

            # Convert /home/user  ->  \\wsl$\distro\home\user\.codex\config.toml
            rel = home.lstrip("/")
            config_path = Path(
                f"\\\\wsl$\\{distro}\\{rel.replace('/', '\\')}\\.codex\\config.toml"
            )

            if config_path.exists():
                results.append({
                    "config_path": config_path,
                    "distro": distro,
                    "label": f"{distro} ({home}/.codex/config.toml)",
                })
        except (subprocess.CalledProcessError, OSError):
            continue

    return results
