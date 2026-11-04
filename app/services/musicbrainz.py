import re
import os
import threading
import time
import unicodedata
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import quote

import httpx


BASE_URL = "https://musicbrainz.org/ws/2"
CONTACT = os.environ.get("SMC_CONTACT", "http://127.0.0.1:8000/docs")
USER_AGENT = f"SmartMusicCollector/0.2 ({CONTACT})"
_request_lock = threading.Lock()
_last_request_at = 0.0


class ProviderError(RuntimeError):
    pass


def _request_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    global _last_request_at
    for attempt in range(3):
        with _request_lock:
            delay = 1.1 - (time.monotonic() - _last_request_at)
            if delay > 0:
                time.sleep(delay)
            try:
                response = httpx.get(
                    url,
                    params=params,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                    timeout=15,
                )
                _last_request_at = time.monotonic()
                if response.status_code in {429, 503} and attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as error:
                if (
                    attempt < 2
                    and isinstance(error, httpx.HTTPStatusError)
                    and error.response.status_code in {429, 503}
                ):
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise ProviderError(f"MusicBrainz request failed: {error}") from error
    raise ProviderError("MusicBrainz request failed after 3 retries")


def normalize(value: str | None) -> str:
    if not value:
        return ""
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", folded.lower()).split())


def similarity(left: str | None, right: str | None) -> float:
    a, b = normalize(left), normalize(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _credit_names(credit: Any) -> list[str]:
    if not isinstance(credit, list):
        return []
    return [
        item.get("name") or item.get("artist", {}).get("name", "")
        for item in credit
        if isinstance(item, dict)
    ]


def _candidate(recording: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    artists = _credit_names(recording.get("artist-credit"))
    releases = recording.get("releases") or []
    album_target = request.get("album_name") or request.get("movie_name")
    release = (
        max(
            releases,
            key=lambda item: similarity(album_target, item.get("title")),
        )
        if releases and album_target
        else releases[0]
        if releases
        else {}
    )
    title_score = similarity(request.get("song_title"), recording.get("title"))
    artist_target = request.get("artist")
    artist_score = (
        max((similarity(artist_target, artist) for artist in artists), default=0.0)
        if artist_target
        else 0.0
    )
    release_score = (
        similarity(album_target, release.get("title")) if album_target else 0
    )
    weights = [(title_score, 0.55)]
    if artist_target:
        weights.append((artist_score, 0.35))
    if album_target:
        weights.append((release_score, 0.10))
    score = sum(value * weight for value, weight in weights) / sum(
        weight for _, weight in weights
    )
    date = release.get("date") or ""
    return {
        "recording_id": recording.get("id"),
        "title": recording.get("title"),
        "artist": ", ".join(artists) or None,
        "artist_names": artists,
        "album_artist": ", ".join(_credit_names(release.get("artist-credit"))) or None,
        "release_id": release.get("id"),
        "release_name": release.get("title"),
        "release_date": date or None,
        "year": date[:4] if len(date) >= 4 and date[:4].isdigit() else None,
        "duration_ms": recording.get("length"),
        "score": round(score, 4),
        "musicbrainz_url": (
            f"https://musicbrainz.org/recording/{quote(recording['id'])}"
            if recording.get("id")
            else None
        ),
    }


def find_recordings(
    request: dict[str, Any], limit: int = 8
) -> dict[str, Any]:
    title = request["song_title"].replace('"', r'\"')
    query = f'recording:"{title}"'
    if request.get("artist"):
        artist = request["artist"].replace('"', r'\"')
        query += f' AND artist:"{artist}"'
    data = _request_json(
        f"{BASE_URL}/recording/",
        {"query": query, "fmt": "json", "limit": min(max(limit, 1), 25)},
    )
    raw_recordings = data.get("recordings", [])
    candidates = sorted(
        (_candidate(recording, request) for recording in raw_recordings),
        key=lambda item: item["score"],
        reverse=True,
    )
    candidates = candidates[:limit]
    if not candidates:
        return {"status": "not_found", "selected": None, "candidates": []}

    minimum_score = 0.80 if request.get("artist") else 0.88
    margin = (
        candidates[0]["score"] - candidates[1]["score"]
        if len(candidates) > 1
        else 1.0
    )
    confident = candidates[0]["score"] >= minimum_score and margin >= 0.08
    return {
        "status": "matched" if confident else "needs_selection",
        "selected": candidates[0] if confident else None,
        "candidates": candidates,
    }


def recording_details(recording_id: str) -> dict[str, Any]:
    return _request_json(
        f"{BASE_URL}/recording/{quote(recording_id)}",
        {"fmt": "json", "inc": "artists+artist-credits+releases+tags+artist-rels+work-rels"},
    )


def work_details(work_id: str) -> dict[str, Any]:
    return _request_json(
        f"{BASE_URL}/work/{quote(work_id)}",
        {"fmt": "json", "inc": "artist-rels"},
    )


def release_details(release_id: str) -> dict[str, Any]:
    return _request_json(
        f"{BASE_URL}/release/{quote(release_id)}",
        {"fmt": "json", "inc": "labels+media+recordings+artist-credits"},
    )


def _role_credits(relations: Any, roles: set[str]) -> list[str]:
    if not isinstance(relations, list):
        return []
    return list(
        dict.fromkeys(
            relation["artist"]["name"]
            for relation in relations
            if relation.get("type") in roles
            and isinstance(relation.get("artist"), dict)
            and relation["artist"].get("name")
        )
    )


def metadata_from_recording(
    request: dict[str, Any], candidate: dict[str, Any], details: dict[str, Any]
) -> dict[str, Any]:
    artists = _credit_names(details.get("artist-credit"))
    tags = details.get("tags") or []
    relations = details.get("relations") or []
    work_ids = [
        relation.get("work", {}).get("id")
        for relation in relations
        if isinstance(relation.get("work"), dict) and relation["work"].get("id")
    ]
    credit_relations = relations
    credit_status = "available"
    work_language = None
    if work_ids:
        try:
            work = work_details(work_ids[0])
            credit_relations = [*relations, *(work.get("relations") or [])]
            work_language = work.get("language")
        except ProviderError:
            credit_status = "unavailable"
    composers = _role_credits(credit_relations, {"composer"})
    lyricists = _role_credits(credit_relations, {"lyricist"})
    release = details.get("selected_release") or {}
    media = release.get("media") or []
    matched_track = None
    for medium in media:
        matched_track = next(
            (
                track
                for track in medium.get("tracks", [])
                if track.get("recording", {}).get("id")
                == candidate.get("recording_id")
            ),
            None,
        )
        if matched_track:
            break
    label_names = [
        item["label"]["name"]
        for item in release.get("label-info", [])
        if isinstance(item.get("label"), dict) and item["label"].get("name")
    ]
    metadata = {
        "song_title": details.get("title") or candidate.get("title"),
        "artist": ", ".join(artists) or candidate.get("artist"),
        "featured_artists": request.get("featured_artists", []),
        "album_name": candidate.get("release_name") or request.get("album_name"),
        "album_artist": candidate.get("album_artist"),
        "movie_name": request.get("movie_name"),
        "composer": ", ".join(composers) or request.get("composer"),
        "lyricist": ", ".join(lyricists) or None,
        "language": work_language or request.get("language"),
        "genre": tags[0].get("name") if tags else None,
        "year": candidate.get("year"),
        "duration_ms": details.get("length") or candidate.get("duration_ms"),
        "track_number": (
            matched_track.get("number") or str(matched_track.get("position"))
            if matched_track
            else None
        ),
        "disc_number": (
            str(medium.get("position"))
            if matched_track and medium.get("position") is not None
            else None
        ),
        "total_tracks": sum(
            item.get("track-count") or len(item.get("tracks", [])) for item in media
        )
        if media
        else None,
        "label": ", ".join(label_names) or None,
        "musicbrainz_recording_id": candidate.get("recording_id"),
        "musicbrainz_release_id": candidate.get("release_id"),
        "source_url": request.get("source_url"),
        "musicbrainz_url": candidate.get("musicbrainz_url"),
    }
    return {
        "fields": metadata,
        "sources": {
            "musicbrainz": candidate.get("musicbrainz_url"),
            "source_url": "user_input" if request.get("source_url") else None,
            "composer": "musicbrainz" if composers else "user_input" if request.get("composer") else None,
            "lyricist": "musicbrainz" if lyricists else None,
            "language": "musicbrainz" if work_language else "user_input" if request.get("language") else None,
            "contributor_credits": credit_status,
            "release_details": "musicbrainz" if release else None,
            "user_input": {
                key: request.get(key)
                for key in ("movie_name", "composer", "language", "featured_artists")
                if request.get(key)
            },
        },
    }
