"""Private helper: shared local test-command detection — not a pipeline."""
from __future__ import annotations

from pathlib import Path


# --- per-project test command resolution ----------------------------------

def detect_test_cmd(project_dir: str) -> str:
    """Pick a sensible local test command from common project markers.

    Resolution order — first match wins. Add markers as needed for new
    project shapes; this stays a flat lookup on purpose so each project
    can also override via the ``test_cmd`` arg or ``NORN_TEST_CMD`` env
    var without touching this file.
    """
    root = Path(project_dir)
    if (root / "pom.xml").exists():
        return "mvn -q verify"
    if (root / "gradlew").exists():
        return "./gradlew test"
    if (root / "package.json").exists():
        return "npm test"
    if (root / "Cargo.toml").exists():
        return "cargo test"
    if (root / "go.mod").exists():
        return "go test ./..."
    if (root / "tests").is_dir() or (root / "pyproject.toml").exists():
        if (root / "tests").is_dir():
            return "uv run python -m pytest tests/ -v"
        return "uv run python -m pytest -v"
    return "true"
