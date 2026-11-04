import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.services.musicbrainz import ProviderError, similarity


logger = logging.getLogger(__name__)
USER_AGENT = "SmartMusicCollector/0.2 (personal local music library)"
MAX_ARTWORK_BYTES = 15 * 1024 * 1024


def _find_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in (
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
    ):
        if Path(candidate).is_file():
            return candidate
    return "ffmpeg"


def _convert_to_jpeg(data: bytes) -> tuple[bytes, str]:
    if data.startswith(b"\xff\xd8\xff"):
        return data, "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data, "image/png"

    ffmpeg_bin = _find_ffmpeg()
    with tempfile.NamedTemporaryFile(suffix=".tmp", delete=False) as in_f:
        in_f.write(data)
        in_path = in_f.name
    out_path = in_path + ".jpg"
    try:
        subprocess.run(
            [ffmpeg_bin, "-y", "-i", in_path, "-q:v", "2", out_path],
            check=True,
            capture_output=True,
        )
        converted = Path(out_path).read_bytes()
        return converted, "image/jpeg"
    except Exception as error:
        logger.warning("Failed to convert image to JPEG: %s", error)
        return data, "image/jpeg"
    finally:
        Path(in_path).unlink(missing_ok=True)
        Path(out_path).unlink(missing_ok=True)


def _image_response(url: str) -> tuple[bytes, str] | None:
    try:
        with httpx.stream(
            "GET",
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=20,
            follow_redirects=True,
        ) as response:
            if response.status_code == 404:
                return None
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > MAX_ARTWORK_BYTES:
                raise ProviderError("Artwork response exceeded the 15 MiB limit")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_ARTWORK_BYTES:
                    raise ProviderError("Artwork response exceeded the 15 MiB limit")
                chunks.append(chunk)
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    except httpx.HTTPError as error:
        raise ProviderError(f"Artwork request failed: {error}") from error

    data = b"".join(chunks)
    if data.startswith(b"\xff\xd8\xff"):
        return data, "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data, "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a") or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        return _convert_to_jpeg(data)
    if content_type.startswith("image/"):
        return _convert_to_jpeg(data)
    raise ProviderError("Artwork provider returned a non-image response")


def _cover_art_archive(release_id: str) -> tuple[bytes, str] | None:
    url = f"https://coverartarchive.org/release/{quote(release_id)}/front-1200"
    return _image_response(url)


def _itunes_fallback(candidate: dict[str, Any]) -> tuple[bytes, str] | None:
    search_terms = []
    rel_name = candidate.get("release_name")
    title = candidate.get("title")
    artist = candidate.get("artist") or candidate.get("album_artist") or ""
    primary_artist = artist.split(",")[0].strip()

    if rel_name and primary_artist:
        search_terms.append((f"{rel_name} {primary_artist}", "album"))
    if title and primary_artist:
        search_terms.append((f"{title} {primary_artist}", "song"))
    if rel_name:
        search_terms.append((rel_name, "album"))
    if title:
        search_terms.append((title, "song"))

    for term, entity in search_terms:
        try:
            response = httpx.get(
                "https://itunes.apple.com/search",
                params={"term": term, "entity": entity, "limit": 6},
                headers={"User-Agent": USER_AGENT},
                timeout=12,
            )
            response.raise_for_status()
            results = response.json().get("results", [])
        except Exception:
            continue

        for item in results:
            image_url = item.get("artworkUrl100")
            if not image_url:
                continue
            # Check match
            target_name = rel_name if entity == "album" else title
            item_name = item.get("collectionName") if entity == "album" else item.get("trackName")
            name_score = similarity(target_name, item_name)
            name_match = (
                name_score >= 0.55
                or (bool(target_name and item_name) and target_name.lower() in item_name.lower())
                or (bool(target_name and item_name) and item_name.lower() in target_name.lower())
            )

            # Check artist match
            raw_item_artist = item.get("artistName") or ""
            item_artist = raw_item_artist.lower()
            p_artist = primary_artist.lower()
            artist_match = (
                not primary_artist
                or similarity(primary_artist, raw_item_artist) >= 0.5
                or (bool(p_artist) and p_artist in item_artist)
                or (bool(p_artist) and item_artist in p_artist)
                or any(similarity(p.strip(), raw_item_artist) >= 0.6 for p in artist.split(",") if p.strip())
                or any(p.strip().lower() in item_artist for p in artist.split(",") if p.strip())
            )
            if name_match and artist_match:
                large_url = image_url.replace("100x100bb", "1200x1200bb")
                image = _image_response(large_url)
                if image:
                    return image
    return None


def find_artwork(candidate: dict[str, Any]) -> dict[str, Any]:
    release_id = candidate.get("release_id")
    cover_art_error = None
    if release_id:
        try:
            image = _cover_art_archive(release_id)
        except ProviderError as error:
            cover_art_error = error
            image = None
        if image:
            return {
                "status": "available",
                "data": image[0],
                "mime_type": image[1],
                "extension": _extension_for(image[1]),
                "provider": "Cover Art Archive",
            }

    try:
        image = _itunes_fallback(candidate)
    except ProviderError as error:
        if cover_art_error:
            raise ProviderError(
                f"Cover Art Archive failed ({cover_art_error}); iTunes fallback failed ({error})"
            ) from error
        raise

    if image:
        return {
            "status": "available",
            "data": image[0],
            "mime_type": image[1],
            "extension": _extension_for(image[1]),
            "provider": "Apple iTunes Search API",
        }

    if cover_art_error:
        raise cover_art_error
    return {"status": "unavailable", "data": None, "mime_type": None}


def artwork_from_url(url: str, provider: str = "YouTube Thumbnail") -> dict[str, Any] | None:
    try:
        image = _image_response(url)
        if image:
            return {
                "status": "available",
                "data": image[0],
                "mime_type": image[1],
                "extension": _extension_for(image[1]),
                "provider": provider,
            }
    except Exception as error:
        logger.debug("Failed to get artwork from url %s: %s", url, error)
    return None


def _extension_for(mime_type: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
    }.get(mime_type, ".jpg")
