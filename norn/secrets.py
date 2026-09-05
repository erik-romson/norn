from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from norn.models import PipelineContext

log = logging.getLogger(__name__)


@dataclass
class CredentialSpec:
    """Specification for resolving a named credential."""

    name: str
    org: str | None = None
    sources: list[str] | None = None
    required: bool = True


class SecretsManager(ABC):
    """Abstract base for pluggable secret backends."""

    @abstractmethod
    async def get(self, name: str, org: str | None = None) -> str | None:
        """Retrieve a secret value. Return ``None`` if not found."""
        ...


_secret_managers: list[SecretsManager] = []


def register_secrets_manager(manager: SecretsManager) -> None:
    """Register a SecretsManager to be consulted by ``resolve_credential``."""
    _secret_managers.append(manager)


async def resolve_credential(spec: CredentialSpec) -> str | None:
    """Resolve a credential using a layered lookup strategy.

    Lookup order:
    1. Org-specific env var (``ISSUEPROC_{ORG}_{NAME}``).
    2. Global env var (``{NAME}``).
    3. Registered SecretsManager instances.
    4. macOS Keychain (``issueprocessing/{org|global}/{name}``).
    5. Interactive prompt (when not in headless / CI mode).

    Raises:
        ValueError: if the credential is required and not found.
    """
    # 1. Org-specific env var
    if spec.org:
        org_upper = spec.org.upper().replace("-", "_")
        env_key = f"ISSUEPROC_{org_upper}_{spec.name}"
        val = os.environ.get(env_key)
        if val:
            return val

    # 2. Global env var
    val = os.environ.get(spec.name)
    if val:
        return val

    # 3. Registered secrets managers
    for manager in _secret_managers:
        val = await manager.get(spec.name, org=spec.org)
        if val:
            return val

    # 4. macOS Keychain
    val = await _keychain_get(f"issueprocessing/{spec.org or 'global'}/{spec.name}")
    if val:
        return val

    # 5. Interactive prompt (non-headless)
    headless = os.environ.get("CI") or os.environ.get("ISSUEPROC_HEADLESS")
    if not headless:
        from norn import ui
        return ui.console.input(f"Enter credential [{spec.name}]: ", password=True)

    if spec.required:
        raise ValueError(
            f"Required credential {spec.name!r} not found for org {spec.org!r}"
        )
    return None


async def _keychain_get(service: str) -> str | None:
    """Look up a password in the macOS Keychain by service name."""
    import sys
    if sys.platform != "darwin":
        return None
    proc = await asyncio.create_subprocess_exec(
        "security",
        "find-generic-password",
        "-s",
        service,
        "-w",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode == 0:
        return stdout.decode().strip()
    return None


def resolve_secret(name: str, source: str) -> str:
    """Resolve a secret value from the given source.

    Args:
        name: The secret name (used as env var name for ``env`` source,
              keychain account for ``keychain`` source, etc.)
        source: One of ``env``, ``keychain``, ``file``, ``prompt``.

    Raises:
        RuntimeError: if the secret cannot be resolved.
    """
    if source == "env":
        value = os.environ.get(name)
        if value is None:
            raise RuntimeError(f"Secret '{name}': environment variable '{name}' is not set")
        return value

    if source == "keychain":
        result = subprocess.run(
            ["security", "find-generic-password", "-a", name, "-w"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Secret '{name}': keychain lookup failed: {result.stderr.strip()}"
            )
        return result.stdout.strip()

    if source == "file":
        env_file = ".env"
        try:
            with open(env_file) as f:
                for line in f:
                    stripped = line.strip()
                    if stripped.startswith(f"{name}="):
                        return stripped[len(name) + 1:]
        except OSError as e:
            raise RuntimeError(f"Secret '{name}': cannot read .env file: {e}") from e
        raise RuntimeError(f"Secret '{name}': not found in '{env_file}'")

    if source == "prompt":
        from norn import ui

        return ui.console.input(f"Enter secret [{name}]: ", password=True)

    raise RuntimeError(
        f"Secret '{name}': unknown source {source!r}. Supported: env, keychain, file, prompt"
    )


def resolve_env(env: dict[str, str], ctx: PipelineContext) -> dict[str, str]:
    """Resolve ``{secret.NAME}`` and ``{param.NAME}`` placeholders in env dict values.

    Args:
        env: Environment variable dict, may contain ``{secret.NAME}`` or ``{param.NAME}``
             placeholders as values.
        ctx: Pipeline context carrying resolved secrets and params.

    Raises:
        RuntimeError: if a ``{secret.NAME}`` placeholder references an undeclared secret.
    """

    def _resolve_value(val: str) -> str:
        def replacer(m: re.Match[str]) -> str:
            prefix, key = m.group(1), m.group(2)
            if prefix == "secret":
                if key not in ctx.secrets:
                    raise RuntimeError(
                        f"Secret '{key}' not found — declare it with .secret() on the Pipeline"
                    )
                return ctx.secrets[key]
            if prefix == "param":
                return ctx.params.get(key, m.group(0))
            return m.group(0)

        return re.sub(r"\{(\w+)\.(\w+)\}", replacer, val)

    return {k: _resolve_value(v) for k, v in env.items()}
