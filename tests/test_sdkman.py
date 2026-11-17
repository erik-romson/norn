from __future__ import annotations

import os
from pathlib import Path

import pytest

from norn.models import PipelineContext
from norn.sdkman import SdkmanError, sdkman_env
from norn.stages.run_command import RunCommand


def _install(sdkman: Path, candidate: str, version: str, tool: str | None = None) -> Path:
    """Create a fake SDKMAN candidate whose tool prints its own version."""
    bin_dir = sdkman / "candidates" / candidate / version / "bin"
    bin_dir.mkdir(parents=True)
    if tool:
        script = bin_dir / tool
        script.write_text(f"#!/bin/sh\necho {tool} {version}\n")
        script.chmod(0o755)
    return bin_dir.parent


def test_no_sdkmanrc_changes_nothing(tmp_path):
    assert sdkman_env(tmp_path, {"PATH": "/usr/bin"}) == {}


def test_pinned_version_goes_first_and_replaces_the_current_one(tmp_path):
    sdkman = tmp_path / "sdkman"
    home = _install(sdkman, "java", "11.0.26-zulu")
    current = sdkman / "candidates" / "java" / "current" / "bin"
    (tmp_path / ".sdkmanrc").write_text("# comment\n\njava=11.0.26-zulu  # pinned\n")

    env = sdkman_env(tmp_path, {"SDKMAN_DIR": str(sdkman), "PATH": f"{current}:/usr/bin"})

    assert env["JAVA_HOME"] == str(home)
    assert env["PATH"].split(os.pathsep) == [str(home / "bin"), "/usr/bin"]


def test_every_candidate_is_applied(tmp_path):
    sdkman = tmp_path / "sdkman"
    java = _install(sdkman, "java", "11.0.26-zulu")
    maven = _install(sdkman, "maven", "3.9.9")
    (tmp_path / ".sdkmanrc").write_text("java=11.0.26-zulu\nmaven=3.9.9\n")

    env = sdkman_env(tmp_path, {"SDKMAN_DIR": str(sdkman), "PATH": "/usr/bin"})

    assert env["JAVA_HOME"] == str(java)
    assert env["MAVEN_HOME"] == str(maven)
    assert env["PATH"].split(os.pathsep)[:2] == [str(maven / "bin"), str(java / "bin")]


def test_a_missing_version_fails_and_names_the_install_command(tmp_path):
    (tmp_path / ".sdkmanrc").write_text("java=11.0.26-zulu\n")
    with pytest.raises(SdkmanError, match="sdk env install"):
        sdkman_env(tmp_path, {"SDKMAN_DIR": str(tmp_path / "sdkman"), "PATH": "/usr/bin"})


@pytest.mark.parametrize("line", ["java", "java=", "java=../../../usr", "java=/usr/lib/jvm", "../x=1"])
def test_a_malformed_line_fails(tmp_path, line):
    (tmp_path / ".sdkmanrc").write_text(f"{line}\n")
    with pytest.raises(SdkmanError, match=r"\.sdkmanrc:1: expected candidate=version"):
        sdkman_env(tmp_path, {"SDKMAN_DIR": str(tmp_path / "sdkman"), "PATH": "/usr/bin"})


@pytest.mark.asyncio
async def test_run_command_uses_the_pinned_version(tmp_path, monkeypatch):
    """A command runs the working directory's pinned JDK, not the one norn was started with."""
    sdkman = tmp_path / "sdkman"
    home = _install(sdkman, "java", "11.0.26-zulu", tool="java")
    _install(sdkman, "java", "25.0.3", tool="java")
    current = sdkman / "candidates" / "java" / "25.0.3" / "bin"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".sdkmanrc").write_text("java=11.0.26-zulu\n")
    monkeypatch.setenv("SDKMAN_DIR", str(sdkman))
    monkeypatch.setenv("PATH", f"{current}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("JAVA_HOME", str(current.parent))

    result = await RunCommand(cmd='java; echo "$JAVA_HOME"').run(
        PipelineContext(working_dir=str(repo))
    )

    assert result.success, result.output
    assert result.output["stdout"].split() == ["java", "11.0.26-zulu", str(home)]


@pytest.mark.asyncio
async def test_pipeline_env_still_wins_over_the_sdkmanrc(tmp_path, monkeypatch):
    sdkman = tmp_path / "sdkman"
    _install(sdkman, "java", "11.0.26-zulu")
    (tmp_path / ".sdkmanrc").write_text("java=11.0.26-zulu\n")
    monkeypatch.setenv("SDKMAN_DIR", str(sdkman))

    result = await RunCommand(cmd='echo "$JAVA_HOME"').run(
        PipelineContext(working_dir=str(tmp_path), env={"JAVA_HOME": "/explicit"})
    )

    assert result.output["stdout"].strip() == "/explicit"
