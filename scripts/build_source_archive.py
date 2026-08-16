from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build_work",
    "release",
    "release_work",
    "vendor_cache",
}
EXCLUDED_ROOTS = {"qa"}
EXCLUDED_SUFFIXES = {".exe", ".pyc", ".zip"}


def include(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if relative.parts and relative.parts[0] in EXCLUDED_ROOTS:
        return False
    if any(part in EXCLUDED_PARTS for part in relative.parts):
        return False
    if path.suffix.casefold() in EXCLUDED_SUFFIXES:
        return False
    return path.is_file()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    options = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = options.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing archive: {output}")
    prefix = "Audio_and_Subtitle_Batch_Copy_v1_2_4_Source"
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(root.rglob("*")):
            if include(path, root):
                archive.write(path, Path(prefix) / path.relative_to(root))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
