from __future__ import annotations

import pytest

from norn.secrets import (
    CredentialSpec,
    SecretsManager,
    _secret_managers,
    register_secrets_manager,
    resolve_credential,
)


@pytest.fixture(autouse=True)
def clear_managers():
    """Reset registered secret managers between tests."""
    _secret_managers.clear()
    yield
    _secret_managers.clear()


# ---------------------------------------------------------------------------
# Org-specific env var
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_org_specific_env_var(monkeypatch):
    monkeypatch.setenv("ISSUEPROC_ACME_MY_TOKEN", "org_secret")
    spec = CredentialSpec(name="MY_TOKEN", org="acme")
    result = await resolve_credential(spec)
    assert result == "org_secret"


@pytest.mark.asyncio
async def test_org_specific_env_var_hyphen_in_org(monkeypatch):
    monkeypatch.setenv("ISSUEPROC_MY_ORG_MY_TOKEN", "hyphen_secret")
    spec = CredentialSpec(name="MY_TOKEN", org="my-org")
    result = await resolve_credential(spec)
    assert result == "hyphen_secret"


# ---------------------------------------------------------------------------
# Global env var
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_env_var(monkeypatch):
    monkeypatch.delenv("ISSUEPROC_ACME_MY_TOKEN", raising=False)
    monkeypatch.setenv("MY_TOKEN", "global_secret")
    spec = CredentialSpec(name="MY_TOKEN", org="acme")
    result = await resolve_credential(spec)
    assert result == "global_secret"


@pytest.mark.asyncio
async def test_global_env_var_no_org(monkeypatch):
    monkeypatch.setenv("PLAIN_TOKEN", "plain_val")
    spec = CredentialSpec(name="PLAIN_TOKEN")
    result = await resolve_credential(spec)
    assert result == "plain_val"


# ---------------------------------------------------------------------------
# Required=True raises when not found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_required_raises_when_not_found(monkeypatch):
    monkeypatch.delenv("MISSING_CRED", raising=False)
    monkeypatch.setenv("CI", "true")  # headless mode, no prompt
    spec = CredentialSpec(name="MISSING_CRED", required=True)
    with pytest.raises(ValueError, match="MISSING_CRED"):
        await resolve_credential(spec)


@pytest.mark.asyncio
async def test_not_required_returns_none_when_not_found(monkeypatch):
    monkeypatch.delenv("OPTIONAL_CRED", raising=False)
    monkeypatch.setenv("CI", "true")
    spec = CredentialSpec(name="OPTIONAL_CRED", required=False)
    result = await resolve_credential(spec)
    assert result is None


# ---------------------------------------------------------------------------
# Headless mode skips interactive prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_headless_ci_skips_prompt(monkeypatch):
    monkeypatch.delenv("NO_SUCH_VAR", raising=False)
    monkeypatch.setenv("CI", "1")
    spec = CredentialSpec(name="NO_SUCH_VAR", required=False)
    result = await resolve_credential(spec)
    assert result is None


@pytest.mark.asyncio
async def test_headless_issueproc_env_skips_prompt(monkeypatch):
    monkeypatch.delenv("NO_SUCH_VAR2", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("ISSUEPROC_HEADLESS", "1")
    spec = CredentialSpec(name="NO_SUCH_VAR2", required=False)
    result = await resolve_credential(spec)
    assert result is None


# ---------------------------------------------------------------------------
# Registered SecretsManager
# ---------------------------------------------------------------------------


class _FakeManager(SecretsManager):
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    async def get(self, name: str, org: str | None = None) -> str | None:
        return self._values.get(name)


@pytest.mark.asyncio
async def test_secrets_manager_consulted(monkeypatch):
    monkeypatch.delenv("MANAGER_SECRET", raising=False)
    monkeypatch.setenv("CI", "true")
    register_secrets_manager(_FakeManager({"MANAGER_SECRET": "from_manager"}))
    spec = CredentialSpec(name="MANAGER_SECRET", required=True)
    result = await resolve_credential(spec)
    assert result == "from_manager"


@pytest.mark.asyncio
async def test_env_takes_precedence_over_manager(monkeypatch):
    monkeypatch.setenv("PRIORITY_SECRET", "from_env")
    register_secrets_manager(_FakeManager({"PRIORITY_SECRET": "from_manager"}))
    spec = CredentialSpec(name="PRIORITY_SECRET")
    result = await resolve_credential(spec)
    assert result == "from_env"
