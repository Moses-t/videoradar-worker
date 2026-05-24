"""
Transcript worker.

Polls the Lovable backend for pending transcription jobs, downloads audio
with yt-dlp, converts/normalizes with ffmpeg, transcribes with OpenAI
Whisper, and posts segments back to the callback endpoint.

All requests to the Lovable backend are signed with HMAC-SHA256 using
TRANSCRIPT_WEBHOOK_SECRET, matching the server-side verification in
src/routes/api/public/transcript-callback.ts and transcript-jobs.claim.ts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any

import requests
from openai import OpenAI

# ---------- Config ----------

BASE_URL = os.environ["BASE_URL"].rstrip("/")
WEBHOOK_SECRET = os.environ["TRANSCRIPT_WEBHOOK_SECRET"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
MAX_JOBS_PER_POLL = int(os.environ.get("MAX_JOBS_PER_POLL", "1"))
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-1")
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "60"))
# Whisper hard limit is 25MB; downsample target keeps us well under.
AUDIO_SAMPLE_RATE = os.environ.get("AUDIO_SAMPLE_RATE", "16000")
AUDIO_BITRATE = os.environ.get("AUDIO_BITRATE", "64k")

CLAIM_URL = f"{BASE_URL}/api/public/transcript-jobs/claim"
CALLBACK_URL = f"{BASE_URL}/api/public/transcript-callback"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("worker")

client = OpenAI(api_key=OPENAI_API_KEY)

_shutdown = False


def _handle_signal(signum, _frame):
    global _shutdown
    log.info("received signal %s, shutting down after current job", signum)
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ---------- HMAC helpers ----------

def _sign(body: str) -> str:
    return hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _post_signed(url: str, payload: dict[str, Any]) -> requests.Response:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": _sign(body),
    }
    return requests.post(url, data=body, headers=headers, timeout=REQUEST_TIMEOUT)


# ---------- Job pipeline ----------

def claim_jobs() -> list[dict[str, Any]]:
    res = _post_signed(CLAIM_URL, {"max": MAX_JOBS_PER_POLL})
    res.raise_for_status()
    return res.json().get("jobs", [])


def download_audio(video_url: str, workdir: str) -> str:
    """Download bestaudio with yt-dlp into workdir; return file path."""
    out_template = os.path.join(workdir, "src.%(ext)s")
    cmd = [
        "yt-dlp",
        "-f", "bestaudio/best",
        "--no-playlist",
        "--no-warnings",
        "-o", out_template,
        video_url,
    ]
    log.info("yt-dlp downloading %s", video_url)
    subprocess.run(cmd, check=True, capture_output=True)
    for name in os.listdir(workdir):
        if name.startswith("src."):
            return os.path.join(workdir, name)
    raise RuntimeError("yt-dlp produced no output file")


def convert_audio(src_path: str, workdir: str) -> str:
    """Convert to mono 16kHz mp3 for Whisper."""
    out_path = os.path.join(workdir, "audio.mp3")
    cmd = [
        "ffmpeg", "-y", "-i", src_path,
        "-vn",
        "-ac", "1",
        "-ar", AUDIO_SAMPLE_RATE,
        "-b:a", AUDIO_BITRATE,
        out_path,
    ]
    log.info("ffmpeg converting to mp3")
    subprocess.run(cmd, check=True, capture_output=True)
    return out_path


def transcribe(audio_path: str) -> list[dict[str, Any]]:
    """Call Whisper with verbose_json to get segments."""
    log.info("whisper transcribing %s", audio_path)
    with open(audio_path, "rb") as f:
        result = client.audio.transcriptions.create(
            model=WHISPER_MODEL,
            file=f,
            response_format="verbose_json",
        )
    segments_raw = getattr(result, "segments", None) or []
    segments: list[dict[str, Any]] = []
    for s in segments_raw:
        text = (getattr(s, "text", "") or "").strip()
        if not text:
            continue
        segments.append({
            "text": text[:5000],
            "start_time": float(getattr(s, "start", 0.0)),
            "end_time": float(getattr(s, "end", 0.0)),
        })
    if not segments:
        # Fallback: single segment with full text.
        full = (getattr(result, "text", "") or "").strip()
        if full:
            segments.append({"text": full[:5000], "start_time": 0.0, "end_time": 0.0})
    return segments


def send_callback(video_id: str, status: str, *,
                  segments: list[dict[str, Any]] | None = None,
                  error: str | None = None) -> None:
    payload: dict[str, Any] = {"video_id": video_id, "status": status}
    if segments is not None:
        payload["segments"] = segments
    if error is not None:
        payload["error"] = error[:2000]
    res = _post_signed(CALLBACK_URL, payload)
    if res.status_code >= 300:
        log.error("callback %s failed: %s %s", status, res.status_code, res.text)
        res.raise_for_status()
    log.info("callback %s ok for %s", status, video_id)


def process_job(job: dict[str, Any]) -> None:
    video_id = job["video_id"]
    video_url = job["video_url"]
    log.info("processing job video_id=%s url=%s", video_id, video_url)
    workdir = tempfile.mkdtemp(prefix="transcript-")
    try:
        src = download_audio(video_url, workdir)
        audio = convert_audio(src, workdir)
        segments = transcribe(audio)
        if not segments:
            send_callback(video_id, "failed", error="No transcript segments produced")
            return
        send_callback(video_id, "ready", segments=segments)
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", "ignore")[-1500:]
        log.exception("subprocess failed")
        try:
            send_callback(video_id, "failed", error=f"{e.cmd[0]} failed: {stderr}")
        except Exception:
            log.exception("failed to report failure")
    except Exception as e:
        log.exception("job failed")
        try:
            send_callback(video_id, "failed", error=str(e))
        except Exception:
            log.exception("failed to report failure")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    log.info("worker starting; base=%s poll=%ss", BASE_URL, POLL_INTERVAL)
    while not _shutdown:
        try:
            jobs = claim_jobs()
        except Exception:
            log.exception("claim failed")
            jobs = []

        if not jobs:
            for _ in range(POLL_INTERVAL):
                if _shutdown:
                    break
                time.sleep(1)
            continue

        for job in jobs:
            if _shutdown:
                break
            process_job(job)

    log.info("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
