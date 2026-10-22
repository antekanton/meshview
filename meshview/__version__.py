"""Version information for MeshView."""

import os
import subprocess
from pathlib import Path

__version__ = "3.0.8"
__release_date__ = "2026-5-12"


def _env(name, environ=None):
    """Return a non-empty environment value, or None."""
    value = (os.environ if environ is None else environ).get(name, "").strip()
    return value or None


def resolve_version(default, environ=None):
    """Release tag baked into the container (rel- prefix dropped), else the default."""
    tag = _env("MESHVIEW_VERSION", environ)
    return tag.removeprefix("rel-") if tag else default


def resolve_revision(environ=None):
    """Commit baked into the container, or None outside a release build."""
    return _env("MESHVIEW_REVISION", environ)


def get_git_revision():
    """Get the current git revision hash."""
    if revision := resolve_revision():
        return revision
    try:
        repo_dir = Path(__file__).parent.parent
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=repo_dir,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def get_git_revision_short():
    """Get the short git revision hash."""
    if revision := resolve_revision():
        return revision[:7]
    try:
        repo_dir = Path(__file__).parent.parent
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=repo_dir,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


# Release builds pass the fork's tag in (see Dockerfile); local runs keep the value above.
__version__ = resolve_version(__version__)


def get_version_info():
    """Get complete version information."""
    return {
        "version": __version__,
        "release_date": __release_date__,
        "git_revision": get_git_revision(),
        "git_revision_short": get_git_revision_short(),
    }


# Cache git info at import time for performance
_git_revision = get_git_revision()
_git_revision_short = get_git_revision_short()

# Full version string for display
__version_string__ = f"{__version__} ~ {__release_date__}"
