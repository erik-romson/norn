from __future__ import annotations

import re

JAVA_STACKTRACE = re.compile(
    r'(?:Exception|Error|Throwable)[^\n]*\n'
    r'(?:\s+at\s+[\w.$<>]+\([\w.]+(?::\d+)?\)\n?)+',
    re.MULTILINE,
)

PYTHON_STACKTRACE = re.compile(
    r'Traceback \(most recent call last\):.*?'
    r'(?:\w+Error|\w+Exception)[^\n]*',
    re.DOTALL,
)


def extract_stacktraces(text: str) -> list[str]:
    """Extract Java and Python stacktraces from text."""
    traces: list[str] = []
    traces.extend(m.group(0) for m in JAVA_STACKTRACE.finditer(text))
    traces.extend(m.group(0) for m in PYTHON_STACKTRACE.finditer(text))
    return traces
