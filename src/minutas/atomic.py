"""Files replaced whole: whoever reads one, even after a power cut, finds the old or the new."""

import json
import os
import tempfile
from pathlib import Path


def write_text(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as tmp:
        tmp.write(text)
        tmp.flush()
        # A rename that reaches the disk before the data leaves an empty file after a power cut.
        os.fsync(tmp.fileno())
    Path(tmp.name).replace(path)


def write_json(path: Path, value: object) -> None:
    write_text(path, json.dumps(value, indent=2) + "\n")
