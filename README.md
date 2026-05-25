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
| `YOUTUBE_COOKIES` | no | — | Netscape-format cookies for yt-dlp (see below). |
| `POLL_INTERVAL_SECONDS` | no | `30` | Sleep between empty polls. |
| `MAX_JOBS_PER_POLL` | no | `1` | 1–5 (server-capped at 5). |
| `WHISPER_MODEL` | no | `whisper-1` | |
| `REQUEST_TIMEOUT_SECONDS` | no | `60` | HTTP timeout to Lovable backend. |
| `AUDIO_SAMPLE_RATE` | no | `16000` | ffmpeg `-ar`. |
| `AUDIO_BITRATE` | no | `64k` | ffmpeg `-b:a`. |
| `LOG_LEVEL` | no | `INFO` | |

## YouTube cookies (optional)

If YouTube blocks or rate-limits downloads (403/429 errors), set the
`YOUTUBE_COOKIES` environment variable with a Netscape-format cookie string.
The worker writes it to a temporary file, passes it to yt-dlp via
`--cookies`, and deletes the file when the job finishes.

**How to get cookies:**

1. Install the **Get cookies.txt LOCALLY** browser extension (Chrome / Firefox).
2. Log in to YouTube in your browser.
3. Click the extension → **Export** → choose **Netscape format**.
4. Copy the entire contents of the exported file.
5. Paste it into the `YOUTUBE_COOKIES` Railway variable (see below).

**Setting on Railway:**

1. Open your Railway project → **Variables**.
2. Click **New Variable**.
3. Name: `YOUTUBE_COOKIES`
4. Value: paste the full Netscape cookie file contents (including `# Netscape HTTP Cookie File` header).
5. **Add** → **Deploy** to restart the worker with the new variable.

**Security note:** The cookies contain your YouTube session. Do not commit
them to GitHub. Use Railway Variables (or any other secret manager) only.

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
    yt-dlp -f bestaudio [--cookies cookies.txt] -> src.<ext>
    ffmpeg -ac 1 -ar 16k -> audio.mp3
    whisper verbose_json -> [{text, start_time, end_time}, ...]
    POST /api/public/transcript-callback {video_id, status: "ready", segments}
  if no jobs: sleep POLL_INTERVAL_SECONDS
```

On any failure the worker reports `status: "failed"` with an `error`
string so the video doesn't stay stuck in `processing`.
