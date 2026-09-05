from __future__ import annotations

import ast
import importlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from norn.dsl import Pipeline

log = logging.getLogger(__name__)

_PIPELINES_DIR = Path(__file__).parent / "pipelines"


@dataclass
class PipelineInfo:
    name: str
    short: str
    long: str
    env_vars: list[str] = field(default_factory=list)
    args: dict[str, str] = field(default_factory=dict)
    path: Path = field(default_factory=lambda: Path())


def _extract_metadata(source: str) -> tuple[str, str, list[str], dict[str, str]]:
    """Extract docstring and metadata dict from pipeline source using AST.

    Returns (short_description, long_description, env_vars, args).
    """
    tree = ast.parse(source)

    # Module docstring
    docstring = ast.get_docstring(tree) or ""
    short = docstring.split("\n", 1)[0].strip() if docstring else ""
    long = docstring

    # Walk top-level assignments looking for `metadata = {...}`
    env_vars: list[str] = []
    args: dict[str, str] = {}
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "metadata":
                meta = ast.literal_eval(node.value)
                if isinstance(meta, dict):
                    env_vars = meta.get("env_vars", [])
                    args = meta.get("args", {})
                break

    return short, long, env_vars, args


def list_pipelines() -> list[PipelineInfo]:
    """Discover all bundled pipelines via AST parsing (no imports).

    Modules whose name starts with ``_`` are skipped: ``__init__.py`` and
    private helpers such as ``_snapshot_diff.py`` are not pipelines and must
    not show up in ``norn list`` or the TUI launcher.
    """
    results: list[PipelineInfo] = []
    for path in sorted(_PIPELINES_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        source = path.read_text()
        short, long, env_vars, args = _extract_metadata(source)
        results.append(PipelineInfo(
            name=path.stem,
            short=short,
            long=long,
            env_vars=env_vars,
            args=args,
            path=path,
        ))
    return results


def _get_pipeline_dirs() -> list[Path]:
    """Return configured pipeline directories to scan for user pipelines.

    Checks (in order):
    - ``./pipelines`` relative to the current working directory
    - ``$NORN_CONFIG_DIR/pipelines`` (or ``~/.norn/pipelines`` by default)
    """
    dirs: list[Path] = []
    local = Path.cwd() / "pipelines"
    if local.is_dir():
        dirs.append(local)
    config_dir_env = os.environ.get("NORN_CONFIG_DIR")
    if config_dir_env:
        user_dir = Path(config_dir_env) / "pipelines"
    else:
        user_dir = Path.home() / ".norn" / "pipelines"
    if user_dir.is_dir():
        dirs.append(user_dir)
    return dirs


def list_discovered_pipelines(extra_dirs: list[Path] | None = None) -> list[PipelineInfo]:
    """Discover pipelines from configured directories via AST parsing (no imports).

    Scans :func:`_get_pipeline_dirs` plus any *extra_dirs*.  Files that fail
    to parse are silently skipped (logged at DEBUG).  Duplicate paths are
    deduplicated; bundled pipelines are **not** included (use
    :func:`list_pipelines` for those).

    Args:
        extra_dirs: Additional directories to scan (used in tests).

    Returns:
        List of :class:`PipelineInfo` sorted by directory order then filename.
    """
    dirs = _get_pipeline_dirs()
    if extra_dirs:
        dirs.extend(extra_dirs)
    results: list[PipelineInfo] = []
    seen: set[str] = set()
    for directory in dirs:
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            abs_path = str(path.resolve())
            if abs_path in seen:
                continue
            seen.add(abs_path)
            try:
                source = path.read_text()
                short, long, env_vars, args = _extract_metadata(source)
                results.append(PipelineInfo(
                    name=path.stem,
                    short=short,
                    long=long,
                    env_vars=env_vars,
                    args=args,
                    path=path,
                ))
            except Exception:
                log.debug("Failed to parse pipeline %s", path, exc_info=True)
    return results


def get_pipeline_info(name: str) -> PipelineInfo | None:
    """Look up a single bundled pipeline by name."""
    for info in list_pipelines():
        if info.name == name:
            return info
    return None


def load_bundled_pipeline(name: str) -> Pipeline:
    """Import a bundled pipeline module and return its ``config`` Pipeline.

    Raises:
        ValueError: if the module has no ``config`` attribute of type Pipeline.
    """
    module = importlib.import_module(f"norn.pipelines.{name}")
    config = getattr(module, "config", None)
    if not isinstance(config, Pipeline):
        raise ValueError(
            f"norn.pipelines.{name} must define a 'config' variable of type Pipeline"
        )
    return config
