"""The README's code must at least parse, and every name it imports must exist."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import List

import pytest

import pingroom

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
BLOCKS: List[str] = re.findall(r"```python\n(.*?)```", README, re.S)


def test_the_readme_has_examples() -> None:
    assert len(BLOCKS) >= 15


@pytest.mark.parametrize("index", range(len(BLOCKS)))
def test_each_example_parses_and_its_pingroom_imports_exist(index: int) -> None:
    source = BLOCKS[index]
    # `...` placeholders and top-level `await`-free snippets all parse as written.
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "pingroom":
            for alias in node.names:
                assert hasattr(pingroom, alias.name), f"README imports pingroom.{alias.name}"


NAMESPACES = {
    "notifications": "Notifications",
    "approvals": "Approvals",
    "questions": "Questions",
    "handoffs": "Handoffs",
    "metrics": "Metrics",
    "attachments": "Attachments",
    "live": "Live",
}


def test_every_method_the_readme_calls_on_a_client_exists() -> None:
    from pingroom import client as client_module

    nested = re.findall(r"client\.(\w+)\.(\w+)\(", README)
    assert len(nested) > 15
    for namespace, method in nested:
        assert hasattr(getattr(client_module, NAMESPACES[namespace]), method), (namespace, method)
    for method in re.findall(r"client\.(\w+)\(", README):
        assert hasattr(pingroom.PingRoom, method), method
