"""pysparql-generate's own conformance cases (tests/cases), compared by isomorphism."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from conformance import run_case  # noqa: E402

CASES_DIR = os.path.join(os.path.dirname(__file__), "cases")
CASES = sorted(n for n in os.listdir(CASES_DIR) if os.path.isdir(os.path.join(CASES_DIR, n)))


@pytest.mark.parametrize("name", CASES)
def test_case(name):
    ok, ngot, nexp, dt, detail = run_case(os.path.join(CASES_DIR, name), allow_network=False, verbose=True)
    assert ok, f"{name}: got {ngot} triples, expected {nexp}{detail}"
