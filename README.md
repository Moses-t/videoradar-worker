# Transcript Worker

External worker that polls the Lovable app for transcript jobs, downloads audio with `yt-dlp`, transcribes via OpenAI Whisper, and posts results back.

## Endpoints called

- `POST /api/public/transcript-jobs/claim`
- `POST /api/public/transcript-jobs/:id/complete`
- `POST /api/public/transcript-jobs/:id/fail`

## Environment variables

| Var | Required | Description |
|-----|----------|-------------|
| `APP_BASE_URL` | yes | Base URL of the app, e.g. `https://your-app.lovable.app` |
| `OPENAI_API_KEY` | yes | OpenAI key for Whisper |
| `API_SECRET` | no | Shared secret sent as `x-api-secret` header |
| `POLL_INTERVAL` | no | Seconds between polls (default `10`) |
| `WHISPER_MODEL` | no | Default `whisper-1` |
| `YOUTUBE_COOKIES` | no | Full Netscape cookies.txt contents (see below) |

## YouTube cookies (Railway)

YouTube increasingly blocks unauthenticated downloads. Provide a cookies file via env var:

1. In your browser (logged into YouTube), export cookies using an extension like **Get cookies.txt LOCALLY** (Netscape format).
2. Open the exported file and copy the **entire contents** (including the `# Netscape HTTP Cookie File` header line).
3. In Railway → your service → **Variables** → **New Variable**:
   - Name: `YOUTUBE_COOKIES`
   - Value: paste the entire cookies.txt contents
4. Redeploy.

The worker writes the value to a temporary `cookies.txt` per job, passes it to yt-dlp via `--cookies`, and deletes it when the job ends. Cookies are never stored permanently and never logged.

## Diagnostic logging

On startup and per job, the worker logs:

- Whether `YOUTUBE_COOKIES` is set and its length (not contents)
- Whether `cookies.txt` was written, plus path and byte size
- The full `yt-dlp` command line (cookie path only, never values)
- On failure: full yt-dlp `stderr` and `stdout`

Check Railway logs to confirm cookies are being applied.

## Local run

```bash
cp .env.example .env
# fill in values
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)
python worker.py
```

## Deploy to Railway

1. Push this folder to a GitHub repo.
2. Railway → New Project → Deploy from GitHub → select repo.
3. Railway auto-detects the `Dockerfile`.
4. Add the env vars above under **Variables**.
5. Deploy.
