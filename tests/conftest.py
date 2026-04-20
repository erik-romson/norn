from __future__ import annotations

import pytest

from norn.testing import reset_call_counter


@pytest.fixture(autouse=True)
def _reset_global_call_counter():
    reset_call_counter()
    yield
    reset_call_counter()
