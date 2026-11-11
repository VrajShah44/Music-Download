import logging
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,
    TALB,
    TCMP,
    TCOM,
    TCON,
    TDRC,
    TIT2,
    TLAN,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    TEXT,
    TXXX,
    USLT,
)
from mutagen.mp4 import MP4, MP4Cover
from mutagen.mp3 import MP3
from mutagen.wave import WAVE


logger = logging.getLogger(__name__)


def _first(tags: Any, key: str) -> Any:
    try:
        value = tags.get(key)
        return value[0] if value else None
    except (AttributeError, IndexError, KeyError, TypeError):
        return None


def tag_audio(
    path: Path,
    fields: dict[str, Any],
    cover_path: Path | None = None,
    lyrics_text: str | None = None,
) -> None:
    audio = MutagenFile(path)
    if audio is None:
        raise ValueError(f"Unsupported or invalid audio file: {path.name}")

    title = fields.get("song_title")
    artist = fields.get("artist")
    album = fields.get("album_name")
    album_artist = fields.get("album_artist")
    is_compilation = bool(
        fields.get("is_compilation")
        or fields.get("movie_name")
        or (artist and "," in str(artist) and not album_artist)
    )
    if not album_artist:
        album_artist = "Various Artists" if is_compilation else artist
    composer = fields.get("composer")
    lyricist = fields.get("lyricist")
    genre = fields.get("genre")
    year = fields.get("year")
    language = fields.get("language")
    track = fields.get("track_number")
    total_tracks = fields.get("total_tracks")
    disc = fields.get("disc_number")
    recording_id = fields.get("musicbrainz_recording_id")

    if isinstance(audio, (MP3, WAVE)):
        if audio.tags is None:
            audio.add_tags()
        updates = (
            (TIT2, title),
            (TPE1, artist),
            (TALB, album),
            (TPE2, album_artist),
            (TCOM, composer),
            (TEXT, lyricist),
            (TCON, genre),
            (TDRC, year),
            (TLAN, language),
            (
                TRCK,
                f"{track}/{total_tracks}" if track and total_tracks else str(track)
                if track
                else None,
            ),
            (TPOS, str(disc) if disc else None),
        )
        for frame_type, value in updates:
            if value and not _first(audio.tags, frame_type.__name__):
                audio.tags.add(frame_type(encoding=3, text=[str(value)]))
        if is_compilation and not _first(audio.tags, "TCMP"):
            audio.tags.add(TCMP(encoding=3, text=["1"]))
        if recording_id and not audio.tags.getall("TXXX:MusicBrainz Track Id"):
            audio.tags.add(
                TXXX(
                    encoding=3,
                    desc="MusicBrainz Track Id",
                    text=[str(recording_id)],
                )
            )
        if lyrics_text and not audio.tags.getall("USLT"):
            audio.tags.add(
                USLT(
                    encoding=3,
                    lang="eng",
                    desc="",
                    text=str(lyrics_text),
                )
            )
        cover_mime = (
            "image/png"
            if cover_path and cover_path.suffix.lower() == ".png"
            else "image/jpeg"
        )
        if cover_path and cover_path.is_file() and not audio.tags.getall("APIC"):
            audio.tags.add(
                APIC(
                    encoding=3,
                    mime=cover_mime,
                    type=3,
                    desc="Cover",
                    data=cover_path.read_bytes(),
                )
            )
    elif isinstance(audio, FLAC):
        for key, value in (
            ("title", title),
            ("artist", artist),
            ("album", album),
            ("albumartist", album_artist),
            ("composer", composer),
            ("lyricist", lyricist),
            ("genre", genre),
            ("date", year),
            ("language", language),
            (
                "tracknumber",
                f"{track}/{total_tracks}" if track and total_tracks else track,
            ),
            ("discnumber", disc),
        ):
            if value and not audio.get(key):
                audio[key] = str(value)
        if recording_id and not audio.get("musicbrainz_trackid"):
            audio["musicbrainz_trackid"] = [str(recording_id)]
        if lyrics_text and not audio.get("lyrics"):
            audio["lyrics"] = [str(lyrics_text)]
        if cover_path and cover_path.is_file() and not audio.pictures:
            picture = Picture()
            picture.data = cover_path.read_bytes()
            picture.type = 3
            picture.mime = (
                "image/png"
                if cover_path.suffix.lower() == ".png"
                else "image/jpeg"
            )
            picture.desc = "Cover"
            audio.add_picture(picture)
    elif isinstance(audio, MP4):
        if audio.tags is None:
            audio.add_tags()
        updates = (
            ("\xa9nam", title),
            ("\xa9ART", artist),
            ("\xa9alb", album),
            ("aART", album_artist),
            ("\xa9wrt", composer),
            ("\xa9day", year),
            ("\xa9gen", genre),
        )
        for key, value in updates:
            if value:
                audio.tags[key] = [str(value)]
        if is_compilation:
            audio.tags["cpil"] = True
        if track and not audio.tags.get("trkn"):
            try:
                import re
                digits = re.findall(r"\d+", str(track))
                t_num = int(digits[0]) if digits else 1
                tot_digits = re.findall(r"\d+", str(total_tracks or 0))
                tot_num = int(tot_digits[0]) if tot_digits else 0
                audio.tags["trkn"] = [(t_num, tot_num)]
            except Exception as error:
                logger.warning("Cannot encode non-numeric MP4 track number %r: %s", track, error)
        if disc and not audio.tags.get("disk"):
            try:
                audio.tags["disk"] = [(int(str(disc).split("/", 1)[0]), 0)]
            except ValueError as error:
                logger.warning("Cannot encode non-numeric MP4 disc number %r: %s", disc, error)
        if language and not audio.tags.get("----:com.apple.iTunes:LANGUAGE"):
            audio.tags["----:com.apple.iTunes:LANGUAGE"] = [language.encode()]
        if recording_id and not audio.tags.get(
            "----:com.apple.iTunes:MusicBrainz Track Id"
        ):
            audio.tags["----:com.apple.iTunes:MusicBrainz Track Id"] = [
                str(recording_id).encode()
            ]
        if lyricist and not audio.tags.get("----:com.apple.iTunes:LYRICIST"):
            audio.tags["----:com.apple.iTunes:LYRICIST"] = [lyricist.encode()]
        if lyrics_text:
            # MP4 lyrics atom (©lyr) in Apple ecosystem uses standard carriage returns '\r'
            audio.tags["\xa9lyr"] = [str(lyrics_text).replace("\r\n", "\n").replace("\n", "\r")]
        if cover_path and cover_path.is_file():
            image_format = (
                MP4Cover.FORMAT_PNG
                if cover_path.suffix.lower() == ".png"
                else MP4Cover.FORMAT_JPEG
            )
            audio.tags["covr"] = [
                MP4Cover(cover_path.read_bytes(), imageformat=image_format)
            ]
    else:
        raise ValueError(
            f"Audio tagging is not supported for {type(audio).__name__}; supported tag formats are MP3, FLAC, and M4A."
        )
    audio.save()
