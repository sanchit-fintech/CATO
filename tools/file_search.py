from pathlib import Path


def search_files(query: str, root: str = "~") -> list[str]:
    """
    Search for files and folders whose names contain the query.
    """

    root_path = Path(root).expanduser()
    query = query.lower().strip()

    if not query:
        return []

    matches = []

    try:
        for path in root_path.rglob("*"):
            if query in path.name.lower():
                matches.append(str(path))

    except PermissionError:
        pass

    return matches[:50]

