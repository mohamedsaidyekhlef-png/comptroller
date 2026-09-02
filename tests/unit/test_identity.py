import pathlib
import re

import pytest

from comptroller.core.identity import IdentityError, Principal

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"


def test_scope_path_is_canonical() -> None:
    p = Principal("acme", "researcher", "run1")
    assert p.scope_path == "tenant/acme/agent/researcher/run/run1"


def test_ancestors_are_root_first_prefixes() -> None:
    p = Principal("acme", "researcher", "run1", "t9")
    assert p.ancestors() == (
        "tenant/acme",
        "tenant/acme/agent/researcher",
        "tenant/acme/agent/researcher/run/run1",
        "tenant/acme/agent/researcher/run/run1/task/t9",
    )


def test_ancestors_are_sorted_so_acquisition_order_is_deterministic() -> None:
    p = Principal("acme", "researcher", "run1", "t9")
    assert list(p.ancestors()) == sorted(p.ancestors())


def test_path_traversal_and_injection_rejected() -> None:
    for evil in ["../admin", "a/b", "tenant/x", "", "a" * 65, "acme\n", " acme", "acme\r", "acme\t", "acme\x00", "\nacme"]:
        with pytest.raises(IdentityError):
            Principal(evil, "agent", "run")


def test_credential_requires_string_claims() -> None:
    with pytest.raises(IdentityError):
        Principal.from_credential({"tenant": None, "agent": "a"}, run_id="r")


def test_credential_happy_path() -> None:
    p = Principal.from_credential({"tenant": "acme", "agent": "buyer"}, run_id="r7")
    assert p.scope_path == "tenant/acme/agent/buyer/run/r7"


def test_principal_never_constructed_outside_from_credential() -> None:
    """SECURITY: enforces the identity boundary as a test, not a convention.

    Any new direct `Principal(` call in src/ fails CI. A future legitimate
    call site must be added to `allowed` with a written justification.
    """
    allowed = {"core/identity.py"}
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC / "comptroller").as_posix()
        if rel in allowed:
            continue
        if re.search(r"\bPrincipal\s*\(", path.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert not offenders, f"direct Principal() outside from_credential: {offenders}"
