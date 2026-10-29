"""Path matching shared by config validation and request routing."""


def normalize_path(path: str) -> str:
    """Drop a trailing slash so `/api/v1/generate/` matches `/api/v1/generate`."""
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path


def path_matches(pattern: str, path: str) -> bool:
    """Match a config path.

    A pattern ending in ``*`` is a prefix match (``/v1/*``).
    Every other pattern is an exact match after trailing-slash normalization.
    """
    path = normalize_path(path)
    if pattern.endswith("*"):
        prefix = pattern[:-1]
        if prefix.endswith("/"):
            return path == prefix[:-1] or path.startswith(prefix)
        return path.startswith(prefix)
    return path == normalize_path(pattern)
