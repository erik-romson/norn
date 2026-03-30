from unittest.mock import patch

import pytest

from norn.cli import _expand_file_refs, main


def test_expand_single_file_ref(tmp_path):
    f = tmp_path / "spec.txt"
    f.write_text("hello world")
    result = _expand_file_refs(f"Based on @{f}")
    assert result == "Based on hello world"


def test_expand_multiple_file_refs(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("AAA")
    b.write_text("BBB")
    result = _expand_file_refs(f"@{a} and @{b}")
    assert result == "AAA and BBB"


def test_no_file_refs_unchanged():
    result = _expand_file_refs("just a normal string with an email@address")
    assert result == "just a normal string with an email@address"


def test_expand_preserves_surrounding_text(tmp_path):
    f = tmp_path / "data.md"
    f.write_text("contents")
    result = _expand_file_refs(f"before @{f} after")
    assert result == "before contents after"


def test_expand_missing_file_exits(tmp_path):
    with pytest.raises(SystemExit):
        _expand_file_refs("@nonexistent/file.txt")


def test_expand_relative_path():
    result = _expand_file_refs("See @examples/spec.txt for details")
    assert "Greeter" in result
    assert "@examples/spec.txt" not in result


# --- list subcommand ---


def test_list_shows_bundled_pipelines(capsys):
    with patch("sys.argv", ["norn", "list"]):
        main()
    captured = capsys.readouterr()
    assert "hello" in captured.out
    assert "vanilla_change" in captured.out
    assert "implement_features" in captured.out


# --- describe subcommand ---


def test_describe_shows_pipeline_details(capsys):
    with patch("sys.argv", ["norn", "describe", "hello"]):
        main()
    captured = capsys.readouterr()
    assert "hello" in captured.out


def test_describe_unknown_pipeline_exits():
    with patch("sys.argv", ["norn", "describe", "nonexistent_pipeline"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


# --- run resolves bundled pipeline names ---


def test_run_resolves_bundled_pipeline_name():
    """Verify that `norn run hello --dry-run` resolves the bundled pipeline."""
    with patch("sys.argv", ["norn", "run", "hello", "--dry-run"]):
        # dry-run prints structure and returns without executing
        main()


def test_run_unknown_file_exits():
    with patch("sys.argv", ["norn", "run", "no_such_file.py"]):
        with pytest.raises(SystemExit):
            main()
