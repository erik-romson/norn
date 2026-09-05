from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from norn.models import PipelineContext, StageResult
from norn.runner import resolve_run_path
from norn.stages.base import BaseStage


class Check(ABC):
    """Abstract base for a single validation check.

    Implementations must resolve paths through ``resolve_run_path(ctx, self.path)``
    and must not touch ``pathlib.Path`` directly.  This is the worktree-isolation
    contract: relative paths resolve under ``ctx.working_dir`` when set, falling
    back to process cwd otherwise; absolute paths pass through unchanged.
    """

    @abstractmethod
    def run(self, ctx: PipelineContext) -> list[str]:
        """Return a list of error strings. Empty list means the check passed.

        Args:
            ctx: Pipeline context providing the working directory for path resolution.

        Returns:
            A list of human-readable error strings; empty means the check passed.
        """
        ...


class FileExists(Check):
    """Check that a file exists at the given path."""

    def __init__(self, path: str) -> None:
        self.path = path

    def run(self, ctx: PipelineContext) -> list[str]:
        if not resolve_run_path(ctx, self.path).exists():
            return [f"File not found: {self.path}"]
        return []


class Contains(Check):
    """Check that a file contains all given patterns (substring match)."""

    def __init__(self, path: str, *, patterns: list[str]) -> None:
        self.path = path
        self.patterns = patterns

    def run(self, ctx: PipelineContext) -> list[str]:
        resolved = resolve_run_path(ctx, self.path)
        try:
            content = resolved.read_text()
        except OSError as e:
            return [f"Cannot read {self.path}: {e}"]
        return [
            f"{self.path}: missing pattern {pattern!r}"
            for pattern in self.patterns
            if pattern not in content
        ]


class JsonSchema(Check):
    """Check that a JSON file parses and structurally matches the given schema.

    Supports ``type``, ``required``, ``properties``, and ``items`` keywords.
    All violations are collected before returning (non-fail-fast).
    """

    def __init__(self, path: str, *, schema: dict[str, Any]) -> None:
        self.path = path
        self.schema = schema

    def run(self, ctx: PipelineContext) -> list[str]:
        resolved = resolve_run_path(ctx, self.path)
        try:
            content = resolved.read_text()
        except OSError as e:
            return [f"Cannot read {self.path}: {e}"]
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            return [f"{self.path}: invalid JSON: {e}"]
        return self._validate(data, self.schema, self.path)

    def _validate(self, data: Any, schema: dict[str, Any], loc: str) -> list[str]:
        errors: list[str] = []
        schema_type = schema.get("type")
        if schema_type == "object":
            if not isinstance(data, dict):
                return [f"{loc}: expected object, got {type(data).__name__}"]
            for key in schema.get("required", []):
                if key not in data:
                    errors.append(f"{loc}: missing required key {key!r}")
            for key, prop_schema in schema.get("properties", {}).items():
                if key in data:
                    errors.extend(self._validate(data[key], prop_schema, f"{loc}.{key}"))
        elif schema_type == "array":
            if not isinstance(data, list):
                return [f"{loc}: expected array, got {type(data).__name__}"]
            item_schema = schema.get("items", {})
            for i, item in enumerate(data):
                errors.extend(self._validate(item, item_schema, f"{loc}[{i}]"))
        elif schema_type == "string":
            if not isinstance(data, str):
                errors.append(f"{loc}: expected string, got {type(data).__name__}")
        elif schema_type == "boolean":
            if not isinstance(data, bool):
                errors.append(f"{loc}: expected boolean, got {type(data).__name__}")
        elif schema_type == "number":
            if not isinstance(data, (int, float)) or isinstance(data, bool):
                errors.append(f"{loc}: expected number, got {type(data).__name__}")
        elif schema_type == "integer":
            if not isinstance(data, int) or isinstance(data, bool):
                errors.append(f"{loc}: expected integer, got {type(data).__name__}")
        return errors


class Validate(BaseStage):
    """Run a list of checks and collect all errors. No agent needed.

    All checks run even when earlier ones fail — the full error report is returned.
    ``StageResult.output`` is the list of error strings (empty list = all passed).
    The stage fails when any check reports at least one error.
    """

    needs_agent: bool = False

    def __init__(self, *, checks: list[Check]) -> None:
        self.checks = checks

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        errors: list[str] = []
        for check in self.checks:
            errors.extend(check.run(ctx))
        if errors:
            return StageResult(name="", success=False, output=errors, error="\n".join(errors))
        return StageResult(name="", success=True, output=[])
