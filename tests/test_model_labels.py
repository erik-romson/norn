"""Tests for norn/model_labels.py and norn/model_labels.toml."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from norn.model_labels import (
    ModelLabelError,
    ResolvedModel,
    known_labels,
    labels_for,
    labels_path,
    load_labels,
    resolve_label,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_toml(tmp_path: Path, content: str) -> Path:
    """Write a TOML file under tmp_path and return its path."""
    p = tmp_path / "labels.toml"
    p.write_text(textwrap.dedent(content))
    return p


# ---------------------------------------------------------------------------
# Bundled file
# ---------------------------------------------------------------------------


def test_bundled_file_loads():
    """The installed norn/model_labels.toml loads and validates without error."""
    labels = load_labels()
    assert isinstance(labels, dict)
    assert "claude-code" in labels
    assert "codex" in labels
    assert "opencode" in labels


def test_bundled_file_has_intent_labels():
    labels = load_labels()
    for label in ("light", "standard", "deep"):
        assert label in labels["claude-code"], f"claude-code.{label} missing"
    assert "review" in labels["codex"]
    for label in ("light", "standard", "deep"):
        assert label in labels["opencode"], f"opencode.{label} missing"


def test_bundled_file_claude_code_effort_set():
    labels = load_labels()
    for label_name, rm in labels["claude-code"].items():
        assert rm.effort is not None, (
            f"claude-code.{label_name} must have effort"
        )


def test_bundled_file_codex_effort_set():
    labels = load_labels()
    for label_name, rm in labels["codex"].items():
        assert rm.effort is not None, f"codex.{label_name} must have effort"


def test_bundled_file_opencode_no_effort():
    labels = load_labels()
    for label_name, rm in labels["opencode"].items():
        assert rm.effort is None, f"opencode.{label_name} must not have effort"


# ---------------------------------------------------------------------------
# labels_path()
# ---------------------------------------------------------------------------


def test_labels_path_exists():
    p = labels_path()
    assert isinstance(p, Path)
    assert p.exists(), f"labels_path() {p} does not exist on disk"
    assert p.suffix == ".toml"


def test_labels_path_is_model_labels_toml():
    assert labels_path().name == "model_labels.toml"


# ---------------------------------------------------------------------------
# resolve_label — happy path
# ---------------------------------------------------------------------------


def test_resolve_label_claude_code_standard():
    rm = resolve_label("claude-code", "standard")
    assert isinstance(rm, ResolvedModel)
    assert rm.provider == "claude-code"
    assert rm.label == "standard"
    assert rm.model  # non-empty
    assert rm.effort in ("low", "medium", "high", "xhigh", "max")


def test_resolve_label_claude_code_light():
    rm = resolve_label("claude-code", "light")
    assert rm.effort == "low"


def test_resolve_label_claude_code_deep():
    rm = resolve_label("claude-code", "deep")
    assert rm.effort in ("high", "xhigh", "max")


def test_resolve_label_codex_review():
    rm = resolve_label("codex", "review")
    assert isinstance(rm, ResolvedModel)
    assert rm.provider == "codex"
    assert rm.effort is not None


def test_resolve_label_opencode_light():
    rm = resolve_label("opencode", "light")
    assert isinstance(rm, ResolvedModel)
    assert rm.provider == "opencode"
    assert rm.effort is None


def test_resolve_label_opencode_standard():
    rm = resolve_label("opencode", "standard")
    assert rm.effort is None


def test_resolve_label_opencode_deep():
    rm = resolve_label("opencode", "deep")
    assert rm.effort is None


# ---------------------------------------------------------------------------
# resolve_label — error paths
# ---------------------------------------------------------------------------


def test_resolve_label_unknown_label_lists_valid():
    with pytest.raises(ModelLabelError, match="unknown label") as exc_info:
        resolve_label("claude-code", "does-not-exist")
    msg = str(exc_info.value)
    # Valid labels should be listed in the error message
    assert "light" in msg or "standard" in msg or "deep" in msg


def test_resolve_label_unknown_label_names_provider():
    with pytest.raises(ModelLabelError, match="claude-code"):
        resolve_label("claude-code", "does-not-exist")


def test_resolve_label_unknown_provider():
    with pytest.raises(ModelLabelError, match="unknown provider"):
        resolve_label("groq", "standard")


def test_resolve_label_unknown_provider_names_it():
    with pytest.raises(ModelLabelError, match="groq"):
        resolve_label("groq", "fast")


# ---------------------------------------------------------------------------
# known_labels()
# ---------------------------------------------------------------------------


def test_known_labels_is_frozenset():
    result = known_labels()
    assert isinstance(result, frozenset)


def test_known_labels_is_union_across_providers():
    labels = load_labels()
    expected: set[str] = set()
    for provider_labels in labels.values():
        expected.update(provider_labels.keys())
    assert known_labels() == frozenset(expected)


def test_known_labels_contains_intent_labels():
    kl = known_labels()
    for label in ("light", "standard", "deep", "review"):
        assert label in kl, f"{label!r} missing from known_labels()"


# ---------------------------------------------------------------------------
# labels_for()
# ---------------------------------------------------------------------------


def test_labels_for_claude_code_sorted():
    result = labels_for("claude-code")
    assert result == sorted(result)
    assert "light" in result
    assert "standard" in result
    assert "deep" in result


def test_labels_for_codex():
    result = labels_for("codex")
    assert "review" in result


def test_labels_for_opencode():
    result = labels_for("opencode")
    assert "light" in result


def test_labels_for_unknown_provider_returns_empty():
    # No provider table means no labels; no error
    result = labels_for("nonexistent")
    assert result == []


# ---------------------------------------------------------------------------
# Schema validation — wrong version
# ---------------------------------------------------------------------------


def test_wrong_version_raises(tmp_path):
    p = write_toml(tmp_path, """\
        version = 2
        [claude-code.light]
        model = "some-model"
        effort = "low"
    """)
    with pytest.raises(ModelLabelError, match="version"):
        load_labels(path=p)


def test_missing_version_raises(tmp_path):
    p = write_toml(tmp_path, """\
        [claude-code.light]
        model = "some-model"
        effort = "low"
    """)
    with pytest.raises(ModelLabelError, match="version"):
        load_labels(path=p)


# ---------------------------------------------------------------------------
# Schema validation — unknown top-level table
# ---------------------------------------------------------------------------


def test_unknown_top_level_table_raises(tmp_path):
    p = write_toml(tmp_path, """\
        version = 1
        [groq.fast]
        model = "some-model"
        effort = "low"
    """)
    with pytest.raises(ModelLabelError, match="groq"):
        load_labels(path=p)


def test_unknown_top_level_table_message_names_it(tmp_path):
    p = write_toml(tmp_path, """\
        version = 1
        [mistral.light]
        model = "some-model"
        effort = "low"
    """)
    with pytest.raises(ModelLabelError) as exc_info:
        load_labels(path=p)
    assert "mistral" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Schema validation — unknown key in label table
# ---------------------------------------------------------------------------


def test_unknown_key_in_label_table_raises(tmp_path):
    p = write_toml(tmp_path, """\
        version = 1
        [claude-code.light]
        model = "some-model"
        effort = "low"
        efort = "low"
    """)
    with pytest.raises(ModelLabelError, match=r"\[claude-code\.light\]"):
        load_labels(path=p)


def test_unknown_key_message_names_the_key(tmp_path):
    p = write_toml(tmp_path, """\
        version = 1
        [claude-code.light]
        model = "some-model"
        effort = "low"
        typo_key = "oops"
    """)
    with pytest.raises(ModelLabelError, match="typo_key"):
        load_labels(path=p)


# ---------------------------------------------------------------------------
# Schema validation — empty model
# ---------------------------------------------------------------------------


def test_empty_model_raises(tmp_path):
    p = write_toml(tmp_path, """\
        version = 1
        [claude-code.light]
        model = ""
        effort = "low"
    """)
    with pytest.raises(ModelLabelError, match=r"\[claude-code\.light\]"):
        load_labels(path=p)


def test_missing_model_raises(tmp_path):
    p = write_toml(tmp_path, """\
        version = 1
        [claude-code.light]
        effort = "low"
    """)
    with pytest.raises(ModelLabelError, match=r"\[claude-code\.light\]"):
        load_labels(path=p)


# ---------------------------------------------------------------------------
# Schema validation — bad effort for claude-code
# ---------------------------------------------------------------------------


def test_bad_effort_for_claude_code_raises(tmp_path):
    p = write_toml(tmp_path, """\
        version = 1
        [claude-code.light]
        model = "some-model"
        effort = "bogus-value"
    """)
    with pytest.raises(ModelLabelError, match=r"\[claude-code\.light\]"):
        load_labels(path=p)


def test_bad_effort_message_names_effort_value(tmp_path):
    p = write_toml(tmp_path, """\
        version = 1
        [claude-code.light]
        model = "some-model"
        effort = "superslow"
    """)
    with pytest.raises(ModelLabelError, match="superslow"):
        load_labels(path=p)


def test_missing_effort_for_claude_code_raises(tmp_path):
    p = write_toml(tmp_path, """\
        version = 1
        [claude-code.light]
        model = "some-model"
    """)
    with pytest.raises(ModelLabelError, match=r"\[claude-code\.light\]"):
        load_labels(path=p)


def test_missing_effort_for_codex_raises(tmp_path):
    p = write_toml(tmp_path, """\
        version = 1
        [codex.review]
        model = "gpt-5"
    """)
    with pytest.raises(ModelLabelError, match=r"\[codex\.review\]"):
        load_labels(path=p)


# ---------------------------------------------------------------------------
# Schema validation — effort under opencode (forbidden)
# ---------------------------------------------------------------------------


def test_effort_forbidden_for_opencode_raises(tmp_path):
    p = write_toml(tmp_path, """\
        version = 1
        [opencode.light]
        model = "github-copilot/some-model"
        effort = "low"
    """)
    with pytest.raises(ModelLabelError, match=r"\[opencode\.light\]"):
        load_labels(path=p)


def test_effort_forbidden_message_mentions_opencode(tmp_path):
    p = write_toml(tmp_path, """\
        version = 1
        [opencode.standard]
        model = "github-copilot/some-model"
        effort = "medium"
    """)
    with pytest.raises(ModelLabelError, match="opencode"):
        load_labels(path=p)


# ---------------------------------------------------------------------------
# Schema validation — bad label name
# ---------------------------------------------------------------------------


def test_bad_label_name_uppercase_raises(tmp_path):
    p = write_toml(tmp_path, """\
        version = 1
        [claude-code.Light]
        model = "some-model"
        effort = "low"
    """)
    with pytest.raises(ModelLabelError, match=r"Light"):
        load_labels(path=p)


def test_bad_label_name_starts_with_digit_raises(tmp_path):
    # TOML requires quoted keys for names starting with digits;
    # write it directly to bypass Python string restrictions.
    p = tmp_path / "labels2.toml"
    # Use a name that starts with a digit — valid TOML bare key but fails our regex.
    content = 'version = 1\n["claude-code"."1fast"]\nmodel = "x"\neffort = "low"\n'
    p.write_text(content)
    with pytest.raises(ModelLabelError, match="1fast"):
        load_labels(path=p)


# ---------------------------------------------------------------------------
# ResolvedModel is a frozen dataclass
# ---------------------------------------------------------------------------


def test_resolved_model_is_frozen():
    rm = ResolvedModel(
        provider="claude-code",
        label="standard",
        model="some-model",
        effort="medium",
        since="2026-09-07",
    )
    with pytest.raises((AttributeError, TypeError)):
        rm.effort = "low"  # type: ignore[misc]


def test_resolved_model_fields():
    rm = resolve_label("claude-code", "standard")
    assert hasattr(rm, "provider")
    assert hasattr(rm, "label")
    assert hasattr(rm, "model")
    assert hasattr(rm, "effort")
    assert hasattr(rm, "since")


# ---------------------------------------------------------------------------
# Guard: no raw model IDs outside norn/model_labels.toml
# ---------------------------------------------------------------------------


def test_no_raw_model_ids_in_source():
    """Raw model IDs and stale family-name defaults must only live in norn/model_labels.toml.

    Searches norn/**/*.py, bin/, and examples/ for:
    - Raw model id patterns: claude-[a-z]+-[0-9], gpt-5, github-copilot/, anthropic/claude
    - Stale family-name defaults used as model values: "haiku", "sonnet", "opus"

    Permitted exceptions (excluded from the check):
    - norn/model_labels.py  — the loader; may reference model id concepts in docstrings
    - norn/model_labels.toml — the source of truth
    - tests/test_model_labels.py — this file; patterns appear in test code
    - tests/test_metrics.py — recorded journal fixtures, not configuration
    - tests/test_transcripts.py — recorded journal fixtures, not configuration
    """
    import re
    from pathlib import Path

    root = Path(__file__).parent.parent

    excluded = {
        root / "norn" / "model_labels.py",
        root / "norn" / "model_labels.toml",
        root / "tests" / "test_model_labels.py",
        root / "tests" / "test_metrics.py",
        root / "tests" / "test_transcripts.py",
    }

    # Raw provider model-id patterns
    id_patterns = [
        re.compile(r"claude-[a-z]+-[0-9]"),
        re.compile(r"gpt-5"),
        re.compile(r"github-copilot/"),
        re.compile(r"anthropic/claude"),
    ]

    # Stale family-name defaults that must not be used as model values outside the TOML
    family_patterns = [
        re.compile(r'"haiku"'),
        re.compile(r'"sonnet"'),
        re.compile(r'"opus"'),
    ]

    all_patterns = id_patterns + family_patterns

    # Search: norn/**/*.py, bin/*.py, examples/*.py
    search_roots = [
        root / "norn",
        root / "bin",
        root / "examples",
    ]

    violations: list[str] = []
    for search_root in search_roots:
        if not search_root.exists():
            continue
        for py_file in sorted(search_root.rglob("*.py")):
            if py_file in excluded:
                continue
            text = py_file.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                for pat in all_patterns:
                    if pat.search(line):
                        rel = py_file.relative_to(root)
                        violations.append(f"{rel}:{lineno}: {line.rstrip()}")
                        break  # one violation per line

    assert not violations, (
        "Raw model IDs or stale family-name defaults found outside "
        "norn/model_labels.toml:\n"
        + "\n".join(violations)
    )
