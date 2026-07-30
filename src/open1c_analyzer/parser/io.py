"""Source input helpers."""

from pathlib import Path


def read_text(path: Path) -> str:
    """Read common encodings used by Designer and EDT exports."""
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeError(f"Unsupported source encoding: {path}")
