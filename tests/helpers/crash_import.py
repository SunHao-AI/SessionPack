"""在指定行数处强制退出，用于验证断点续传。"""

import sys
from pathlib import Path

from sessionpack.adapters import TraeMemoryAdapter
from sessionpack.importer import import_source


def main() -> None:
    root, db, crash_after, batch = sys.argv[1:5]
    import_source(
        TraeMemoryAdapter(),
        Path(root),
        Path(db),
        batch=int(batch),
        crash_after_rows=int(crash_after),
    )


if __name__ == "__main__":
    main()
