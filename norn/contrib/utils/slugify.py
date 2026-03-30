from __future__ import annotations

import re


def slugify(text: str, max_length: int = 50) -> str:
    """Convert text to a URL/branch-friendly slug."""
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:max_length]
