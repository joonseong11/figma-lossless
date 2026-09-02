"""The parts of a canonical bundle a fixture cannot leave out.

The compiler writes `property-accounting.json` for every REST-evidenced
bundle -- that artifact is what proves it dropped none of the canonical
evidence it was given. So a fixture that claims `evidenceSource: figma-rest`
without one is not a smaller version of a real bundle; it is a real bundle
with its accounting gate deleted, and the validator now says so instead of
recording `NOT_EVALUATED` and passing.

Suites that assert about some *other* gate need the artifact for the same
reason a real run has it, not as an opt-out: `clean_accounting()` gives them
one that accounts for everything and therefore raises nothing of its own.
A suite that is genuinely about the artifact being absent omits it.
"""

from __future__ import annotations

from typing import Any


# Tells "this fixture did not care about accounting" (gets what a real
# canonical bundle ships) apart from "this fixture is about the artifact being
# absent" (`accounting=None`). `None` alone cannot carry both meanings.
DEFAULT_ACCOUNTING = object()


def clean_accounting(
    feature_id: str, *, screens: int = 1, nodes: int = 1, **overrides: Any
) -> dict[str, Any]:
    """An accounting artifact that accounts for everything it was given."""

    value: dict[str, Any] = {
        "schemaVersion": "1.0",
        "featureId": feature_id,
        "counts": {"screens": screens, "nodes": nodes},
        "compiled": [],
        "preservedOpaque": [],
        "unsupported": [],
        "violations": [],
        "unresolvedTokenBindings": [],
        "nonRenderingNodes": [],
        "unverifiedImageFills": [],
        "restCollection": {"complete": True},
    }
    value.update(overrides)
    return value


def attach_accounting(
    manifest: dict[str, Any], accounting: dict[str, Any]
) -> dict[str, Any]:
    """Point a manifest at an accounting artifact the way the compiler does."""

    manifest["propertyAccounting"] = {
        "path": "property-accounting.json",
        "counts": accounting.get("counts", {}),
        "violations": accounting.get("violations", []),
    }
    return manifest
