import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

from app.services.library import safe_component


SUPPORTED_EXTENSIONS = {".mp3", ".m4a"}
AUTO_ADD_CANDIDATES = (
    Path.home() / "Music/Music/Media.localized/Automatically Add to Music.localized",
    Path.home() / "Music/Music/Media/Automatically Add to Music",
)


def _recording_id(path: Path) -> str | None:
    audio = MutagenFile(path)
    if audio is None or audio.tags is None:
        return None
    tags = audio.tags
    try:
        if isinstance(tags, ID3):
            value = tags.get("TXXX:MusicBrainz Track Id")
            return str(value.text[0]) if value and value.text else None
        if isinstance(audio, FLAC):
            value = tags.get("musicbrainz_trackid")
            return str(value[0]) if value else None
        if isinstance(audio, MP4):
            value = tags.get("----:com.apple.iTunes:MusicBrainz Track Id")
            return value[0].decode() if value else None
        value = tags.get("musicbrainz_trackid")
        return str(value[0]) if value else None
    except (AttributeError, IndexError, KeyError, TypeError):
        return None


def _find_existing_recording(
    directories: list[Path], recording_id: str | None, exclude: Path
) -> Path | None:
    if not recording_id:
        return None
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if not path.is_file() or path == exclude or path.suffix.lower() not in {
                ".mp3",
                ".m4a",
                ".wav",
                ".flac",
            }:
                continue
            try:
                if _recording_id(path) == recording_id:
                    return path
            except (OSError, ValueError):
                continue
    return None


def _automatic_add_folder() -> Path:
    configured = os.environ.get("SMC_AUTO_ADD_DIR")
    if configured:
        directory = Path(configured).expanduser()
        if not directory.is_dir():
            raise FileNotFoundError(
                f"Configured Apple Music automatic-add folder does not exist: {directory}"
            )
        return directory
    for directory in AUTO_ADD_CANDIDATES:
        if directory.is_dir():
            return directory
    raise FileNotFoundError(
        "Apple Music's Automatically Add to Music folder was not found. "
        "Set SMC_AUTO_ADD_DIR to the folder shown in Finder."
    )


def queue_track(
    audio_path: Path,
    job_id: str,
    fields: dict[str, Any],
    cover_path: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    extension = audio_path.suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        return {
            "status": "unsupported_format",
            "message": (
                "Apple Music automatic import supports MP3 and M4A here. "
                "The local file was preserved and not transcoded."
            ),
            "file": str(audio_path),
        }

    recording_id = fields.get("musicbrainz_recording_id")
    auto_add_directory = _automatic_add_folder()
    music_media_directory = auto_add_directory.parent
    music_library_directories = [
        directory
        for directory in music_media_directory.iterdir()
        if directory.is_dir() and directory != auto_add_directory
    ]
    if not force:
        existing_library_file = _find_existing_recording(
            music_library_directories, recording_id, audio_path
        )
        if existing_library_file:
            return {
                "status": "already_in_library",
                "file": str(existing_library_file),
                "message": "This MusicBrainz recording is already in the local library.",
            }

        already_queued = _find_existing_recording(
            [auto_add_directory], recording_id, audio_path
        )
        if already_queued:
            return {
                "status": "already_queued",
                "file": str(already_queued),
            }

    title = safe_component(fields.get("song_title"), "Unknown Track")
    artist = safe_component(fields.get("artist"), "Unknown Artist")
    base_name = f"{title} - {artist}"
    target = auto_add_directory / f"{base_name}{extension}"
    suffix = 2
    while target.exists():
        target = auto_add_directory / f"{base_name} ({suffix}){extension}"
        suffix += 1

    temporary_path = auto_add_directory / f".{uuid.uuid4().hex}.partial"
    try:
        shutil.copy2(audio_path, temporary_path)
        os.replace(temporary_path, target)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise

    return {
        "status": "queued",
        "file": str(target),
        "automatic_add_folder": str(auto_add_directory),
        "message": (
            "Copied to Automatically Add to Music. Music.app will import the "
            "embedded audio tags and cover art when its folder watcher processes it."
        ),
    }
