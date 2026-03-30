from __future__ import annotations

import json
import logging
import re

from norn.contrib.models.fix_plan import FileChange, FixPlan

log = logging.getLogger(__name__)


def parse_fix_plan(text: str) -> FixPlan:
    """Parse LLM JSON output into a FixPlan. Lenient — returns partial plan on error."""
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if not m:
        return FixPlan(analysis=text)
    try:
        data = json.loads(m.group(0))
        files_to_change = [
            FileChange(
                path=fc.get("path", ""),
                description=fc.get("description", ""),
                reason=fc.get("reason", ""),
            )
            for fc in data.get("files_to_change", [])
        ]
        return FixPlan(
            analysis=data.get("analysis", text),
            files_to_change=files_to_change,
            test_strategy=data.get("test_strategy", ""),
            test_files=data.get("test_files", []),
            risks=data.get("risks", []),
            confidence=float(data.get("confidence", 0.0)),
        )
    except (json.JSONDecodeError, TypeError) as e:
        log.warning("Failed to parse fix plan JSON: %s", e)
        return FixPlan(analysis=text)
