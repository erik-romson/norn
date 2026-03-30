from __future__ import annotations

from norn.contrib.matchers.component_matcher import ComponentMatcher
from norn.contrib.matchers.keyword_matcher import KeywordMatcher
from norn.contrib.matchers.label_matcher import LabelMatcher
from norn.contrib.matchers.llm_matcher import LLMMatcher
from norn.contrib.matchers.stacktrace_matcher import StacktraceMatcher


def component(mapping: dict[str, str]) -> ComponentMatcher:
    return ComponentMatcher(mapping)


def label(mapping: dict[str, str]) -> LabelMatcher:
    return LabelMatcher(mapping)


def stacktrace(github_org: str | None = None) -> StacktraceMatcher:
    return StacktraceMatcher(github_org)


def keyword() -> KeywordMatcher:
    return KeywordMatcher()


def llm(model: str = "claude-sonnet-4-6") -> LLMMatcher:
    return LLMMatcher(model)
