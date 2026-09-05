---
test_cmd: python3 -c "pass"
---

# Step 01: alpha fixture

Exists only so `implement_features_v2` builds a pipeline at import time. The step
name carries a `v2-fixture-` prefix so no real `refactor:` commit in this repo
ever causes it to be resume-skipped. Nothing here is ever executed.
