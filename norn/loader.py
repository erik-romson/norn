from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path

from norn.dsl import Pipeline

log = logging.getLogger(__name__)


def load_pipeline(config_path: str) -> Pipeline:
    """Load a Pipeline definition from an external Python file.

    Raises:
        FileNotFoundError: if the config file does not exist.
        ValueError: if the file does not define a ``config`` variable of type Pipeline.
    """
    path = Path(config_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Pipeline config not found: {path}")

    spec = importlib.util.spec_from_file_location("_pipeline_config", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    config = getattr(module, "config", None)
    if not isinstance(config, Pipeline):
        raise ValueError(f"{path} must define a 'config' variable of type Pipeline")

    return config


def load_org_config(org_name: str) -> Pipeline:
    """Load a Pipeline definition from the orgs config directory.

    Raises:
        FileNotFoundError: if the org config file does not exist.
    """
    config_dir = _get_config_dir()
    path = config_dir / "orgs" / f"{org_name}.py"
    if not path.exists():
        raise FileNotFoundError(f"Org config not found: {path}")
    return _load_config(path)


def find_org_for_project(project_key: str) -> tuple[str, Pipeline]:
    """Find the org config that handles the given project key.

    Raises:
        FileNotFoundError: if no orgs directory exists.
        ValueError: if no org config handles the project key.
    """
    config_dir = _get_config_dir()
    orgs_dir = config_dir / "orgs"
    if not orgs_dir.exists():
        raise FileNotFoundError(f"No orgs directory: {orgs_dir}")
    first_match: tuple[str, Pipeline] | None = None
    for path in sorted(orgs_dir.glob("*.py")):
        try:
            pipeline = _load_config(path)
        except Exception:
            continue
        if project_key in (pipeline.project_keys or []):
            if first_match is None:
                first_match = (path.stem, pipeline)
            else:
                log.warning(
                    "Multiple org configs handle project %r — using %r",
                    project_key,
                    first_match[0],
                )
                break
    if first_match:
        return first_match
    raise ValueError(f"No org config handles project {project_key!r}")


def list_orgs() -> list[str]:
    """Return sorted list of org names found in the config directory."""
    config_dir = _get_config_dir()
    orgs_dir = config_dir / "orgs"
    if not orgs_dir.exists():
        return []
    return sorted(p.stem for p in orgs_dir.glob("*.py"))


def _get_config_dir() -> Path:
    env = os.environ.get("NORN_CONFIG_DIR")
    if env:
        return Path(env)
    # Legacy support
    env = os.environ.get("ISSUEPROC_CONFIG_DIR")
    if env:
        return Path(env)
    new_dir = Path.home() / ".norn"
    old_dir = Path.home() / ".issueprocessing"
    if not new_dir.exists() and old_dir.exists():
        return old_dir
    return new_dir


def _load_config(path: Path) -> Pipeline:
    """Internal loader that temporarily adds the config parent dir to sys.path."""
    config_dir = path.parent.parent  # orgs dir's parent
    sys_path_added = False
    if str(config_dir) not in sys.path:
        sys.path.insert(0, str(config_dir))
        sys_path_added = True
    try:
        return load_pipeline(str(path))
    finally:
        if sys_path_added and str(config_dir) in sys.path:
            sys.path.remove(str(config_dir))
