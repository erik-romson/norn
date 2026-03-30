from __future__ import annotations

import re

JAVA_CLASS_PATTERN = re.compile(
    r'at\s+([\w.]+)\.([\w$]+)\([\w.]+(?::\d+)?\)'
)

_FRAMEWORK_PREFIXES = (
    "java.", "javax.", "sun.", "com.sun.",
    "org.springframework.", "org.junit.", "org.mockito.",
    "io.netty.", "com.google.", "net.bytebuddy.",
)


def extract_class_names(stacktraces: list[str]) -> list[str]:
    """Extract unique fully qualified class names from Java stacktraces."""
    classes: set[str] = set()
    for trace in stacktraces:
        for match in JAVA_CLASS_PATTERN.finditer(trace):
            fqcn = match.group(1)
            if not fqcn.startswith(_FRAMEWORK_PREFIXES):
                classes.add(fqcn)
    return sorted(classes)
