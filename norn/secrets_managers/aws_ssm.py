from __future__ import annotations

from norn.secrets import SecretsManager


class AwsSsmManager(SecretsManager):
    """Retrieve secrets from AWS Systems Manager Parameter Store."""

    def __init__(self, prefix: str = "/issueprocessing") -> None:
        self.prefix = prefix

    async def get(self, name: str, org: str | None = None) -> str | None:
        try:
            import aioboto3
        except ImportError:
            raise ImportError("aioboto3 is required for AWS SSM: uv add aioboto3")
        path = f"{self.prefix}/{org or 'global'}/{name}"
        session = aioboto3.Session()
        async with session.client("ssm") as ssm:
            try:
                resp = await ssm.get_parameter(Name=path, WithDecryption=True)
                return resp["Parameter"]["Value"]
            except Exception:
                return None
