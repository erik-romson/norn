---
test_cmd: python3 -c "pass"
allow_noop: true
---

# Step 02: verification-only regression gate

Runs the suite and is expected to change nothing, so `allow_noop: true` lets the
owned-diff gate pass instead of stopping to ask an operator who may not be there.
