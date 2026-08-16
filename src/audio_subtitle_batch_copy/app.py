from __future__ import annotations

import argparse
import contextlib
import ctypes
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QComboBox

from .constants import APP_NAME, APP_VERSION
from .media import FFmpegToolchain, MediaProbeError, no_window_creation_flags, probe_media
from .models import DestinationEntry, MediaInfo, MediaStream, SourceEntry, TrackSelection
from .planner import (
    PreparedJob,
    apply_reliable_default_container,
    expected_default_indices,
    mapped_streams,
    plan_output,
)
from .processor import BatchProcessor
from .ui import MainWindow


def _set_windows_app_id() -> None:
    if os.name != "nt":
        return
    with contextlib.suppress(AttributeError, OSError):
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "AudioSubtitleBatchCopy.Desktop.1"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="audio-subtitle-batch-copy")
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Open the main window briefly, then exit with the Qt event loop exercised.",
    )
    parser.add_argument(
        "--screenshot",
        type=Path,
        help="Save a PNG of the rendered main window and exit (for release QA).",
    )
    parser.add_argument(
        "--selection-self-test",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--compatibility-self-test",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def _resource_roots() -> list[Path]:
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent / "assets" / "fonts")
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(str(meipass)) / "assets" / "fonts")
    roots.append(Path(__file__).resolve().parents[2] / "assets" / "fonts")
    return roots


def _install_bundled_fonts(application: QApplication) -> None:
    family = ""
    for root in _resource_roots():
        for name in ("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf"):
            font_id = QFontDatabase.addApplicationFont(str(root / name))
            if font_id >= 0 and not family:
                families = QFontDatabase.applicationFontFamilies(font_id)
                family = families[0] if families else ""
    if family:
        application.setFont(QFont(family, 9))


def _install_window_icon(application: QApplication) -> None:
    for font_root in _resource_roots():
        icon_path = font_root.parent / "app_icon.png"
        if icon_path.is_file():
            application.setWindowIcon(QIcon(str(icon_path)))
            return


def _install_light_palette(application: QApplication) -> None:
    """Use an explicit high-contrast light palette regardless of Windows theme."""
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: "#f5f7fb",
        QPalette.ColorRole.WindowText: "#172033",
        QPalette.ColorRole.Base: "#ffffff",
        QPalette.ColorRole.AlternateBase: "#f7f9fc",
        QPalette.ColorRole.ToolTipBase: "#fffbea",
        QPalette.ColorRole.ToolTipText: "#172033",
        QPalette.ColorRole.Text: "#172033",
        QPalette.ColorRole.Button: "#eef2f7",
        QPalette.ColorRole.ButtonText: "#172033",
        QPalette.ColorRole.BrightText: "#ffffff",
        QPalette.ColorRole.Highlight: "#dbeafe",
        QPalette.ColorRole.HighlightedText: "#102a56",
        QPalette.ColorRole.Link: "#1769d2",
        QPalette.ColorRole.PlaceholderText: "#667085",
    }
    for role, color in colors.items():
        palette.setColor(QPalette.ColorGroup.All, role, QColor(color))
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#717b8d"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base, QColor("#eef1f5"))
    application.setPalette(palette)


def _selection_self_test(window: MainWindow) -> bool:
    """Exercise frozen Qt combo payloads and process-time live selection sync."""
    with tempfile.TemporaryDirectory(prefix="abcopy-self-test-") as directory:
        return _selection_self_test_in_directory(window, Path(directory))


def _selection_self_test_in_directory(window: MainWindow, directory: Path) -> bool:
    if window.reliable_defaults_check.isChecked():
        return False
    source_path = directory / "source.mkv"
    destination_path = directory / "destination.mp4"
    previous_output_path = directory / "destination_copied_audio.mp4"
    source_path.write_bytes(b"source self-test")
    destination_path.write_bytes(b"destination self-test")
    previous_output_path.write_bytes(b"protected previous output")
    source_stat = source_path.stat()
    destination_stat = destination_path.stat()
    source = SourceEntry(source_path)
    source.apply_probe(
        MediaInfo(
            source_path,
            (
                MediaStream(
                    1,
                    "audio",
                    "aac",
                    {"language": "yue", "title": "Cantonese"},
                ),
                MediaStream(
                    2,
                    "audio",
                    "aac",
                    {"language": "cmn", "title": "Mandarin"},
                    frozenset({"default"}),
                ),
                MediaStream(
                    3,
                    "subtitle",
                    "subrip",
                    {"title": "English"},
                    frozenset({"default"}),
                ),
            ),
            "self-test",
            1.0,
            source_stat.st_size,
            source_stat.st_mtime_ns,
        )
    )
    destination = DestinationEntry(destination_path)
    destination.apply_probe(
        MediaInfo(
            destination_path,
            (MediaStream(0, "video", "h264"),),
            "self-test",
            1.0,
            destination_stat.st_size,
            destination_stat.st_mtime_ns,
        )
    )
    window.state.sources.append(source)
    window.state.destinations.append(destination)
    window.render_table()

    audio_widget = window.table.cellWidget(0, 2)
    subtitle_widget = window.table.cellWidget(0, 3)
    audio_combo = audio_widget.findChild(QComboBox) if audio_widget else None
    subtitle_combo = subtitle_widget.findChild(QComboBox) if subtitle_widget else None
    if audio_combo is None or subtitle_combo is None:
        return False
    source_first = TrackSelection("source", 1)
    source_default = TrackSelection("source", 2)
    subtitle_default = TrackSelection("source", 3)
    if audio_combo.currentData() != source_default.to_token():
        return False
    first_index = audio_combo.findData(source_first.to_token())
    if first_index < 0:
        return False
    audio_combo.setCurrentIndex(first_index)
    subtitle_combo.setCurrentIndex(0)

    # Deliberately restore stale signal-time state. _build_jobs must read the
    # displayed combos and replace it with the visible selections.
    pair_key = (source.id, destination.id)
    window._audio_default_choices[pair_key] = source_default
    window._subtitle_default_choices[pair_key] = subtitle_default
    source.selected_audio_index = source_default.stream_index
    source.selected_subtitle_index = subtitle_default.stream_index
    jobs, skipped = window._build_jobs()
    mapped_audio = (
        [
            mapped
            for mapped in mapped_streams(jobs[0])
            if mapped.stream.codec_type == "audio"
        ]
        if jobs
        else []
    )
    return (
        not skipped
        and len(jobs) == 1
        and jobs[0].selected_audio == source_first
        and jobs[0].selected_subtitle is None
        and jobs[0].output.freshened
        and jobs[0].output.primary != previous_output_path
        and previous_output_path.read_bytes() == b"protected previous output"
        and bool(mapped_audio)
        and mapped_audio[0].input_index == 1
        and mapped_audio[0].stream.index == source_first.stream_index
        and expected_default_indices(jobs[0]) == ({0}, set())
    )


