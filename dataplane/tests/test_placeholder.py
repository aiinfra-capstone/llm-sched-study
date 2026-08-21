"""Keeps the CI job honest until the real tests land. Delete once they have."""

from __future__ import annotations

import dataplane


def test_package_imports() -> None:
    assert dataplane.__doc__
