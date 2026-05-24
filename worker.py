#!/usr/bin/env python3
"""
Transcript worker: polls the app for jobs, downloads audio via yt-dlp,
transcribes with OpenAI Whisper, and reports results back.
"""
import os
import sys
import time
import json
import logging
import tempfile
import subprocess
from pathlib import Path

import requests

# --- Config ---
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")
API_SECRET = os.environ.get("API_SECRET", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
YOUTUBE_COOKIES = os.environ.get("YOUTUBE_COOKIES", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-1")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("worker")

if not APP_BASE_URL:
    log.error("APP_BASE_URL is required")
    sys.exit(1)
if not OPENAI_API_KEY:
    log.error("OPENAI_API_KEY is required")
    sys.exit(1)


def _auth_headers():
    h = {"Content-Type": "application/json"}
    if API_SECRET:
        h["x-api-secret"] = API_SECRET
    return h


def _write_cookies_file(tmpdir: str):
    """Write YOUTUBE_COOKIES env var to a temp cookies.txt file. Returns path or None."""
    if "YOUTUBE_COOKIES" not in os.environ:
        log.info("YOUTUBE_COOKIES: not set")
        return None

    raw = os.environ.get("YOUTUBE_COOKIES", "")
    log.info("YOUTUBE_COOKIES: set, length=%d", len(raw))

    if not raw.strip():
        log.warning("YOUTUBE_COOKIES is empty/whitespace; skipping cookies file")
        return None

    path = os.path.join(tmpdir, "cookies.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
        size = os.path.getsize(path)
        log.info("cookies.txt written: path=%s bytes=%d", path, size)
        return path
    except Exception as e:
        log.error("Failed to write cookies.txt: %s", e)
        return None


def claim_job():
    url = f"{APP_BASE_URL}/api/public/transcript-jobs/claim"
    try:
        r = requests.post(url, headers=_auth_headers(), json={}, timeout=30)
        if r.status_code == 204 or not r.text:
            return None
        r.raise_for_status()
        data = r.json()
        return data.get("job") or data
    except Exception as e:
        log.error("claim_job failed: %s", e)
        return None


def complete_job(job_id: str, transcript: str):
    url = f"{APP_BASE_URL}/api/public/transcript-jobs/{job_id}/complete"
    r = requests.post(url, headers=_auth_headers(), json={"transcript": transcript}, timeout=60)
    r.raise_for_status()


def fail_job(job_id: str, error: str):
    url = f"{APP_BASE_URL}/api/public/transcript-jobs/{job_id}/fail"
    try:
        r = requests.post(url, headers=_auth_headers(), json={"error": error[:2000]}, timeout=30)
        r.raise_for_status()
    except Exception as e:
        log.error("fail_job request failed: %s", e)


def download_audio(video_url: str, tmpdir: str) -> str:
    out_template = os.path.join(tmpdir, "audio.%(ext)s")
    cookies_path = _write_cookies_file(tmpdir)

    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format", "mp3",
        "--no-playlist",
        "-o", out_template,
    ]
    if cookies_path:
        cmd += ["--cookies", cookies_path]
    cmd.append(video_url)

    # Log command (path only, never cookie contents)
    log.info("yt-dlp command: %s", " ".join(cmd))

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        log.error("yt-dlp failed (exit=%d)", proc.returncode)
        log.error("yt-dlp stderr:\n%s", proc.stderr)
        log.error("yt-dlp stdout:\n%s", proc.stdout)
        raise RuntimeError(f"yt-dlp failed: {proc.stderr.strip()[:500]}")

    # Find the produced file
    for p in Path(tmpdir).iterdir():
        if p.name.startswith("audio."):
            return str(p)
    raise RuntimeError("yt-dlp produced no output file")


def transcribe(audio_path: str) -> str:
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    with open(audio_path, "rb") as f:
        files = {"file": (os.path.basename(audio_path), f, "audio/mpeg")}
        data = {"model": WHISPER_MODEL, "response_format": "text"}
        r = requests.post(url, headers=headers, files=files, data=data, timeout=600)
    if r.status_code != 200:
        raise RuntimeError(f"Whisper API failed: {r.status_code} {r.text[:500]}")
    return r.text.strip()


def process_job(job: dict):
    job_id = job.get("id") or job.get("job_id")
    video_url = job.get("video_url") or job.get("url")
    if not job_id or not video_url:
        log.error("Invalid job payload: %s", job)
        return

    log.info("Processing job %s: %s", job_id, video_url)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = download_audio(video_url, tmpdir)
            log.info("Downloaded audio: %s", audio_path)
            transcript = transcribe(audio_path)
            log.info("Transcribed %d chars", len(transcript))
            complete_job(job_id, transcript)
            log.info("Job %s completed", job_id)
    except Exception as e:
        log.exception("Job %s failed", job_id)
        fail_job(job_id, str(e))


def main():
    log.info("Worker starting. APP_BASE_URL=%s poll=%ss", APP_BASE_URL, POLL_INTERVAL)
    log.info("YOUTUBE_COOKIES present: %s", "yes" if os.environ.get("YOUTUBE_COOKIES") else "no")
    while True:
        try:
            job = claim_job()
            if job:
                process_job(job)
            else:
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            log.info("Shutting down")
            break
        except Exception as e:
            log.exception("Main loop error: %s", e)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
