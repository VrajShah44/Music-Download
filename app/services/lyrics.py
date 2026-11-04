import os
import random
import re
import time
from typing import Any

import httpx

from app.services.musicbrainz import ProviderError, similarity


BASE_URL = "https://lrclib.net/api"
CONTACT = os.environ.get("SMC_CONTACT", "http://127.0.0.1:8000/docs")
USER_AGENT = f"SmartMusicCollector/0.2 ({CONTACT})"


def _get_json(path: str, params: dict[str, Any]) -> Any:
    for attempt in range(3):
        try:
            response = httpx.get(
                f"{BASE_URL}/{path}",
                params=params,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=15,
            )
            if response.status_code in {429, 503}:
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and data.get("statusCode") in {429, 503}:
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
            return data
        except (httpx.HTTPError, ValueError) as error:
            if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 404:
                raise ProviderError(f"LRCLIB returned 404: {error}") from error
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise ProviderError(f"LRCLIB request failed: {error}") from error
    raise ProviderError("LRCLIB remained unavailable after retries")


def _is_match(item: dict[str, Any], candidate: dict[str, Any]) -> bool:
    candidate_artists = candidate.get("artist_names") or re.split(
        r"\s*(?:,|&|\bfeat\.?\b)\s*", candidate.get("artist") or "", flags=re.I
    )
    lyric_artists = re.split(
        r"\s*(?:,|&|\bfeat\.?\b)\s*", item.get("artistName") or "", flags=re.I
    )
    artist_score = max(
        (
            similarity(candidate_artist, lyric_artist)
            for candidate_artist in candidate_artists
            for lyric_artist in lyric_artists
        ),
        default=0.0,
    )
    candidate_artist_str = str(candidate.get("artist") or "").lower()
    item_artist_str = str(item.get("artistName") or "").lower()
    artist_substring = any(
        ca.strip().lower() in item_artist_str for ca in candidate_artists if len(ca.strip()) > 3
    ) or any(
        la.strip().lower() in candidate_artist_str for la in lyric_artists if len(la.strip()) > 3
    )
    artist_matched = (artist_score >= 0.70) or artist_substring

    target_title = str(candidate.get("title") or "")
    item_title = str(item.get("trackName") or "")
    title_score = similarity(target_title, item_title)
    title_match = (
        title_score >= 0.75
        or (bool(target_title and item_title) and target_title.lower() in item_title.lower())
        or (bool(target_title and item_title) and item_title.lower() in target_title.lower())
    )
    return title_match and artist_matched


def clean_lyrics(text: str | None) -> str | None:
    if not text:
        return None
    lines = text.splitlines()
    cleaned = []
    skip_until_tag = False
    for line in lines:
        stripped = line.strip()
        if not cleaned and (
            stripped.endswith("के बोल]")
            or stripped.endswith("Lyrics]")
            or ("Lyrics" in stripped and stripped.startswith("["))
        ):
            continue
        if re.search(r"See\s+.*Live", stripped, re.I) or re.search(
            r"Get tickets as low as", stripped, re.I
        ):
            skip_until_tag = True
            continue
        if re.search(r"You might also like", stripped, re.I) or re.search(
            r"Genius Romanizations", stripped, re.I
        ):
            skip_until_tag = True
            continue
        if skip_until_tag:
            if stripped.startswith("[") and stripped.endswith("]"):
                skip_until_tag = False
            else:
                continue
        if re.match(r"^\d*Embed$", stripped):
            continue
        cleaned.append(line)
    result = "\n".join(cleaned).strip()
    return result or None


def find_lyrics(candidate: dict[str, Any]) -> dict[str, Any]:
    duration_ms = candidate.get("duration_ms")
    artist_names = candidate.get("artist_names") or re.split(
        r"\s*(?:,|&|\bfeat\.?\b)\s*", candidate.get("artist") or "", flags=re.I
    )
    primary_artist = next((name.strip() for name in artist_names if name.strip()), "")
    exact_params = {
        "track_name": candidate["title"],
        "artist_name": primary_artist,
        "album_name": candidate.get("release_name") or "",
    }
    if duration_ms:
        exact_params["duration"] = round(duration_ms / 1000)
    try:
        exact = _get_json("get", exact_params)
    except ProviderError:
        exact = None

    matches = [exact] if isinstance(exact, dict) and _is_match(exact, candidate) else []
    if not matches:
        try:
            results = _get_json(
                "search",
                {"q": f"{candidate['title']} {candidate.get('artist') or ''}".strip()},
            )
        except ProviderError:
            results = []
        if isinstance(results, list):
            matches = [
                item for item in results if isinstance(item, dict) and _is_match(item, candidate)
            ]
    if not matches:
        return {"status": "unavailable", "synced": None, "plain": None}

    matches.sort(key=lambda item: bool(item.get("syncedLyrics")), reverse=True)
    best = matches[0]
    cleaned_plain = clean_lyrics(best.get("plainLyrics"))
    return {
        "status": "available",
        "synced": best.get("syncedLyrics") or None,
        "plain": cleaned_plain or None,
        "provider": "LRCLIB",
        "matched_track": best.get("trackName"),
        "matched_artist": best.get("artistName"),
        "matched_album": best.get("albumName"),
    }
