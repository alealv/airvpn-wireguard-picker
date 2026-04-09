"""Smoke tests to verify the package and tooling are wired up."""

from __future__ import annotations

import airvpn_picker


def test_version_is_set() -> None:
    assert airvpn_picker.__version__
    assert isinstance(airvpn_picker.__version__, str)
