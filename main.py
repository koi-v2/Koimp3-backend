import asyncio
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import yt_dlp


APP_NAME = "KoiMP3 Backend"

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/tmp/koimp3"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MAX_DURATION = int(os.getenv("MAX_DURATION_SECONDS", "1800"))
JOB_TTL = int(os.getenv("JOB_TTL_SECONDS", "1800"))


app = FastAPI(
    title=APP_NAME,
    version="1.0.0"
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


jobs = {}


class ConvertRequest(BaseModel):
    url: str = Field(
        min_length=8,
        max_length=2048
    )

    format: str = Field(
        default="mp3"
    )

    quality: int = Field(
        default=192,
        ge=64,
        le=320
    )


def valid_http_url(url: str) -> bool:
    return bool(
        re.match(
            r"^https?://",
            url.strip(),
            re.I
        )
    )


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def cleanup_old_jobs():
    now = time.time()

    expired = []

    for job_id, job in jobs.items():
        created = job.get(
            "created_at",
            now
        )

        if now - created > JOB_TTL:
            expired.append(job_id)

    for job_id in expired:
        job = jobs.pop(
            job_id,
            None
        )

        if job:
            path = job.get("file")

            if path:
                try:
                    Path(path).unlink(
                        missing_ok=True
                    )
                except Exception:
                    pass


def find_output(job_id: str) -> Optional[Path]:
    for path in OUTPUT_DIR.glob(
        f"{job_id}.*"
    ):
        if path.is_file():
            return path

    return None


def convert_worker(
    job_id: str,
    url: str,
    quality: int
):
    job = jobs[job_id]

    job["status"] = "processing"

    try:

        if not ffmpeg_available():
            raise RuntimeError(
                "FFmpeg is not installed on the server."
            )

        template = str(
            OUTPUT_DIR /
            f"{job_id}.%(ext)s"
        )

        ydl_opts = {
            "format": "bestaudio/best",

            "outtmpl": template,

            "noplaylist": True,

            "quiet": True,

            "no_warnings": True,

            "restrictfilenames": True,

            "socket_timeout": 30,

            "retries": 2,

            "extractor_args": {
                "youtube": {
                    "player_client": [
                        "android",
                        "web"
                    ]
                }
            },

            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",

                    "preferredcodec": "mp3",

                    "preferredquality": str(
                        quality
                    ),
                }
            ],
        }

        with yt_dlp.YoutubeDL(
            ydl_opts
        ) as ydl:

            info = ydl.extract_info(
                url,
                download=False
            )

            duration = info.get(
                "duration"
            )

            if (
                duration
                and duration > MAX_DURATION
            ):
                raise RuntimeError(
                    "Media is longer than the allowed "
                    f"{MAX_DURATION} seconds."
                )

            title = (
                info.get("title")
                or "audio"
            )

            job["title"] = title

            job["duration"] = duration

            ydl.download(
                [url]
            )

        output = find_output(
            job_id
        )

        if not output:
            raise RuntimeError(
                "Conversion finished but MP3 output was not found."
            )

        job["file"] = str(
            output
        )

        job["filename"] = (
            f"{job_id}.mp3"
        )

        job["status"] = "completed"

        job["downloadUrl"] = (
            f"/api/download/{job_id}"
        )

    except Exception as exc:

        job["status"] = "failed"

        job["error"] = str(
            exc
        )[:1000]


@app.get("/")
def root():

    return {
        "name": APP_NAME,
        "status": "online",
        "version": "1.0.0"
    }


@app.get("/health")
def health():

    return {
        "ok": True,
        "ffmpeg": ffmpeg_available(),
        "jobs": len(jobs)
    }


@app.post("/api/convert")
async def convert(
    request: ConvertRequest
):

    cleanup_old_jobs()

    if request.format.lower() != "mp3":

        raise HTTPException(
            status_code=400,
            detail="Only MP3 format is supported."
        )

    url = request.url.strip()

    if not valid_http_url(url):

        raise HTTPException(
            status_code=400,
            detail="Invalid URL."
        )

    job_id = uuid.uuid4().hex

    jobs[job_id] = {

        "id": job_id,

        "status": "queued",

        "created_at": time.time(),

        "title": None,

        "duration": None,

        "file": None,

        "filename": None,

        "downloadUrl": None,

        "error": None
    }

    asyncio.create_task(
        asyncio.to_thread(
            convert_worker,
            job_id,
            url,
            request.quality
        )
    )

    return {

        "jobId": job_id,

        "status": "queued",

        "downloadUrl":
            f"/api/download/{job_id}"
    }


@app.get(
    "/api/convert/{job_id}"
)
def get_job(
    job_id: str
):

    cleanup_old_jobs()

    job = jobs.get(
        job_id
    )

    if not job:

        raise HTTPException(
            status_code=404,
            detail="Job not found."
        )

    return {

        "jobId": job["id"],

        "status": job["status"],

        "title": job["title"],

        "duration": job["duration
