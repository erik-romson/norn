---
test_cmd: python3 -c "pass"
---

# Fixture step

Exists only so `implement_features` builds a pipeline at import time. The step
name is deliberately unique so no `refactor:` commit in this repo's history
makes it resume-skipped. Nothing here is ever executed.
