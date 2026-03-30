from __future__ import annotations

from norn.contrib.utils.slugify import slugify


def test_slugify_basic():
    assert slugify("Fix Null Pointer Exception") == "fix-null-pointer-exception"


def test_slugify_special_chars():
    assert slugify("Bug: NPE in auth/login") == "bug-npe-in-auth-login"


def test_slugify_max_length():
    long_text = "a" * 100
    result = slugify(long_text, max_length=50)
    assert len(result) == 50


def test_slugify_strips_trailing_hyphens():
    assert slugify("hello---") == "hello"


def test_slugify_empty():
    assert slugify("") == ""