def _run_self_test_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=no_window_creation_flags(),
    )


def _self_test_stream_hash(toolchain: FFmpegToolchain, path: Path, stream: str) -> str:
    completed = _run_self_test_command(
        [
            str(toolchain.ffmpeg),
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            f"0:{stream}",
            "-c",
            "copy",
            "-f",
            "hash",
            "-hash",
            "sha256",
            "-",
        ]
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _compatibility_self_test(toolchain: FFmpegToolchain) -> bool:
    try:
        with tempfile.TemporaryDirectory(prefix="abcopy-compatibility-test-") as directory:
            root = Path(directory)
            source = root / "source tracks.mkv"
            destination = root / "destination video.mp4"
            source_command = [
                str(toolchain.ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=660:sample_rate=48000:duration=1.2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=880:sample_rate=48000:duration=1.2",
                "-map",
                "0:a:0",
                "-map",
                "1:a:0",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-ac:a:0",
                "1",
                "-ac:a:1",
                "2",
                "-metadata:s:a:0",
                "language=chi",
                "-metadata:s:a:0",
                "title=Cantonese",
                "-metadata:s:a:1",
                "language=chi",
                "-metadata:s:a:1",
                "title=Mandarin",
                "-disposition:a:0",
                "0",
                "-disposition:a:1",
                "default",
                "-t",
                "1.0",
                str(source),
            ]
            destination_command = [
                str(toolchain.ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=160x90:r=10:d=1.0",
                "-c:v",
                "mpeg4",
                "-q:v",
                "5",
                str(destination),
            ]
            if any(
                _run_self_test_command(command).returncode != 0
                for command in (source_command, destination_command)
            ):
                return False

            source_info = probe_media(toolchain.ffprobe, source)
            destination_info = probe_media(toolchain.ffprobe, destination)
            if len(source_info.audio_streams) != 2:
                return False
            job = PreparedJob(
                row_number=1,
                source=source_info,
                destination=destination_info,
                selected_audio=TrackSelection("source", source_info.audio_streams[0].index),
                selected_subtitle=None,
                copy_audio=True,
                copy_subtitles=False,
                keep_destination_audio=False,
                keep_destination_subtitles=False,
                output=plan_output(destination, "_compatibility", None),
            )
            job = apply_reliable_default_container(job)
            logs: list[str] = []
            result = BatchProcessor(toolchain).run_job(job, lambda _value: None, logs.append)
            if result.status != "fallback" or result.output_path is None:
                return False
            output = probe_media(toolchain.ffprobe, result.output_path)
            return (
                [item.is_default for item in source_info.audio_streams] == [False, True]
                and result.output_path.suffix.casefold() == ".mkv"
                and not destination.with_name("destination video_compatibility.mp4").exists()
                and [item.title for item in output.audio_streams] == ["Cantonese", "Mandarin"]
                and [item.is_default for item in output.audio_streams] == [True, False]
                and _self_test_stream_hash(toolchain, destination, "v:0")
                == _self_test_stream_hash(toolchain, result.output_path, "v:0")
                and _self_test_stream_hash(toolchain, source, "a:0")
                == _self_test_stream_hash(toolchain, result.output_path, "a:0")
                and any("-c copy" in line for line in logs)
                and any("using MKV for reliable default-track playback" in line for line in logs)
            )
    except (MediaProbeError, OSError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    options = _parser().parse_args(argv)
    _set_windows_app_id()
    application = QApplication(sys.argv[:1])
    application.setApplicationDisplayName(APP_NAME)
    application.setApplicationName(APP_NAME)
    application.setApplicationVersion(APP_VERSION)
    application.setOrganizationName("AudioSubtitleBatchCopy")
    application.setStyle("Fusion")
    _install_light_palette(application)
    _install_bundled_fonts(application)
    _install_window_icon(application)
    window = MainWindow()
    if options.selection_self_test:
        try:
            passed = _selection_self_test(window)
        finally:
            window.close()
        return 0 if passed else 3
    if options.compatibility_self_test:
        try:
            passed = bool(window.toolchain and _compatibility_self_test(window.toolchain))
        finally:
            window.close()
        return 0 if passed else 4
    window.show()
    if options.screenshot:
        target = options.screenshot.resolve()

        def save_screenshot() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(target), "PNG"):
                application.exit(2)
                return
            application.quit()

        QTimer.singleShot(800, save_screenshot)
    elif options.smoke_test:
        QTimer.singleShot(700, application.quit)
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
