import asyncio
import json
import logging
import os
import shutil
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from mutagen import MutagenError
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints, model_validator

from app.services import apple_music, musicbrainz, processor
from app.services.library import safe_component
from app.services.tagging import tag_audio


logger = logging.getLogger(__name__)
DATA_DIR = Path(
    os.environ.get("SMC_DATA_DIR", Path(__file__).resolve().parent.parent / "data")
)
DATABASE_PATH = DATA_DIR / "jobs.db"
MAX_AUDIO_BYTES = 100 * 1024 * 1024
NonEmptyText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
]
OptionalText = Annotated[str | None, Field(max_length=500)]
FeaturedArtist = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


class SongRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    song_title: NonEmptyText
    artist: OptionalText = None
    featured_artists: list[FeaturedArtist] = Field(default_factory=list, max_length=20)
    album_name: OptionalText = None
    movie_name: OptionalText = None
    composer: OptionalText = None
    language: OptionalText = None
    source_url: HttpUrl | None = None
    download_audio: bool = True

    @model_validator(mode="before")
    @classmethod
    def normalize_missing_values(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = value.copy()
        for field in (
            "artist",
            "album_name",
            "movie_name",
            "composer",
            "language",
            "source_url",
        ):
            if normalized.get(field) is False:
                normalized[field] = None
        return normalized


class JobAccepted(BaseModel):
    job_id: str
    status: Literal[
        "pending", "processing", "completed", "partially_completed", "failed"
    ]
    song_title: str
    message: str


class RecordingSelection(BaseModel):
    recording_id: NonEmptyText


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _initialize_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                request_json TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def _update_job(job_id: str, status: str, result: dict | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE jobs SET status = ?, result_json = COALESCE(?, result_json), "
            "updated_at = ? WHERE job_id = ?",
            (status, json.dumps(result) if result is not None else None, now, job_id),
        )
        if cursor.rowcount == 0:
            raise LookupError(f"Job {job_id} does not exist")


def _read_job(job_id: str) -> dict | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT job_id, status, request_json, result_json, created_at, updated_at "
            "FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "job_id": row["job_id"],
        "status": row["status"],
        "song": json.loads(row["request_json"]),
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _process_job(job_id: str, request: dict, selected_recording_id: str | None = None) -> None:
    _update_job(job_id, "processing")
    try:
        download_audio = request.get("download_audio", True)
        try:
            result = processor.process_song(
                request, selected_recording_id, download_audio=download_audio
            )
        except TypeError:
            result = processor.process_song(request, selected_recording_id)
        job_status = (
            "partially_completed"
            if result["status"] == "needs_selection"
            else result["status"]
        )
        _update_job(job_id, job_status, result)
    except Exception as error:
        logger.exception("Song processing failed for job %s", job_id)
        result = {
            "status": "failed",
            "message": "Song processing failed. See the backend log for details.",
            "error": str(error),
        }
        _update_job(job_id, "failed", result)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _initialize_database()
    yield


app = FastAPI(
    title="Smart Music Collector",
    version="0.3.0",
    description="Local music discovery, asset organization, audio tagging, and Apple Music import API.",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/songs/identify")
def identify_song(song: SongRequest) -> dict:
    return musicbrainz.find_recordings(song.model_dump(mode="json"))


@app.post("/api/v1/songs/process", response_model=JobAccepted, status_code=202)
def process_song(song: SongRequest, background_tasks: BackgroundTasks) -> JobAccepted:
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        connection.execute(
            "INSERT INTO jobs (job_id, status, request_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (job_id, "pending", song.model_dump_json(), now, now),
        )
    background_tasks.add_task(_process_job, job_id, song.model_dump(mode="json"))
    return JobAccepted(
        job_id=job_id,
        status="pending",
        song_title=song.song_title,
        message="Request accepted; check the job endpoint for its result.",
    )


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = _read_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/v1/jobs/{job_id}/select", status_code=202)
def select_recording(
    job_id: str, selection: RecordingSelection, background_tasks: BackgroundTasks
) -> dict[str, str]:
    job = _read_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    candidates = (job.get("result") or {}).get("candidates", [])
    if not any(item.get("recording_id") == selection.recording_id for item in candidates):
        raise HTTPException(status_code=422, detail="Recording ID is not a candidate for this job")
    _update_job(job_id, "pending")
    background_tasks.add_task(
        _process_job, job_id, job["song"], selection.recording_id
    )
    return {"job_id": job_id, "status": "pending", "message": "Selection accepted"}


@app.post("/api/v1/jobs/{job_id}/audio", status_code=201)
async def add_authorized_audio(
    job_id: str,
    confirm_recording: bool = Form(...),
    audio_file: UploadFile = File(...),
) -> dict:
    job = _read_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    result = job.get("result") or {}
    output_directory = result.get("output_directory")
    if not output_directory or not result.get("metadata", {}).get("fields"):
        raise HTTPException(
            status_code=409,
            detail="Identify and process the recording before uploading audio",
        )
    if not confirm_recording:
        raise HTTPException(
            status_code=422,
            detail="Confirm that the user-provided file is the selected recording",
        )

    extension = Path(audio_file.filename or "").suffix.lower()
    if extension not in {".mp3", ".flac", ".m4a", ".wav"}:
        raise HTTPException(status_code=415, detail="Supported audio formats: MP3, FLAC, M4A, WAV")

    library_root = processor.LIBRARY_DIR.resolve()
    destination = Path(output_directory).resolve()
    try:
        destination.relative_to(library_root)
    except ValueError as error:
        raise HTTPException(status_code=409, detail="Job output path is outside the configured library") from error
    destination.mkdir(parents=True, exist_ok=True)

    temporary_path = destination / f".incoming-{uuid.uuid4().hex}{extension}"
    total = 0
    cover_path = next(
        (path for path in destination.glob("cover.*") if path.is_file()), None
    )
    try:
        with temporary_path.open("wb") as target:
            while chunk := await audio_file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_AUDIO_BYTES:
                    raise HTTPException(status_code=413, detail="Audio file exceeds the 100 MiB limit")
                target.write(chunk)
        await asyncio.to_thread(
            tag_audio,
            temporary_path,
            result["metadata"]["fields"],
            cover_path,
        )
        title = safe_component(result["metadata"]["fields"].get("song_title"), "track")
        final_path = destination / f"{title}{extension}"
        suffix = 2
        while final_path.exists():
            final_path = destination / f"{title} ({suffix}){extension}"
            suffix += 1
        shutil.move(str(temporary_path), final_path)
    except HTTPException:
        temporary_path.unlink(missing_ok=True)
        raise
    except (MutagenError, OSError, ValueError) as error:
        temporary_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=f"Audio could not be tagged: {error}") from error
    finally:
        await audio_file.close()

    result["audio"] = {
        "status": "success",
        "file": str(final_path),
        "source": "user_provided",
        "verification": "user_confirmed",
    }
    try:
        result["apple_music"] = await asyncio.to_thread(
            apple_music.queue_track,
            final_path,
            job_id,
            result["metadata"]["fields"],
            cover_path,
        )
    except (OSError, ValueError) as error:
        logger.exception("Apple Music queueing failed for job %s", job_id)
        result["apple_music"] = {"status": "error", "message": str(error)}
    done = (
        result.get("metadata", {}).get("status") == "success"
        and result.get("artwork", {}).get("status") == "success"
        and result.get("apple_music", {}).get("status") == "already_in_library"
    )
    result["status"] = "completed" if done else "partially_completed"
    result["message"] = (
        "Authorized audio is tagged and already present in the Apple Music library."
        if done
        else "Authorized audio was tagged and saved locally. Apple Music import is queued or needs attention."
    )
    _update_job(job_id, result["status"], result)
    return {
        "job_id": job_id,
        **result["audio"],
        "apple_music": result["apple_music"],
        "status": result["status"],
    }
