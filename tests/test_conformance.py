"""The Java test cases, run through the API and compared by graph isomorphism.
Skipped when the Java repository is not checked out next to this one."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from conformance import DEFAULT_TESTS, run_case  # noqa: E402

NETWORK = {"capital"}  # fetches http://country.io/capital.json
CASES = sorted(n for n in os.listdir(DEFAULT_TESTS) if os.path.isdir(os.path.join(DEFAULT_TESTS, n))) \
    if os.path.isdir(DEFAULT_TESTS) else []


@pytest.mark.skipif(not CASES, reason="Java test cases not available")
@pytest.mark.parametrize("name", [c for c in CASES if c not in NETWORK])
def test_java_case(name):
    ok, ngot, nexp, dt, detail = run_case(os.path.join(DEFAULT_TESTS, name), allow_network=False, verbose=True)
    assert ok, f"{name}: got {ngot} triples, expected {nexp}{detail}"
