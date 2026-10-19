"""Frozen originals stay byte-identical (AGENTS.md: originals stay unchanged)."""

from __future__ import annotations

import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FROZEN: dict[str, str] = {
    "norn/pipelines/hello_v1.py": "d8ae17d5748de988e15ad42d43345c05be692ca0f65f9cd46efc5d355c6b5063",
    "norn/pipelines/vanilla_change_v1.py": "7ab2ef415e9f1ab465611cec5ca9ac1b55cde78d210126e488ba48525ba15c55",
    "norn/pipelines/implement_features_v1.py": "ebcafa109d5f041738467eb5726b41c9e0d8a48e442b541f6828312f343940d0",
    "norn/pipelines/implement_features_v2.py": "1d6b1e58a0428afd0bdfbf24f23893a109cd493e61138a4385ac65ffb76dd060",
    "norn/pipelines/plan_with_review_v1.py": "6a549a7e5e418d3dc812cc710c7c9e45daf026d376f9be6cdb1ee87e1a2efa69",
    "norn/pipelines/fix_jira_issue_v1.py": "9543513851ce011bb0b1446182a1a99592acf5d75ace7dd015ae269431c7178b",
    "norn/pipelines/fix_jira_issue_v2.py": "5e7fa984ef304bb550a19c06c784ba23e2e4fe4a9353aa3daf7d37b9d1dbf340",
    "norn/pipelines/check_and_fix_ci_v1.py": "c7bf5981de14c4889218d8b9cbbda9daec695b2bdc49f2aa735d630edaba2ae5",
    "norn/pipelines/merge_branch_v1.py": "2fb4b5997bfd4e04d55cb577aef86601c8362ffa893be9956a7224af2a5d4253",
    "norn/pipelines/_plan_review_v1.py": "34d05b94e533e616a4e3fe3fe3d8dd2d6e09650c9917ac207167f645f7df0a28",
    "norn/pipelines/_plan_gates_v1.py": "b1fccc435330d08f89a8a6eea8c32d5cbc70e9f6a96d552d173f50591254a669",
    "tests/test_vanilla_change_v1.py": "305560b2ca58dac4c1bef8e548f7779e9b2e91eae186153477d45208a95158ed",
    "tests/test_implement_features_v2.py": "c292594de675749223ce3d18d07e5f8bfd5eb4d85288201e6c61122566cf23da",
    "tests/test_plan_with_review_pipeline_v1.py": "3ecc68e55df24ed5ae5efc7a4c52e67bd6c407569af82d1634d2a875b1ab7a46",
    "tests/test_plan_review_builder_v1.py": "d1440b64fe8398b8a5f5716897de23632e02a44ee1ea9d5d09d0433ad2fe0537",
    "tests/test_plan_gates_v1.py": "932465ad27ff99ce93b72e57a6d5ed49ea7745f2b8509461e2ebe63fe23807a1",
    "tests/test_fix_jira_issue_pipeline_v1.py": "709613ca7b4766d9ccd0f243b0b29bbd21b83cb9cbb1cc2952655414bd1ae661",
    "tests/test_fix_jira_issue_pipeline_v2.py": "2a3b97984ad67e5e04dbd176430c53f14214a51fd9d1d7b7442863ef0d9756f4",
    "tests/test_check_and_fix_ci_v1.py": "53258cc89e5251a3e1f994a155017d4010276e778a68e9351dbccfacc34f5fc0",
    "tests/test_merge_branch_v1.py": "9025ebc08e1de480a531766aae8f1f45438c2d0857c2316572e430edcbc33c1b",
}


def test_frozen_originals_unchanged() -> None:
    for rel, expected in FROZEN.items():
        actual = hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()
        assert actual == expected, (
            f"{rel} is frozen (AGENTS.md: originals stay unchanged). A fix to a "
            "run-killing bug in it is ported to its copy in the same change, and "
            "this hash is updated deliberately."
        )
