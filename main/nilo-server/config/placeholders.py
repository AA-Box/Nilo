"""Detect "fill me in" values in the shipped configuration.

``config.yaml`` ships with placeholder values such as ``<your-api-key>``. Code
that would otherwise try to use them calls :func:`is_placeholder`. The legacy
marker (the Chinese character 你, "you", from the inherited config) is still
recognised so existing ``data/.config.yaml`` files keep working.
"""

PLACEHOLDER_MARKERS = ("<your", "你")


def is_placeholder(value) -> bool:
    """True when *value* is a string still holding a template placeholder."""
    return isinstance(value, str) and any(marker in value for marker in PLACEHOLDER_MARKERS)
