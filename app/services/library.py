import json
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any


def safe_component(value: str | None, fallback: str) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:120] or fallback


def track_directory(root: Path, fields: dict[str, Any]) -> Path:
    language = safe_component(fields.get("language"), "Unknown Language")
    album = safe_component(
        fields.get("movie_name") or fields.get("album_name"), "Unknown Album"
    )
    title = safe_component(fields.get("song_title"), "Unknown Track")
    artist = safe_component(fields.get("artist"), "Unknown Artist")
    destination = root / language / album / f"{title} - {artist}"
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def write_assets(
    destination: Path,
    metadata: dict[str, Any],
    lyrics: dict[str, Any],
    artwork: dict[str, Any],
) -> list[str]:
    written: list[str] = []
    metadata_path = destination / "metadata.json"
    temporary_metadata_path = destination / f".metadata-{uuid.uuid4().hex}.tmp"
    temporary_metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_metadata_path.replace(metadata_path)
    written.append(str(metadata_path))

    if lyrics.get("status") == "existing":
        written.extend(lyrics.get("files", []))
    elif lyrics.get("status") == "available":
        title = safe_component(metadata["fields"].get("song_title"), "lyrics")
        if lyrics.get("synced"):
            path = destination / f"{title}.lrc"
            if not path.exists():
                path.write_text(lyrics["synced"], encoding="utf-8")
            written.append(str(path))
            std_lrc = destination / "lyrics.lrc"
            if not std_lrc.exists():
                std_lrc.write_text(lyrics["synced"], encoding="utf-8")
        if lyrics.get("plain"):
            from app.services.lyrics import clean_lyrics
            cleaned_text = clean_lyrics(lyrics["plain"]) or lyrics["plain"]
            path = destination / f"{title}.txt"
            if not path.exists():
                path.write_text(cleaned_text, encoding="utf-8")
            written.append(str(path))
            std_txt = destination / "lyrics.txt"
            if not std_txt.exists():
                std_txt.write_text(cleaned_text, encoding="utf-8")

    if artwork.get("status") == "available" and artwork.get("data"):
        ext = artwork.get("extension", ".jpg")
        if ext not in {".jpg", ".png"}:
            ext = ".jpg"
        path = destination / f"cover{ext}"
        if not path.exists():
            path.write_bytes(artwork["data"])
        written.append(str(path))
    elif artwork.get("status") == "existing" and artwork.get("path"):
        written.append(artwork["path"])
    return written
