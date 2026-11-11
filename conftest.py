"""Pytest no recoge el código archivado del bot de streamers."""

from __future__ import annotations


def pytest_ignore_collect(collection_path, config):
    return "_archive" in getattr(collection_path, "parts", ())
