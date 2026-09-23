"""Loads the packaged agent guide, shared by `pasar guide` and the HTTP API.

The main guide is agents.md. A topic is a longer guide on one subject that the main guide
points to, so the main guide stays short enough to read in one go: `pasar guide cloud` prints
agents-cloud.md.
"""

import importlib.resources

TOPICS = {"cloud": "agents-cloud.md"}


def load_guide(topic: str | None = None) -> str:
    if topic is None:
        name = "agents.md"
    elif topic in TOPICS:
        name = TOPICS[topic]
    else:
        raise ValueError(f"no guide topic {topic!r}; topics: {', '.join(TOPICS)}")
    return importlib.resources.files("pasar").joinpath(name).read_text()
