from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from norn.models import PipelineContext
    from norn.profiles import SessionProfile
    from norn.stages.base import BaseStage

from norn.skills import Skill
from norn.templates import PromptTemplate


class OnFailure(Enum):
    """What to do when a stage or loop exhausts retries."""

    FAIL = auto()
    ASK_USER = auto()
    DRAFT_PR = auto()


# Convenience sentinels for the DSL
fail = OnFailure.FAIL
ask_user = OnFailure.ASK_USER
draft_pr = OnFailure.DRAFT_PR


@dataclass
class Budget:
    """Cost or token budget for a pipeline run.

    Added via ``Pipeline.budget()``. The runner checks all budgets after
    every stage that reports usage. Multiple budgets can be stacked.

    Attributes:
        max_cost_usd: Maximum total API cost in USD (``None`` = no limit).
        max_tokens: Maximum total tokens (input + output, ``None`` = no limit).
        on_exceed: What to do when the limit is crossed — ``FAIL`` raises
            ``BudgetExceededError``, ``ASK_USER`` prompts interactively.
    """

    max_cost_usd: float | None = None
    max_tokens: int | None = None
    on_exceed: OnFailure = OnFailure.FAIL


class ClearContext:
    """Marker that tells the runner to discard the current agent session.

    Inserted into the pipeline via ``Pipeline.clear_context()``. When the
    runner encounters this item it sets ``session_id = None`` so the next
    Generate stage starts a fresh conversation. Structured stage outputs
    (``StageResult``) are not affected — only the agent's memory is shed.
    """


@dataclass
class Stage:
    """A named stage wrapping a BaseStage implementation.

    Stages are the atomic units of work in a pipeline. They can appear
    at the top level, inside loops, or inside parallel blocks.

    Attributes:
        name: Human-readable name (lowercase, may contain spaces).
        impl: The ``BaseStage`` implementation to execute.
        on_failure: ``FAIL`` raises ``PipelineError``, ``ASK_USER`` prompts
            interactively.
        when: Optional predicate ``(ctx) -> bool``. Stage is skipped when
            it returns ``False``. Use built-in helpers like
            ``stage_succeeded()``, ``stage_failed()``, ``file_exists()``.
        timeout: Seconds before the stage is cancelled via
            ``asyncio.wait_for``. ``None`` means no timeout.
    """

    name: str
    impl: BaseStage
    on_failure: OnFailure = OnFailure.FAIL
    when: Callable[[PipelineContext], bool] | None = None
    timeout: float | None = None


@dataclass
class Loop:
    """A do-while retry loop wrapping a list of stages.

    If any stage in the loop fails, the entire loop restarts from the top.
    Agent-backed stages within the same loop share a session so Claude
    remembers prior errors when retrying.

    Attributes:
        name: Human-readable name for the loop.
        stages: Ordered list of stages to execute each attempt.
        max_retries: Maximum number of attempts (including the first).
        on_exhaust: What to do when retries run out — ``FAIL`` raises
            ``RetriesExhaustedError``, ``ASK_USER`` prompts interactively.
        timeout: Seconds before the entire loop is cancelled.
        new_session: When ``True``, the loop starts with a fresh agent session
            instead of inheriting the current pipeline session. The session
            from the preceding stage is not passed through.
    """

    name: str
    stages: list[Stage]
    max_retries: int = 3
    on_exhaust: OnFailure = OnFailure.FAIL
    timeout: float | None = None
    new_session: bool = False


@dataclass
class Parallel:
    """Run stages concurrently via ``asyncio.gather()``.

    Each stage starts its own agent session (session_id=None).
    All results are stored in context for downstream stages.

    If ``fail_fast=True`` (default), a PipelineError is raised on the first
    failure found after all tasks complete. If ``fail_fast=False``, all
    failures are collected and the first is raised.
    """

    name: str
    stages: list[Stage]
    fail_fast: bool = True


