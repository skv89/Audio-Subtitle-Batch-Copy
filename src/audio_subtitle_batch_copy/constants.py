from __future__ import annotations

APP_NAME = "Audio and Subtitle Batch Copy"
APP_VERSION = "1.2.4"
MAX_FILES_PER_SIDE = 120
DEFAULT_SUFFIX = "_copied_audio"
REQUIRED_FFMPEG_MAJOR = 9

SOURCE_EXTENSIONS = frozenset(
    {
        ".3g2",
        ".3gp",
        ".aac",
        ".ac3",
        ".aif",
        ".aiff",
        ".alac",
        ".amv",
        ".ape",
        ".asf",
        ".avi",
        ".dts",
        ".dv",
        ".eac3",
        ".caf",
        ".f4v",
        ".flac",
        ".flv",
        ".m2ts",
        ".m4a",
        ".m4b",
        ".m4v",
        ".m1v",
        ".m2v",
        ".mka",
        ".mkv",
        ".mov",
        ".mp2",
        ".mp3",
        ".mp4",
        ".mpc",
        ".mpv",
        ".mpeg",
        ".mpg",
        ".mts",
        ".oga",
        ".ogg",
        ".ogm",
        ".ogv",
        ".opus",
        ".qt",
        ".tak",
        ".ts",
        ".tta",
        ".vob",
        ".wav",
        ".webm",
        ".wma",
        ".wmv",
        ".wtv",
        ".wv",
        ".mxf",
        ".nut",
        ".rm",
        ".rmvb",
    }
)

DESTINATION_EXTENSIONS = frozenset(
    {
        ".3g2",
        ".3gp",
        ".asf",
        ".amv",
        ".avi",
        ".divx",
        ".dv",
        ".f4v",
        ".flv",
        ".m2ts",
        ".m4v",
        ".m1v",
        ".m2v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpv",
        ".mpeg",
        ".mpg",
        ".mts",
        ".ogm",
        ".ogv",
        ".qt",
        ".ts",
        ".vob",
        ".webm",
        ".wmv",
        ".wtv",
        ".mxf",
        ".nut",
        ".rm",
        ".rmvb",
    }
)

INVALID_WINDOWS_FILENAME_CHARS = frozenset('<>:"/\\|?*')
WINDOWS_RESERVED_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)

# The stable ordering keeps generated FFmpeg disposition expressions deterministic.
DISPOSITION_ORDER = (
    "default",
    "dub",
    "original",
    "comment",
    "lyrics",
    "karaoke",
    "forced",
    "hearing_impaired",
    "visual_impaired",
    "clean_effects",
    "attached_pic",
    "timed_thumbnails",
    "non_diegetic",
    "captions",
    "descriptions",
    "metadata",
    "dependent",
    "still_image",
    "multilayer",
)
