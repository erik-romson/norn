---
final_test_cmd: python3 -c "pass"
---

# Fixture feature (v2, multi-line test_cmd)

A `test_cmd:` written as a YAML block scalar, the shape a real plan uses when
validation is more than one command. It carries the characters that break a
shell when interpolated as code rather than passed as data: newlines, both
quote styles, parentheses, and a command substitution.
