from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BuildConfig:
    """Base build configuration with a command string."""

    cmd: str


@dataclass
class Maven(BuildConfig):
    java_version: int = 21
    cmd: str = "mvn verify -B"


@dataclass
class Npm(BuildConfig):
    cmd: str = "npm test"


@dataclass
class Gradle(BuildConfig):
    cmd: str = "./gradlew build"
