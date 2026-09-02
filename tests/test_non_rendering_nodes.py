"""Decide which canonical nodes paint nothing, and prove the edges.

Figma reports `absoluteRenderBounds: null` for two kinds of node that still
carry a fill and a box: a leaf mask, and the operands of a boolean operation.
Contracting either as a rendered element demands a phantom rectangle. The risk
runs the other way too -- a mask that has children shows its subtree through
the mask, so the same rule applied without a guard would delete visible copy
from the contract. These tests pin both directions.
"""

from __future__ import annotations

import unittest
from typing import Any

from figma_lossless.compiler import (
    non_rendering_reason,
    walk_canonical_nodes,
)


def _node(node_id: str, kind: str, **extra: Any) -> dict[str, Any]:
    return {"id": node_id, "type": kind, "name": node_id, **extra}


def _walk(root: dict[str, Any]) -> dict[str, tuple[bool, bool, str | None]]:
    return {
        node["id"]: (hidden, effective_hidden, reason)
        for node, _parent, hidden, effective_hidden, reason in (
            walk_canonical_nodes(root)
        )
    }


class NonRenderingReasonTest(unittest.TestCase):
    def test_leaf_mask_paints_nothing(self) -> None:
        node = _node("1:1", "RECTANGLE", isMask=True)

        self.assertEqual(non_rendering_reason(node, False), "mask")

    def test_mask_with_children_still_renders(self) -> None:
        """Gradient-filled text: the subtree shows *through* the mask."""

        node = _node(
            "1:1",
            "TEXT",
            isMask=True,
            characters="혜택 안내",
            children=[_node("1:2", "RECTANGLE")],
        )

        self.assertIsNone(non_rendering_reason(node, False))

    def test_boolean_operand_paints_nothing(self) -> None:
        node = _node("1:1", "ELLIPSE")

        self.assertEqual(non_rendering_reason(node, True), "boolean-operand")

    def test_designer_hidden_is_named_separately(self) -> None:
        node = _node("1:1", "FRAME", visible=False)

        self.assertEqual(non_rendering_reason(node, False), "designer-hidden")

    def test_plain_node_renders(self) -> None:
        self.assertIsNone(non_rendering_reason(_node("1:1", "FRAME"), False))


class WalkCanonicalNodesTest(unittest.TestCase):
    def test_boolean_result_renders_but_its_operands_do_not(self) -> None:
        root = _node(
            "0:1",
            "FRAME",
            children=[
                _node(
                    "1:1",
                    "BOOLEAN_OPERATION",
                    booleanOperation="SUBTRACT",
                    children=[
                        _node("1:2", "RECTANGLE"),
                        _node("1:3", "ELLIPSE"),
                    ],
                )
            ],
        )

        seen = _walk(root)

        self.assertEqual(seen["1:1"], (False, False, None))
        self.assertEqual(seen["1:2"], (True, True, "boolean-operand"))
        self.assertEqual(seen["1:3"], (True, True, "boolean-operand"))

    def test_operand_rule_does_not_leak_past_one_level(self) -> None:
        """A frame nested under the operation is not itself an operand's child."""

        root = _node(
            "0:1",
            "FRAME",
            children=[
                _node(
                    "1:1",
                    "BOOLEAN_OPERATION",
                    children=[
                        _node("1:2", "VECTOR", children=[_node("1:3", "VECTOR")])
                    ],
                )
            ],
        )

        seen = _walk(root)

        # 1:3 inherits effectiveHidden from its hidden parent, but it is not
        # itself classified as an operand -- only direct children are.
        self.assertEqual(seen["1:2"], (True, True, "boolean-operand"))
        self.assertEqual(seen["1:3"], (False, True, None))

    def test_masked_subtree_keeps_its_text(self) -> None:
        """The guard that stops a mask from swallowing visible copy."""

        root = _node(
            "0:1",
            "FRAME",
            children=[
                _node(
                    "1:1",
                    "GROUP",
                    isMask=True,
                    children=[_node("1:2", "TEXT", characters="혜택 안내")],
                )
            ],
        )

        seen = _walk(root)

        self.assertEqual(seen["1:1"], (False, False, None))
        self.assertEqual(seen["1:2"], (False, False, None))

    def test_leaf_mask_hides_only_itself(self) -> None:
        root = _node(
            "0:1",
            "FRAME",
            children=[
                _node("1:1", "RECTANGLE", isMask=True),
                _node("1:2", "TEXT", characters="Language"),
            ],
        )

        seen = _walk(root)

        self.assertEqual(seen["1:1"], (True, True, "mask"))
        self.assertEqual(seen["1:2"], (False, False, None))


if __name__ == "__main__":
    unittest.main()
