from __future__ import annotations

import asyncio

from norn.secrets import SecretsManager


class VaultManager(SecretsManager):
    """Retrieve secrets from HashiCorp Vault (KV v2)."""

    def __init__(self, url: str, mount: str = "secret") -> None:
        self.url = url
        self.mount = mount

    async def get(self, name: str, org: str | None = None) -> str | None:
        try:
            import hvac
        except ImportError:
            raise ImportError("hvac is required for HashiCorp Vault: uv add hvac")
        loop = asyncio.get_event_loop()

        def _fetch() -> str | None:
            client = hvac.Client(url=self.url)
            path = f"issueprocessing/{org or 'global'}"
            try:
                secret = client.secrets.kv.v2.read_secret_version(
                    path=path, mount_point=self.mount
                )
                return secret["data"]["data"].get(name)
            except Exception:
                return None

        return await loop.run_in_executor(None, _fetch)
