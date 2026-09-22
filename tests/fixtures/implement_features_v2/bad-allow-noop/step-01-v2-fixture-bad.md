---
test_cmd: python3 -c "pass"
allow_noop: sometimes
---

# Step with a non-boolean allow_noop

`allow_noop` gates a safety check, so anything but a YAML boolean must be
rejected at import time rather than quietly read as truthy.
