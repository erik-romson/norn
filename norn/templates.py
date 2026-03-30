from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class PromptTemplate:
    """Reusable prompt + system_prompt + output_format combination.

    Place template files in a ``templates/`` directory and load them by name
    via :func:`load_template`. Use in ``Generate`` stages with
    ``template="name"``.

    When ``output_format`` is set, the SDK validates Claude's response against
    the JSON schema and returns parsed data via ``ResultMessage.structured_output``.

    Attributes:
        name: Unique template name used for lookup.
        template: Prompt string with ``{input}`` placeholder for the stage input.
        system_prompt: Optional instructions prepended to the agent's system prompt.
        output_format: Optional JSON Schema dict for structured output validation.

    Example::

        code_review = PromptTemplate(
            name="code_review",
            template="Review this code:\\n{input}",
            system_prompt="You are a senior code reviewer.",
            output_format={"type": "object", "required": ["issues", "score"]},
        )
    """

    name: str
    template: str
    system_prompt: str | None = None
    output_format: dict | None = None


def load_template(name: str) -> PromptTemplate:
    """Load a :class:`PromptTemplate` by name from ``templates/{name}.py``.

    Searches relative to the current working directory.

    Raises:
        FileNotFoundError: if ``templates/{name}.py`` does not exist.
        ValueError: if no :class:`PromptTemplate` with the given name is found
            in the file.
    """
    template_path = Path("templates") / f"{name}.py"
    if not template_path.exists():
        raise FileNotFoundError(f"Template file not found: {template_path}")

    spec = importlib.util.spec_from_file_location(f"_template_{name}", template_path.resolve())
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for attr_name in dir(module):
        obj = getattr(module, attr_name)
        if isinstance(obj, PromptTemplate) and obj.name == name:
            log.debug("[templates] Loaded template '%s' from %s", name, template_path)
            return obj

    raise ValueError(f"No PromptTemplate with name '{name}' found in {template_path}")
