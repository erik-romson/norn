# Code review — CBS-1 (aaaaaaa..bbbbbbb)

STATUS: DECIDED
<!-- Set each Disposition to fix, skip or follow-up, then set the STATUS line to DECIDED. -->

## C1. Reset swaps the shared instance without a lock [major]
Where: modules/core/src/main/java/x/Registry.java:41
Problem: reset replaces the instance while readers may hold the old one.
Fix: take the same lock in reset and get.
Source: claude, codex
Disposition: fix

## C2. Retry count is read twice per request [major]
Where: modules/core/src/main/java/x/Client.java:88
Problem: the second read can see a different value after a reload.
Fix: read it once into a local.
Source: codex
Disposition: fix

## C3. Timeout parsing duplicates the config loader [minor]
Where: modules/core/src/main/java/x/Settings.java:12
Problem: two parsers can drift apart.
Source: claude
Disposition: follow-up

## C4. Test helper leaks a temp directory [minor]
Where: modules/core/src/test/java/x/TempDirs.java:20
Problem: the directory is never deleted.
Source: review
Disposition: skip (covered by the build cleanup)

## C5. Rename the loop variable [nit]
Where: modules/core/src/main/java/x/Client.java:93
Problem: i hides the field of the same name.
Source: claude
Disposition: skip (auto: nit)

## Dropped as false positives
- claude R3 Null check missing in get — the field is final and set in the constructor

## Omitted findings
- Sort the imports in Client [nit]
- Drop the unused constant in Settings [nit]
