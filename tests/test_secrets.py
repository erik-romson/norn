from __future__ import annotations

import pytest

from norn.models import PipelineContext
from norn.secrets import CredentialSpec, SecretsManager, register_secrets_manager, resolve_credential, resolve_env, resolve_secret, _secret_managers


# ---------------------------------------------------------------------------
# resolve_secret
# ---------------------------------------------------------------------------


def test_resolve_env_source(monkeypatch):
    monkeypatch.setenv("TEST_SECRET_VAR", "my_value")
    assert resolve_secret("TEST_SECRET_VAR", "env") == "my_value"


def test_resolve_env_source_missing(monkeypatch):
    monkeypatch.delenv("TEST_SECRET_VAR", raising=False)
    with pytest.raises(RuntimeError, match="TEST_SECRET_VAR"):
        resolve_secret("TEST_SECRET_VAR", "env")


def test_resolve_unknown_source():
    with pytest.raises(RuntimeError, match="unknown source"):
        resolve_secret("SOME_SECRET", "magic")


def test_resolve_file_source(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("MY_KEY=my_value\nOTHER=other_val\n")
    monkeypatch.chdir(tmp_path)
    assert resolve_secret("MY_KEY", "file") == "my_value"


def test_resolve_file_source_missing_key(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER=value\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="not found"):
        resolve_secret("MISSING_KEY", "file")


def test_resolve_file_source_no_env_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="cannot read .env file"):
        resolve_secret("ANY_KEY", "file")


# ---------------------------------------------------------------------------
# resolve_env
# ---------------------------------------------------------------------------


def test_resolve_env_substitutes_secret_placeholder():
    ctx = PipelineContext()
    ctx.secrets = {"TOKEN": "abc123"}
    result = resolve_env({"API_KEY": "{secret.TOKEN}"}, ctx)
    assert result == {"API_KEY": "abc123"}


def test_resolve_env_substitutes_param_placeholder():
    ctx = PipelineContext(params={"region": "us-east-1"})
    result = resolve_env({"AWS_REGION": "{param.region}"}, ctx)
    assert result == {"AWS_REGION": "us-east-1"}


def test_resolve_env_missing_secret_raises():
    ctx = PipelineContext()
    with pytest.raises(RuntimeError, match="Secret 'MISSING'"):
        resolve_env({"KEY": "{secret.MISSING}"}, ctx)


def test_resolve_env_plain_value_unchanged():
    ctx = PipelineContext()
    result = resolve_env({"NODE_ENV": "production"}, ctx)
    assert result == {"NODE_ENV": "production"}


def test_resolve_env_unknown_placeholder_left_as_is():
    ctx = PipelineContext()
    result = resolve_env({"KEY": "{other.value}"}, ctx)
    assert result == {"KEY": "{other.value}"}


# ---------------------------------------------------------------------------
# resolve_credential
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_secret_managers():
    """Ensure the global secrets manager list is clean before each test."""
    original = list(_secret_managers)
    _secret_managers.clear()
    yield
    _secret_managers.clear()
    _secret_managers.extend(original)


@pytest.mark.asyncio
async def test_resolve_credential_org_specific_env_var(monkeypatch):
    monkeypatch.setenv("ISSUEPROC_ACME_GITHUB_TOKEN", "org_token")
    monkeypatch.setenv("GITHUB_TOKEN", "global_token")
    spec = CredentialSpec(name="GITHUB_TOKEN", org="acme")
    result = await resolve_credential(spec)
    assert result == "org_token"


@pytest.mark.asyncio
async def test_resolve_credential_global_env_var_fallback(monkeypatch):
    monkeypatch.delenv("ISSUEPROC_ACME_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "global_token")
    spec = CredentialSpec(name="GITHUB_TOKEN", org="acme")
    result = await resolve_credential(spec)
    assert result == "global_token"


@pytest.mark.asyncio
async def test_resolve_credential_no_org_uses_global_env(monkeypatch):
    monkeypatch.setenv("JIRA_TOKEN", "jira_secret")
    spec = CredentialSpec(name="JIRA_TOKEN")
    result = await resolve_credential(spec)
    assert result == "jira_secret"


@pytest.mark.asyncio
async def test_resolve_credential_org_hyphen_normalised(monkeypatch):
    monkeypatch.setenv("ISSUEPROC_MY_ORG_GITHUB_TOKEN", "hyphen_token")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    spec = CredentialSpec(name="GITHUB_TOKEN", org="my-org")
    result = await resolve_credential(spec)
    assert result == "hyphen_token"


@pytest.mark.asyncio
async def test_resolve_credential_uses_registered_secrets_manager(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("CI", "true")

    class StubManager(SecretsManager):
        async def get(self, name: str, org: str | None = None) -> str | None:
            if name == "GITHUB_TOKEN":
                return "manager_token"
            return None

    register_secrets_manager(StubManager())
    spec = CredentialSpec(name="GITHUB_TOKEN", org=None)
    result = await resolve_credential(spec)
    assert result == "manager_token"


@pytest.mark.asyncio
async def test_resolve_credential_required_raises_when_not_found(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ISSUEPROC_ACME_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("CI", "true")
    spec = CredentialSpec(name="GITHUB_TOKEN", org="acme", required=True)
    with pytest.raises(ValueError, match="GITHUB_TOKEN"):
        await resolve_credential(spec)


@pytest.mark.asyncio
async def test_resolve_credential_not_required_returns_none(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ISSUEPROC_ACME_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("CI", "true")
    spec = CredentialSpec(name="GITHUB_TOKEN", org="acme", required=False)
    result = await resolve_credential(spec)
    assert result is None


@pytest.mark.asyncio
async def test_resolve_credential_manager_not_consulted_when_env_found(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env_token")

    class BoomManager(SecretsManager):
        async def get(self, name: str, org: str | None = None) -> str | None:
            raise AssertionError("Should not be called")

    register_secrets_manager(BoomManager())
    spec = CredentialSpec(name="GITHUB_TOKEN")
    result = await resolve_credential(spec)
    assert result == "env_token"


# ---------------------------------------------------------------------------
# SecretsManager plugin: missing optional dependencies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aws_ssm_manager_raises_on_missing_aioboto3(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def _no_aioboto3(name, *args, **kwargs):
        if name == "aioboto3":
            raise ImportError("No module named 'aioboto3'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_aioboto3)
    from norn.secrets_managers.aws_ssm import AwsSsmManager
    manager = AwsSsmManager()
    with pytest.raises(ImportError, match="aioboto3"):
        await manager.get("GITHUB_TOKEN")


@pytest.mark.asyncio
async def test_vault_manager_raises_on_missing_hvac(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def _no_hvac(name, *args, **kwargs):
        if name == "hvac":
            raise ImportError("No module named 'hvac'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_hvac)
    from norn.secrets_managers.vault import VaultManager
    manager = VaultManager(url="https://vault.example.com")
    with pytest.raises(ImportError, match="hvac"):
        await manager.get("JIRA_TOKEN")
