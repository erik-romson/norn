from __future__ import annotations

from pathlib import Path

import pytest

from norn.contrib.build.detect import detect_build_command, detect_test_command
from norn.contrib.build.configs import BuildConfig, Maven, Npm, Gradle


def test_detect_maven(tmp_path: Path):
    (tmp_path / "pom.xml").touch()
    assert detect_build_command(tmp_path) == "mvn verify -B"


def test_detect_gradle(tmp_path: Path):
    (tmp_path / "build.gradle").touch()
    assert detect_build_command(tmp_path) == "./gradlew build"


def test_detect_gradle_kts(tmp_path: Path):
    (tmp_path / "build.gradle.kts").touch()
    assert detect_build_command(tmp_path) == "./gradlew build"


def test_detect_npm(tmp_path: Path):
    (tmp_path / "package.json").touch()
    assert detect_build_command(tmp_path) == "npm test"


def test_detect_python(tmp_path: Path):
    (tmp_path / "pyproject.toml").touch()
    assert detect_build_command(tmp_path) == "python -m pytest"


def test_detect_makefile(tmp_path: Path):
    (tmp_path / "Makefile").touch()
    assert detect_build_command(tmp_path) == "make test"


def test_detect_cargo(tmp_path: Path):
    (tmp_path / "Cargo.toml").touch()
    assert detect_build_command(tmp_path) == "cargo test"


def test_detect_unknown(tmp_path: Path):
    with pytest.raises(ValueError, match="Cannot auto-detect"):
        detect_build_command(tmp_path)


def test_detect_test_java_maven(tmp_path: Path):
    (tmp_path / "pom.xml").touch()
    cmd = detect_test_command(tmp_path, ["src/test/java/FooTest.java"])
    assert "mvn test" in cmd


def test_detect_test_python(tmp_path: Path):
    cmd = detect_test_command(tmp_path, ["tests/test_foo.py"])
    assert "pytest" in cmd


def test_detect_test_requires_repo_path():
    with pytest.raises(ValueError, match="repo_path is required"):
        detect_test_command(None, ["test.py"])


def test_build_config_hierarchy():
    maven = Maven(java_version=17)
    assert isinstance(maven, BuildConfig)
    assert maven.cmd == "mvn verify -B"

    npm = Npm()
    assert isinstance(npm, BuildConfig)
    assert npm.cmd == "npm test"

    gradle = Gradle()
    assert isinstance(gradle, BuildConfig)
    assert gradle.cmd == "./gradlew build"
