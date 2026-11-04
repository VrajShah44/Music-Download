# Smart Music Collector

An automated macOS backend and CLI for downloading songs with full metadata, synchronized/plain lyrics, high-resolution cover art, and audio (M4A/MP3), complete with automatic Apple Music library integration.

It identifies recordings via MusicBrainz, fetches matching lyrics from LRCLIB, retrieves verified release artwork from Cover Art Archive (with iTunes and video thumbnail fallbacks), downloads the audio track via `yt-dlp` using ffmpeg, tags the audio file with mutagen (embedding credits, album art, and lyrics), and queues it into Apple Music's `Automatically Add to Music` folder.

## Requirements and install

- macOS and Python 3.11+
- `ffmpeg` (installed via `brew install ffmpeg`)
- Internet access for MusicBrainz, LRCLIB, Cover Art Archive, and yt-dlp

```sh
cd "/Users/vrajshah/Desktop/Music Download"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Running the Server

```sh
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Visit `http://127.0.0.1:8000/docs` for the interactive API and `GET /health` for a health check.

## Command-Line Usage (CLI)

You can download songs directly from the terminal with a single command:

```sh
# Download by song title and artist:
.venv/bin/python -m app.cli "Jaiye Sajana" --artist "Jasmine Sandlas"

# Download with a direct URL (YouTube, SoundCloud, etc.):
.venv/bin/python -m app.cli "Song Title" --artist "Artist" --url "https://www.youtube.com/watch?v=..."
```

What gets downloaded into `~/Music/Smart Music Collector/<Language>/<Album>/<Track - Artist>/`:
1. **Audio**: `.m4a` audio file tagged with embedded metadata, lyrics, and cover image.
2. **Metadata**: `metadata.json` containing detailed MusicBrainz track, release, and artist relations.
3. **Lyrics**: Synced (`.lrc`) and plain (`.txt`) lyrics from LRCLIB.
4. **Artwork**: High-res `cover.jpg` from Cover Art Archive / Apple iTunes / thumbnail fallback.
5. **Apple Music**: Automatically queued to macOS Music.app for instant library addition.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `SMC_DATA_DIR` | `./data` | SQLite job database location |
| `SMC_LIBRARY_DIR` | `~/Music/Smart Music Collector` | Output library root |
| `SMC_AUTO_ADD_DIR` | Music.app’s detected “Automatically Add to Music” folder | Folder watched by Music.app for imports |
| `SMC_CONTACT` | `http://127.0.0.1:8000/docs` | Contact/project URL in provider User-Agent headers |

## API Endpoints

### 1. Process Song
`POST /api/v1/songs/process` accepts:

```json
{
  "song_title": "Jaiye Sajana",
  "artist": "Jasmine Sandlas",
  "featured_artists": ["Satinder Sartaaj"],
  "album_name": null,
  "movie_name": "Dhurandhar: The Revenge",
  "composer": null,
  "language": null,
  "source_url": null,
  "download_audio": true
}
```

The endpoint responds with HTTP 202 and a `job_id`.

### 2. Poll Job Status
`GET /api/v1/jobs/{job_id}` returns job details, status (`pending`, `processing`, `completed`, `partially_completed`, `failed`), and download results.

### 3. Ambiguous Candidate Selection
If candidates are ambiguous, job status will be `partially_completed` with `result.status: "needs_selection"`. Select a candidate recording ID:

```http
POST /api/v1/jobs/{job_id}/select
Content-Type: application/json

{"recording_id": "<MusicBrainz recording MBID>"}
```

### 4. Manual Audio Upload (Optional)
If you prefer to manually supply your own audio file instead of downloading:

```http
POST /api/v1/jobs/{job_id}/audio
Content-Type: multipart/form-data
```
- `confirm_recording=true`
- `audio_file`: MP3, FLAC, M4A, or WAV

## Tests

Run the full test suite with:

```sh
.venv/bin/pytest -v
```
