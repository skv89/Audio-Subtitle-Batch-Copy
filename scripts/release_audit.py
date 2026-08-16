from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def command_result(
    command: list[str], *, timeout: int, env: dict[str, str] | None = None
) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    return {
        "command": subprocess.list2cmdline(command),
        "exit_code": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }


def png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()[:24]
    if len(data) != 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Not a valid PNG header: {path}")
    return struct.unpack(">II", data[16:24])


def audit(release: Path, source_archive: Path, expected_version: str) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: Any) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    required = [
        "Audio and Subtitle Batch Copy.exe",
        "README.md",
        "LICENSE.txt",
        "THIRD_PARTY_NOTICES.txt",
        "assets/fonts/LiberationSans-Regular.ttf",
        "assets/fonts/LiberationSans-Bold.ttf",
        "assets/fonts/LIBERATION_FONTS_LICENSE.txt",
        "licenses/PYTHON_LICENSE.txt",
        "licenses/PYINSTALLER_LICENSE.txt",
        "licenses/QT_LGPL_3_0.txt",
        "tools/ffmpeg/bin/ffmpeg.exe",
        "tools/ffmpeg/bin/ffprobe.exe",
        "tools/ffmpeg/licenses/FFMPEG_GPLv3_LICENSE.txt",
        "tools/ffmpeg/FFMPEG_BUILD_README.txt",
    ]
    missing = [name for name in required if not (release / name).is_file()]
    check("required_files", not missing, {"missing": missing})

    files = sorted(path for path in release.rglob("*") if path.is_file())
    prohibited = [
        str(path.relative_to(release))
        for path in files
        if any(
            part in {".venv", "__pycache__", "vendor_cache", "release_work"} for part in path.parts
        )
        or path.suffix.casefold() == ".pyc"
    ]
    check("no_development_artifacts", not prohibited, {"found": prohibited})

    ffmpeg = release / "tools/ffmpeg/bin/ffmpeg.exe"
    ffprobe = release / "tools/ffmpeg/bin/ffprobe.exe"
    ffmpeg_result = command_result([str(ffmpeg), "-version"], timeout=20)
    ffprobe_result = command_result([str(ffprobe), "-version"], timeout=20)
    ffmpeg_first = ffmpeg_result["stdout"].splitlines()[0] if ffmpeg_result["stdout"] else ""
    ffprobe_first = ffprobe_result["stdout"].splitlines()[0] if ffprobe_result["stdout"] else ""
    check(
        "ffmpeg_exact_9_0",
        ffmpeg_result["exit_code"] == 0 and ffmpeg_first.startswith("ffmpeg version 9.0-"),
        ffmpeg_first,
    )
    check(
        "ffprobe_exact_9_0",
        ffprobe_result["exit_code"] == 0 and ffprobe_first.startswith("ffprobe version 9.0-"),
        ffprobe_first,
    )

    executable = release / "Audio and Subtitle Batch Copy.exe"
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    version_result = command_result([str(executable), "--version"], timeout=30, env=environment)
    check(
        "packaged_version",
        version_result["exit_code"] == 0
        and f"audio-subtitle-batch-copy {expected_version}" in version_result["stdout"],
        version_result,
    )
    smoke_result = command_result([str(executable), "--smoke-test"], timeout=45, env=environment)
    check("packaged_smoke_test", smoke_result["exit_code"] == 0, smoke_result)
    selection_result = command_result(
        [str(executable), "--selection-self-test"], timeout=45, env=environment
    )
    check(
        "packaged_selection_self_test",
        selection_result["exit_code"] == 0,
        selection_result,
    )
    compatibility_result = command_result(
        [str(executable), "--compatibility-self-test"], timeout=90, env=environment
    )
    check(
        "packaged_ffmpeg_compatibility_self_test",
        compatibility_result["exit_code"] == 0,
        compatibility_result,
    )
    with tempfile.TemporaryDirectory(prefix="abcopy-release-audit-") as temp_directory:
        screenshot = Path(temp_directory) / "packaged-window.png"
        screenshot_result = command_result(
            [str(executable), "--screenshot", str(screenshot)], timeout=45, env=environment
        )
        screenshot_detail: dict[str, Any] = {"process": screenshot_result}
        screenshot_ok = screenshot_result["exit_code"] == 0 and screenshot.is_file()
        if screenshot_ok:
            try:
                dimensions = png_dimensions(screenshot)
            except ValueError as exc:
                screenshot_ok = False
                screenshot_detail["error"] = str(exc)
            else:
                screenshot_detail.update(
                    {
                        "dimensions": dimensions,
                        "size": screenshot.stat().st_size,
                        "sha256": sha256(screenshot),
                    }
                )
                screenshot_ok = (
                    dimensions[0] >= 1120
                    and dimensions[1] >= 720
                    and screenshot.stat().st_size > 20_000
                )
        check("packaged_screenshot", screenshot_ok, screenshot_detail)

    source_ok = source_archive.is_file()
    source_details: dict[str, Any] = {}
    if source_ok:
        try:
            with zipfile.ZipFile(source_archive) as archive:
                bad_member = archive.testzip()
                names = set(archive.namelist())
            required_source_suffixes = {
                "pyproject.toml",
                "src/audio_subtitle_batch_copy/isobmff.py",
                "src/audio_subtitle_batch_copy/ui.py",
                "src/audio_subtitle_batch_copy/processor.py",
                "tests/test_ffmpeg_integration.py",
                "tests/test_isobmff.py",
                "README.md",
            }
            missing_source = [
                suffix
                for suffix in required_source_suffixes
                if not any(name.endswith(suffix) for name in names)
            ]
            prohibited_source = [
                name
                for name in names
                if "/.venv/" in name or "/vendor_cache/" in name or name.endswith((".pyc", ".exe"))
            ]
            source_ok = bad_member is None and not missing_source and not prohibited_source
            source_details = {
                "entry_count": len(names),
                "bad_member": bad_member,
                "missing": missing_source,
                "prohibited": prohibited_source[:20],
                "sha256": sha256(source_archive),
                "size": source_archive.stat().st_size,
            }
        except (OSError, zipfile.BadZipFile) as exc:
            source_ok = False
            source_details = {"error": str(exc)}
    check("source_archive", source_ok, source_details)

    manifest: list[dict[str, Any]] = []
    tree_hasher = hashlib.sha256()
    for path in files:
        relative = path.relative_to(release).as_posix()
        file_hash = sha256(path)
        size = path.stat().st_size
        manifest.append({"path": relative, "size": size, "sha256": file_hash})
        tree_hasher.update(f"{relative}\0{size}\0{file_hash}\n".encode())
    check("nonempty_release", bool(files), {"file_count": len(files)})
    return {
        "release": str(release),
        "source_archive": str(source_archive),
        "passed": all(item["passed"] for item in checks),
        "checks": checks,
        "file_count": len(files),
        "total_bytes": sum(path.stat().st_size for path in files),
        "tree_sha256": tree_hasher.hexdigest(),
        "manifest": manifest,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release", type=Path)
    parser.add_argument("source_archive", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--expected-version", default="1.2.4")
    options = parser.parse_args()
    report = audit(
        options.release.resolve(),
        options.source_archive.resolve(),
        options.expected_version,
    )
    options.report.parent.mkdir(parents=True, exist_ok=True)
    options.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {key: report[key] for key in ("passed", "file_count", "total_bytes", "tree_sha256")},
            indent=2,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
