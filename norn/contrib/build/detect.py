from __future__ import annotations

from pathlib import Path


def detect_build_command(repo_path: Path) -> str:
    """Detect build system from project files."""
    if (repo_path / "pom.xml").exists():
        return "mvn verify -B"
    if (repo_path / "build.gradle").exists() or (repo_path / "build.gradle.kts").exists():
        return "./gradlew build"
    if (repo_path / "package.json").exists():
        return "npm test"
    if (repo_path / "pyproject.toml").exists():
        return "python -m pytest"
    if (repo_path / "Makefile").exists():
        return "make test"
    if (repo_path / "Cargo.toml").exists():
        return "cargo test"
    raise ValueError(f"Cannot auto-detect build system in {repo_path}")


def detect_test_command(repo_path: Path | None, test_files: list[str]) -> str:
    """Detect test runner for specific test files."""
    if repo_path is None:
        raise ValueError("repo_path is required for test command detection")
    if any(f.endswith(".java") for f in test_files):
        if (repo_path / "pom.xml").exists():
            class_names = [f.replace("/", ".").removesuffix(".java") for f in test_files]
            return f"mvn test -B -Dtest={','.join(class_names)}"
        return f"./gradlew test --tests {' '.join(test_files)}"
    if any(f.endswith(".py") for f in test_files):
        return f"python -m pytest {' '.join(test_files)} -v"
    if any(f.endswith((".js", ".ts")) for f in test_files):
        return f"npm test -- {' '.join(test_files)}"
    return detect_build_command(repo_path)
