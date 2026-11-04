import argparse
import json
import logging
import sys
from typing import Any

from app.services import processor


logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("smart-music-collector")


def download_cli(
    song_title: str | None,
    artist: str | None = None,
    album: str | None = None,
    url: str | None = None,
    auto_select: bool = True,
) -> int:
    # If song_title is a URL, shift it to url
    if song_title and (song_title.startswith("http://") or song_title.startswith("https://")):
        if not url:
            url = song_title
        song_title = None

    # Treat generic placeholders as None when url is provided
    if url:
        if song_title and song_title.lower() in {"song title", "unknown track", "unknown"}:
            song_title = None
        if artist and artist.lower() in {"artist", "unknown artist", "unknown"}:
            artist = None

    if not song_title and not url:
        print("Error: You must provide either a song title or a source URL (--url).", file=sys.stderr)
        return 1

    title = song_title or ""
    request: dict[str, Any] = {
        "song_title": title,
        "artist": artist,
        "album_name": album,
        "source_url": url,
        "download_audio": True,
    }

    if url and not title:
        print(f"🎵 Fetching track information from URL: {url}...")
    else:
        print(f"🎵 Searching and downloading '{title}' by '{artist or 'Unknown'}'...")
    result = processor.process_song(request, download_audio=True, auto_select=auto_select)

    if result.get("status") == "needs_selection":
        candidates = result.get("candidates", [])
        if auto_select and candidates:
            chosen = candidates[0]
            print(f"ℹ️ Multiple candidates found. Auto-selecting top match: {chosen.get('title')} - {chosen.get('artist')} ({chosen.get('recording_id')})")
            result = processor.process_song(request, selected_recording_id=chosen.get("recording_id"), download_audio=True)
        else:
            print("⚠️ Ambiguous matches found. Plausible candidates:")
            for index, candidate in enumerate(candidates, start=1):
                print(f"  [{index}] {candidate.get('title')} - {candidate.get('artist')} (ID: {candidate.get('recording_id')}, Album: {candidate.get('release_name')})")
            print("Run again with --recording-id <ID> to proceed.")
            return 2

    print("\n" + "=" * 50)
    print(f"Status:   {result.get('status')}")
    print(f"Message:  {result.get('message')}")
    print(f"Output:   {result.get('output_directory')}")
    
    meta = result.get("metadata", {})
    if meta.get("fields"):
        print(f"Title:    {meta['fields'].get('song_title')}")
        print(f"Artist:   {meta['fields'].get('artist')}")
        print(f"Album:    {meta['fields'].get('album_name')}")

    lyrics = result.get("lyrics", {})
    print(f"Lyrics:   {lyrics.get('status')} ({lyrics.get('provider', 'N/A')})")
    if lyrics.get("files"):
        for f in lyrics["files"]:
            print(f"          ↳ {f}")

    art = result.get("artwork", {})
    print(f"Artwork:  {art.get('status')} ({art.get('provider', 'N/A')})")
    if art.get("file"):
        print(f"          ↳ {art['file']}")

    audio = result.get("audio", {})
    print(f"Audio:    {audio.get('status')} ({audio.get('source', 'N/A')})")
    if audio.get("file"):
        print(f"          ↳ {audio['file']}")

    apple = result.get("apple_music", {})
    if apple:
        print(f"Apple Music: {apple.get('status')} ({apple.get('message', '')})")
    print("=" * 50)

    return 0 if result.get("status") in {"completed", "success"} else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Smart Music Collector: Download songs with metadata, lyrics, cover art, and audio."
    )
    parser.add_argument("title", nargs="?", default=None, help="Song title to search and download")
    parser.add_argument("-a", "--artist", default=None, help="Artist name")
    parser.add_argument("-b", "--album", default=None, help="Album or movie name")
    parser.add_argument("-u", "--url", default=None, help="Direct source URL (YouTube, SoundCloud, etc.)")
    parser.add_argument(
        "--no-auto-select",
        action="store_true",
        help="Do not auto-select the first candidate when multiple matches exist",
    )

    args = parser.parse_args()
    code = download_cli(
        song_title=args.title,
        artist=args.artist,
        album=args.album,
        url=args.url,
        auto_select=not args.no_auto_select,
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