@dataclass
class Include:
    """Include a sub-pipeline from an external Python file.

    Args:
        path: Path to the sub-pipeline ``.py`` file (relative to CWD or absolute).
        isolated: If ``False`` (default), stages are flattened inline and share the
                  parent context and session. If ``True``, the sub-pipeline runs in a
                  fresh context with a forked agent session.
        outputs: Stage names whose results are copied back to the parent context
                 (only meaningful when ``isolated=True``).
        args: Named parameters merged into the sub-pipeline's ``ctx.params``.
    """

    path: str
    isolated: bool = False
    outputs: list[str] = field(default_factory=list)
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class SecretSpec:
    """Declaration of a named secret to be resolved before the pipeline runs.

    Added via ``Pipeline.secret()``. The runner resolves all secrets at
    pipeline start and stores them on ``PipelineContext.secrets``. Values
    are automatically masked in all UI output.

    Attributes:
        name: Secret name. Used as the env var name for ``env`` source,
            keychain account for ``keychain`` source, key in ``.env`` for
            ``file`` source, and prompt label for ``prompt`` source.
        source: Resolution method — ``"env"``, ``"keychain"``, ``"file"``,
            or ``"prompt"``.
    """

    name: str
    source: str  # "env", "keychain", "file", "prompt"


@dataclass
class ContextSpec:
    """Spec for content to inject into Generate stage prompts or system prompts.

    Resolved at pipeline start and stored on ``PipelineContext.injected_context``.
    """

    source: str  # file path, glob pattern, or shell command
    label: str  # section header shown when injecting into the prompt
    kind: str  # "file" (path/glob) or "cmd" (shell command)


# Union of items that can appear in a pipeline
PipelineItem = Stage | Loop | ClearContext | Parallel | Include


