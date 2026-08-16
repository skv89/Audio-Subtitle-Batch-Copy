from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

ISO_BMFF_EXTENSIONS = frozenset({".3g2", ".3gp", ".m4a", ".m4v", ".mj2", ".mov", ".mp4"})


class IsoBmffError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class IsoBmffTrackHeader:
    track_id: int
    handler_type: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class _Box:
    box_type: bytes
    start: int
    size: int
    header_size: int

    @property
    def payload_start(self) -> int:
        return self.start + self.header_size

    @property
    def end(self) -> int:
        return self.start + self.size


def is_iso_bmff_path(path: Path) -> bool:
    return path.suffix.casefold() in ISO_BMFF_EXTENSIONS


def _read_exact(handle: BinaryIO, size: int, *, context: str) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise IsoBmffError(f"Truncated ISO-BMFF data while reading {context}.")
    return data


def _iter_boxes(handle: BinaryIO, start: int, end: int) -> Iterator[_Box]:
    offset = start
    while offset < end:
        if end - offset < 8:
            raise IsoBmffError("Trailing bytes do not form a complete ISO-BMFF box header.")
        handle.seek(offset)
        header = _read_exact(handle, 8, context="box header")
        size = int.from_bytes(header[:4], "big")
        box_type = header[4:8]
        header_size = 8
        if size == 1:
            size = int.from_bytes(
                _read_exact(handle, 8, context="extended box size"), "big"
            )
            header_size = 16
        elif size == 0:
            size = end - offset
        if box_type == b"uuid":
            _read_exact(handle, 16, context="UUID box identifier")
            header_size += 16
        if size < header_size or offset + size > end:
            name = box_type.decode("latin-1", errors="replace")
            raise IsoBmffError(f"Invalid ISO-BMFF box size for {name!r}.")
        yield _Box(box_type, offset, size, header_size)
        offset += size


def _first_child(handle: BinaryIO, parent: _Box, box_type: bytes) -> _Box | None:
    return next(
        (
            child
            for child in _iter_boxes(handle, parent.payload_start, parent.end)
            if child.box_type == box_type
        ),
        None,
    )


def _parse_tkhd(handle: BinaryIO, box: _Box) -> tuple[int, bool]:
    handle.seek(box.payload_start)
    full_header = _read_exact(handle, 4, context="tkhd full-box header")
    version = full_header[0]
    flags = int.from_bytes(full_header[1:4], "big")
    if version == 0:
        handle.seek(box.payload_start + 12)
    elif version == 1:
        handle.seek(box.payload_start + 20)
    else:
        raise IsoBmffError(f"Unsupported tkhd version {version}.")
    track_id = int.from_bytes(_read_exact(handle, 4, context="tkhd track ID"), "big")
    return track_id, bool(flags & 0x000001)


def _parse_handler_type(handle: BinaryIO, box: _Box) -> str:
    if box.size < box.header_size + 12:
        raise IsoBmffError("The hdlr box is too short.")
    handle.seek(box.payload_start + 8)
    raw = _read_exact(handle, 4, context="hdlr handler type")
    return raw.decode("latin-1", errors="replace")


def inspect_iso_bmff_track_headers(path: Path) -> tuple[IsoBmffTrackHeader, ...]:
    try:
        file_size = path.stat().st_size
        with path.open("rb") as handle:
            moov = next(
                (
                    box
                    for box in _iter_boxes(handle, 0, file_size)
                    if box.box_type == b"moov"
                ),
                None,
            )
            if moov is None:
                raise IsoBmffError("The ISO-BMFF output has no moov box.")
            tracks: list[IsoBmffTrackHeader] = []
            for trak in _iter_boxes(handle, moov.payload_start, moov.end):
                if trak.box_type != b"trak":
                    continue
                tkhd = _first_child(handle, trak, b"tkhd")
                mdia = _first_child(handle, trak, b"mdia")
                hdlr = _first_child(handle, mdia, b"hdlr") if mdia is not None else None
                if tkhd is None or hdlr is None:
                    raise IsoBmffError("An ISO-BMFF track lacks tkhd or mdia/hdlr metadata.")
                track_id, enabled = _parse_tkhd(handle, tkhd)
                tracks.append(
                    IsoBmffTrackHeader(
                        track_id=track_id,
                        handler_type=_parse_handler_type(handle, hdlr),
                        enabled=enabled,
                    )
                )
            if not tracks:
                raise IsoBmffError("The ISO-BMFF output has no track headers.")
            return tuple(tracks)
    except OSError as exc:
        raise IsoBmffError(f"Could not inspect ISO-BMFF track headers: {exc}") from exc


def audio_track_enabled_flags(path: Path) -> tuple[bool, ...]:
    return tuple(
        track.enabled
        for track in inspect_iso_bmff_track_headers(path)
        if track.handler_type == "soun"
    )
