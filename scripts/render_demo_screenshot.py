from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from audio_subtitle_batch_copy.app import _install_bundled_fonts, _install_light_palette
from audio_subtitle_batch_copy.media import discover_toolchain
from audio_subtitle_batch_copy.models import (
    DestinationEntry,
    MediaInfo,
    MediaStream,
    SourceEntry,
    TrackSelection,
)
from audio_subtitle_batch_copy.ui import MainWindow


def stream(
    index: int,
    kind: str,
    codec: str,
    language: str,
    title: str,
    *,
    default: bool = False,
    forced: bool = False,
) -> MediaStream:
    dispositions = set()
    if default:
        dispositions.add("default")
    if forced:
        dispositions.add("forced")
    return MediaStream(
        index=index,
        codec_type=kind,
        codec_name=codec,
        tags={"language": language, "title": title},
        dispositions=frozenset(dispositions),
        channels=6 if kind == "audio" else None,
        channel_layout="5.1" if kind == "audio" else None,
    )


def info(path: Path, streams: tuple[MediaStream, ...]) -> MediaInfo:
    return MediaInfo(path, streams, "demo", 5400.0, 1, 1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--dark-start",
        action="store_true",
        help="Begin with a dark system-like palette before applying the app palette.",
    )
    parser.add_argument("--select-row", type=int, default=2, help="One-based row to select.")
    options = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Audio and Subtitle Batch Copy")
    app.setOrganizationName("AudioSubtitleBatchCopy")
    app.setStyle("Fusion")
    if options.dark_start:
        dark_palette = QPalette()
        dark_palette.setColor(QPalette.ColorRole.Window, QColor("#202124"))
        dark_palette.setColor(QPalette.ColorRole.WindowText, QColor("#ffffff"))
        dark_palette.setColor(QPalette.ColorRole.Base, QColor("#202124"))
        dark_palette.setColor(QPalette.ColorRole.Text, QColor("#ffffff"))
        dark_palette.setColor(QPalette.ColorRole.Button, QColor("#303134"))
        dark_palette.setColor(QPalette.ColorRole.ButtonText, QColor("#ffffff"))
        app.setPalette(dark_palette)
    _install_light_palette(app)
    _install_bundled_fonts(app)
    window = MainWindow(toolchain=discover_toolchain())
    samples = [
        (
            "Series.S01E02.Japanese.BluRay.Remux.mkv",
            "Series.S01E02.2160p.WEB-DL.HDR.mp4",
            (
                stream(1, "audio", "truehd", "jpn", "Japanese Atmos", default=True),
                stream(2, "audio", "aac", "eng", "English commentary"),
                stream(3, "subtitle", "hdmv_pgs_subtitle", "eng", "English signs"),
                stream(4, "subtitle", "hdmv_pgs_subtitle", "eng", "English full", default=True),
            ),
        ),
        (
            "Documentary Episode 10 - source audio.flac",
            "Documentary Episode 10 - restored video.mkv",
            (
                stream(0, "audio", "flac", "eng", "Original mix", default=True),
                stream(1, "subtitle", "subrip", "eng", "SDH"),
            ),
        ),
        (
            "Movie_003_multilingual_source.mov",
            "Movie_003_destination.mov",
            (
                stream(0, "audio", "pcm_s24le", "fra", "French 5.1", default=True),
                stream(1, "subtitle", "ass", "spa", "Spanish forced", forced=True),
            ),
        ),
    ]
    for sample_index, (source_name, destination_name, streams) in enumerate(samples):
        source_path = Path("C:/Media/Source") / source_name
        destination_path = Path("C:/Media/Destination") / destination_name
        source_entry = SourceEntry(source_path)
        source_entry.apply_probe(info(source_path, streams))
        destination_entry = DestinationEntry(destination_path)
        destination_entry.apply_probe(
            info(
                destination_path,
                (
                    stream(0, "video", "hevc", "und", "Main video", default=True),
                    stream(1, "audio", "aac", "eng", "Destination audio", default=True),
                    stream(2, "subtitle", "subrip", "eng", "Destination captions"),
                ),
            )
        )
        if sample_index == 1:
            destination_entry.keep_audio = True
            destination_entry.keep_subtitles = True
            pair_key = (source_entry.id, destination_entry.id)
            window._audio_default_choices[pair_key] = TrackSelection("destination", 1)
            window._subtitle_default_choices[pair_key] = TrackSelection("destination", 2)
        window.state.sources.append(source_entry)
        window.state.destinations.append(destination_entry)
    window.render_table()
    if 1 <= options.select_row <= window.table.rowCount():
        window.table.selectRow(options.select_row - 1)
    window.show()
    target = options.output.resolve()

    def capture() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not window.grab().save(str(target), "PNG"):
            app.exit(2)
            return
        app.quit()

    QTimer.singleShot(800, capture)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
