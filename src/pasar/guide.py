"""Loads the packaged agent guide (agents.md), shared by `pasar guide` and the HTTP API."""

import importlib.resources


def load_guide() -> str:
    return importlib.resources.files("pasar").joinpath("agents.md").read_text()
