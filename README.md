# Transcript Worker

External Python worker for the Lovable transcription pipeline.

Polls the Lovable backend for pending transcription jobs, downloads audio
with `yt-dlp`, converts it with `ffmpeg`, transcribes with OpenAI Whisper,
and posts segments back to the callback endpoint. All requests are signed
with HMAC-SHA256 using a shared secret.

## Endpoints used

- `POST {BASE_URL}/api/public/transcript-jobs/claim` — atomically claim pending jobs.
- `POST {BASE_URL}/api/public/transcript-callback` — report `ready` (with segments) or `failed`.

Both require header `X-Webhook-Signature: hex(hmac_sha256(TRANSCRIPT_WEBHOOK_SECRET, raw_body))`.

## Environment variables

| Name | Required | Default | Notes |
|------|----------|---------|-------|
| `BASE_URL` | yes | — | e.g. `https://project--<id>.lovable.app` |
| `TRANSCRIPT_WEBHOOK_SECRET` | yes | — | Must match the value set in Lovable Cloud. |
| `OPENAI_API_KEY` | yes | — | OpenAI key with Whisper access. |
| `POLL_INTERVAL_SECONDS` | no | `30` | Sleep between empty polls. |
| `MAX_JOBS_PER_POLL` | no | `1` | 1–5 (server-capped at 5). |
| `WHISPER_MODEL` | no | `whisper-1` | |
| `REQUEST_TIMEOUT_SECONDS` | no | `60` | HTTP timeout to Lovable backend. |
| `AUDIO_SAMPLE_RATE` | no | `16000` | ffmpeg `-ar`. |
| `AUDIO_BITRATE` | no | `64k` | ffmpeg `-b:a`. |
| `LOG_LEVEL` | no | `INFO` | |

## Local run

```bash
cp .env.example .env   # fill in real values
export $(grep -v '^#' .env | xargs)
pip install -r requirements.txt
# requires ffmpeg installed locally
python worker.py
```

## Deploy to Railway

1. Push this directory to a GitHub repo.
2. Create a new Railway project from the repo (Dockerfile is auto-detected).
3. Add the env vars above under **Variables**.
4. Deploy. The container runs `python worker.py` as a long-running worker
   (no HTTP port is exposed).

`railway.json` configures the Docker build and an automatic restart on
failure (up to 10 retries).

## Flow

```
loop:
  POST /api/public/transcript-jobs/claim {"max": N}
  for each job:
    yt-dlp -f bestaudio  -> src.<ext>
    ffmpeg -ac 1 -ar 16k -> audio.mp3
    whisper verbose_json -> [{text, start_time, end_time}, ...]
    POST /api/public/transcript-callback {video_id, status: "ready", segments}
  if no jobs: sleep POLL_INTERVAL_SECONDS
```

On any failure the worker reports `status: "failed"` with an `error`
string so the video doesn't stay stuck in `processing`.
