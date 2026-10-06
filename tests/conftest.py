from pathlib import Path

import pytest

from tdscope import load

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture
def all_dumps():
    """The small hand-made fixtures (the aemcs/ set has its own tests)."""
    return load([str(p) for p in sorted(FIXTURES.iterdir()) if p.is_file() and p.suffix != ".py"])


@pytest.fixture
def aem_dumps():
    return load([str(FIXTURES / f"aem-node1-{i}.dump") for i in (1, 2, 3)])