@dataclass
class Pipeline:
    """Root container for a pipeline definition, built via chained DSL calls.

    Pipeline configs are Python files that define a ``config`` variable of
    this type. The CLI loads them dynamically via ``importlib``. All builder
    methods return ``self`` for fluent chaining.

    Attributes:
        name: Pipeline name (lowercase with underscores).
        items: Ordered list of pipeline items (stages, loops, parallels,
            includes, clear-context markers).
        alert_channels: Alert channels to notify on lifecycle events.
        hooks: Pipeline-level lifecycle hooks keyed by event name.
        budgets: Cost/token budgets checked after every stage.
        contexts: Files/commands to inject into Generate prompts.
        env_vars: Static environment variables for all stages.
        secret_specs: Secrets to resolve at pipeline start.
        default_model: Fallback model for Generate stages without an
            explicit ``model`` parameter.
        pipeline_skills: Skills applied to all Generate stages.
    """

    name: str
    items: list[PipelineItem] = field(default_factory=list)
    alert_channels: list[Any] = field(default_factory=list)
    hooks: dict[str, list[BaseStage]] = field(default_factory=dict)
    budgets: list[Budget] = field(default_factory=list)
    contexts: list[ContextSpec] = field(default_factory=list)
    env_vars: dict[str, str] = field(default_factory=dict)
    secret_specs: list[SecretSpec] = field(default_factory=list)
    default_model: str | None = None
    pipeline_skills: list[str | Skill] = field(default_factory=list)
    github_org: str | None = None
    project_keys: list[str] = field(default_factory=list)
    _credential_provider: str = field(default="env", init=False, repr=False, compare=False)
    _credential_kwargs: dict = field(default_factory=dict, init=False, repr=False, compare=False)
    _session_profile: SessionProfile | None = field(default=None, init=False, repr=False, compare=False)

    def budget(
        self,
        *,
        max_cost_usd: float | None = None,
        max_tokens: int | None = None,
        on_exceed: OnFailure = OnFailure.FAIL,
    ) -> Pipeline:
        """Add a cost or token budget. Checked after every stage."""
        self.budgets.append(Budget(max_cost_usd=max_cost_usd, max_tokens=max_tokens, on_exceed=on_exceed))
        return self

    def hook(self, event: str, impl: BaseStage) -> Pipeline:
        """Register a pipeline-level lifecycle hook for the given event.

        Supported events: ``pre_stage``, ``post_stage``, ``on_retry``, ``on_failure``.
        Multiple hooks for the same event are run in registration order.
        """
        self.hooks.setdefault(event, []).append(impl)
        return self

    def alert(self, channel: Any) -> Pipeline:
        """Add a single alert channel (e.g. ``SlackChannel`` or ``MacOSChannel``)."""
        self.alert_channels.append(channel)
        return self

    def alerts(self, channels: list[Any]) -> Pipeline:
        """Add multiple alert channels at once."""
        self.alert_channels.extend(channels)
        return self

    def stage(
        self,
        name: str,
        impl: BaseStage | str,
        on_failure: OnFailure = OnFailure.FAIL,
        when: Callable[[PipelineContext], bool] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Pipeline:
        if isinstance(impl, str):
            from norn.registry import get_stage_class
            impl = get_stage_class(impl)(**kwargs)
        self.items.append(Stage(name=name, impl=impl, on_failure=on_failure, when=when, timeout=timeout))
        return self

    def loop(
        self,
        name: str,
        *,
        stages: list[Stage],
        max_retries: int = 3,
        on_exhaust: OnFailure = OnFailure.FAIL,
        timeout: float | None = None,
        new_session: bool = False,
    ) -> Pipeline:
        self.items.append(
            Loop(name=name, stages=stages, max_retries=max_retries, on_exhaust=on_exhaust,
                 timeout=timeout, new_session=new_session)
        )
        return self

    def parallel(
        self,
        name: str,
        *,
        stages: list[Stage],
        fail_fast: bool = True,
    ) -> Pipeline:
        self.items.append(Parallel(name=name, stages=stages, fail_fast=fail_fast))
        return self

    def include(
        self,
        path: str,
        *,
        isolated: bool = False,
        outputs: list[str] | None = None,
        args: dict[str, Any] | None = None,
    ) -> Pipeline:
        """Include a sub-pipeline from an external file.

        Args:
            path: Path to the sub-pipeline ``.py`` file.
            isolated: Run in a fresh context with a forked agent session.
            outputs: Stage names to copy back to the parent context (isolated only).
            args: Named parameters for the sub-pipeline.
        """
        self.items.append(
            Include(path=path, isolated=isolated, outputs=list(outputs or []), args=dict(args or {}))
        )
        return self

    def context(self, path: str, *, label: str | None = None) -> Pipeline:
        """Inject a file or glob pattern into Generate stage prompts at pipeline start.

        For stages with tools (``permission_mode`` set) the content is passed as
        ``system_prompt``; for pure-prompt stages it is prepended to the prompt.
        """
        self.contexts.append(ContextSpec(source=path, label=label or path, kind="file"))
        return self

    def context_cmd(self, cmd: str, *, label: str | None = None) -> Pipeline:
        """Run a shell command and inject its output into Generate stage prompts.

        The command is executed once at pipeline start.
        """
        self.contexts.append(ContextSpec(source=cmd, label=label or cmd, kind="cmd"))
        return self

    def env(self, name: str, value: str) -> Pipeline:
        """Set a static environment variable available to all stages via ``ctx.env``.

        The value is passed as-is (not treated as a secret and not masked).
        Use ``.secret()`` for sensitive values.
        """
        self.env_vars[name] = value
        return self

    def secret(self, name: str, *, source: str) -> Pipeline:
        """Declare a named secret to be resolved at pipeline start.

        Args:
            name: The secret name. Use ``{secret.NAME}`` in stage ``env`` dicts to
                  inject it into a subprocess or agent execution environment.
            source: Where to read the value from — one of ``env``, ``keychain``,
                    ``file``, ``prompt``.
        """
        self.secret_specs.append(SecretSpec(name=name, source=source))
        return self

    def skills(self, skill_list: list[str | Skill]) -> Pipeline:
        """Apply skills to all Generate stages in this pipeline.

        Pipeline-level skills are merged with any stage-level skills in
        ``Generate``.  They are prepended to the agent's ``system_prompt``
        so the LLM has the skill instructions before seeing the prompt.
        """
        self.pipeline_skills.extend(skill_list)
        return self

    def clear_context(self) -> Pipeline:
        self.items.append(ClearContext())
        return self

    def derive(self, name: str) -> Pipeline:
        """Return a shallow copy of this pipeline with a new name.

        Items are deep-copied so mutations on the derived pipeline do not
        affect the parent.
        """
        derived = Pipeline(
            name=name,
            items=copy.deepcopy(self.items),
            alert_channels=list(self.alert_channels),
            hooks={k: list(v) for k, v in self.hooks.items()},
            budgets=list(self.budgets),
            contexts=list(self.contexts),
            env_vars=dict(self.env_vars),
            secret_specs=list(self.secret_specs),
            default_model=self.default_model,
            pipeline_skills=list(self.pipeline_skills),
            github_org=self.github_org,
            project_keys=list(self.project_keys),
        )
        derived._credential_provider = self._credential_provider
        derived._credential_kwargs = dict(self._credential_kwargs)
        derived._session_profile = self._session_profile
        return derived

    def projects(self, *keys: str) -> Pipeline:
        """Register one or more project keys handled by this pipeline."""
        self.project_keys.extend(keys)
        return self

    def credentials(self, *, provider: str = "env", **kwargs: Any) -> Pipeline:
        """Set the credential provider and optional kwargs for this pipeline."""
        self._credential_provider = provider
        self._credential_kwargs = dict(kwargs)
        return self

    def session_profile(self, profile: SessionProfile) -> Pipeline:
        """Apply a SessionProfile to all Generate stages in this pipeline."""
        self._session_profile = profile
        return self

    def skip(self, name: str) -> Pipeline:
        """Remove the stage or loop with the given name from items."""
        self.items = [
            item for item in self.items
            if not (isinstance(item, (Stage, Loop, Parallel)) and item.name == name)
        ]
        return self

    def replace(self, name: str, impl: BaseStage) -> Pipeline:
        """Replace the implementation of a top-level stage by name."""
        for item in self.items:
            if isinstance(item, Stage) and item.name == name:
                item.impl = impl
                return self
        raise KeyError(f"Stage {name!r} not found in pipeline {self.name!r}")

    def in_loop(self, path: str) -> LoopEditor:
        """Return a LoopEditor for the loop with the given name."""
        loop = self._find_loop(path)
        return LoopEditor(loop, self)

    def _find_loop(self, path: str) -> Loop:
        """Find a Loop by dot-notation path, traversing nested loops.

        A simple name like ``"build"`` looks in top-level items. A dotted path
        like ``"deliver.code_test"`` first finds the ``deliver`` loop, then
        looks inside its stages for ``code_test``.
        """
        parts = path.split(".")
        current_items: list[PipelineItem] = self.items
        found: Loop | None = None
        for part in parts:
            found = None
            for item in current_items:
                if isinstance(item, Loop) and item.name == part:
                    found = item
                    break
            if found is None:
                available = [item.name for item in current_items if isinstance(item, Loop)]
                raise KeyError(
                    f"Loop {part!r} not found (path={path!r}); available loops: {available}"
                )
            current_items = list(found.stages)
        assert found is not None  # parts is non-empty, so found was set
        return found


class LoopEditor:
    """Fluent editor for modifying stages inside a Loop.

    Obtained via ``Pipeline.in_loop(loop_name)``. Supports ``skip()``,
    ``replace()``, ``insert_after()``, ``insert_before()``, and
    ``end_loop()`` to return to the pipeline.
    """

    def __init__(self, loop: Loop, pipeline: Pipeline) -> None:
        self._loop = loop
        self._pipeline = pipeline

    def skip(self, name: str) -> LoopEditor:
        """Remove the stage with the given name from the loop. No-op if not found."""
        self._loop.stages = [s for s in self._loop.stages if s.name != name]
        return self

    def replace(self, name: str, impl: BaseStage) -> LoopEditor:
        """Replace the implementation of a stage inside the loop by name."""
        for stage in self._loop.stages:
            if stage.name == name:
                stage.impl = impl
                return self
        raise KeyError(f"Stage {name!r} not found in loop {self._loop.name!r}")

    def insert_after(self, name: str, new_stage: Stage) -> LoopEditor:
        """Insert a new stage immediately after the named stage."""
        for i, stage in enumerate(self._loop.stages):
            if stage.name == name:
                self._loop.stages.insert(i + 1, new_stage)
                return self
        raise KeyError(f"Stage {name!r} not found in loop {self._loop.name!r}")

    def insert_before(self, name: str, new_stage: Stage) -> LoopEditor:
        """Insert a new stage immediately before the named stage."""
        for i, stage in enumerate(self._loop.stages):
            if stage.name == name:
                self._loop.stages.insert(i, new_stage)
                return self
        raise KeyError(f"Stage {name!r} not found in loop {self._loop.name!r}")

    def end_loop(self) -> Pipeline:
        """Return to the pipeline for further chaining."""
        return self._pipeline


# ---------------------------------------------------------------------------
# Built-in condition predicates
# ---------------------------------------------------------------------------


def stage_succeeded(name: str) -> Callable[[PipelineContext], bool]:
    """Predicate: true if the named stage has run and succeeded."""
    return lambda ctx: name in ctx.results and ctx.results[name].success


def stage_failed(name: str) -> Callable[[PipelineContext], bool]:
    """Predicate: true if the named stage has run and failed."""
    return lambda ctx: name in ctx.results and not ctx.results[name].success


def output_contains(stage_name: str, text: str) -> Callable[[PipelineContext], bool]:
    """Predicate: true if the named stage's output contains *text*."""
    return lambda ctx: text in str(ctx.results[stage_name].output if stage_name in ctx.results else "")


def file_exists(path: str) -> Callable[[PipelineContext], bool]:
    """Predicate: true if *path* exists on disk at evaluation time."""
    return lambda ctx: os.path.exists(path)
