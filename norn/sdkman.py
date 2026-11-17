"""Pin a command's tools to the ``.sdkmanrc`` of the directory it runs in.

A repository that pins its JDK (or Maven, Gradle, ...) in ``.sdkmanrc`` builds
with those versions only after ``sdk env`` has run in the shell. norn's
commands inherit the norn process's environment instead, so a run started from
a shell on another default JDK built with that one, and a build whose enforcer
requires the pinned version failed before compiling anything.

``sdkman_env`` does what ``sdk env`` does, without sourcing SDKMAN's shell
functions: for every ``candidate=version`` line it puts
``$SDKMAN_DIR/candidates/<candidate>/<version>/bin`` first on ``PATH``, drops
the candidate's other versions from ``PATH``, and sets ``<CANDIDATE>_HOME``.
A pinned version that is not installed is an error, never a reason to fall back
to whatever is on ``PATH``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

SDKMANRC = ".sdkmanrc"

# A candidate and a version are single path segments under $SDKMAN_DIR/candidates:
# no separators and no "..", so a line can only name an installed SDKMAN version.
_CANDIDATE = re.compile(r"[a-z][a-z0-9]*")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")


class SdkmanError(RuntimeError):
    """The ``.sdkmanrc`` cannot be applied: a malformed line or a missing version."""


def sdkman_env(directory: str | os.PathLike[str], env: Mapping[str, str]) -> dict[str, str]:
    """Return the variables that apply *directory*'s ``.sdkmanrc`` on top of *env*.

    Returns an empty dict when *directory* has no ``.sdkmanrc``. The SDKMAN
    root is ``env["SDKMAN_DIR"]``, or ``~/.sdkman`` when that is unset.

    Raises:
        SdkmanError: a line is not ``candidate=version``, or a pinned version
            is not installed.
    """
    rc = Path(directory) / SDKMANRC
    if not rc.is_file():
        return {}
    candidates = Path(env.get("SDKMAN_DIR") or Path.home() / ".sdkman") / "candidates"
    path = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    updates: dict[str, str] = {}
    for lineno, raw in enumerate(rc.read_text().splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, sep, version = (part.strip() for part in line.partition("="))
        if not sep or not _CANDIDATE.fullmatch(name) or not _VERSION.fullmatch(version):
            raise SdkmanError(f"{rc}:{lineno}: expected candidate=version, got {raw!r}")
        home = candidates / name / version
        if not home.is_dir():
            raise SdkmanError(
                f"{rc} pins {name} {version}, which is not installed at {home}: "
                f"run `sdk env install` in {rc.parent}"
            )
        other_versions = str(candidates / name) + os.sep
        path = [str(home / "bin")] + [p for p in path if not p.startswith(other_versions)]
        updates[f"{name.upper()}_HOME"] = str(home)
    if updates:
        updates["PATH"] = os.pathsep.join(path)
    return updates
