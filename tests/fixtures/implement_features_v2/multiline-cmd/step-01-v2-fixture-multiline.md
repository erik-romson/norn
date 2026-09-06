---
test_cmd: |
  sh -ex -c '
    echo "=== render ==="
    helm template test charts/ | python3 -c "
    import sys, yaml
    docs = [d for d in yaml.safe_load_all(sys.stdin) if d]
    assert len(docs) > 0, \"no documents\"
    "
    touch $(echo /tmp/norn-injection-canary)
  '
---

# Step 01: multi-line fixture

Nothing here is ever executed. The step exists so the generated commit shell can
be parsed and run against a temp repo.
