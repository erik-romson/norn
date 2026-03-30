"""Tests for bundled pipelines in norn/pipelines/."""
from __future__ import annotations


def test_pipelines_package_exists() -> None:
    """The norn.pipelines package is importable."""
    import norn.pipelines  # noqa: F401


def test_hello_has_metadata() -> None:
    """hello.py exposes a metadata dict."""
    from norn.pipelines import hello

    assert isinstance(hello.metadata, dict)
    assert "args" in hello.metadata


def test_vanilla_change_has_metadata() -> None:
    """vanilla_change.py exposes a metadata dict."""
    from norn.pipelines import vanilla_change

    assert isinstance(vanilla_change.metadata, dict)
    assert "env_vars" in vanilla_change.metadata
    assert "args" in vanilla_change.metadata


def test_implement_features_has_metadata() -> None:
    """implement_features.py exposes a metadata dict."""
    from norn.pipelines import implement_features

    assert isinstance(implement_features.metadata, dict)
    assert "env_vars" in implement_features.metadata
    assert "args" in implement_features.metadata
