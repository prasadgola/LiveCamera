# LiveCamera Agent

FastAPI backend with Gemini Live. Watches a camera feed autonomously — when it detects a subject (bird, food, car, etc.), it automatically generates and sends a mobile HTML UI to the Android client.

## Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `GET /health` | HTTP | Health check |
| `WS /livecamera` | WebSocket | Live camera + audio + autonomous UI generation |

## Wire Format

Binary frames over WebSocket:

```
[4 bytes LE = type] [4 bytes LE = payload length] [payload bytes]

type 1 = JPEG video frame
type 2 = PCM-16 audio chunk (16 kHz mono)
```

## WebSocket Messages (server → client)

```json
{ "type": "ui_generating", "subject": "a bird", "message": "I see a bird. Generating UI…" }
{ "type": "ui_generated",  "html": "<!DOCTYPE html>…", "subject": "a bird", "ui_theme": "bird field guide app" }
{ "type": "turn_complete" }
{ "type": "transcript",    "text": "I can see a parrot on a branch." }
{ "type": "error",         "message": "…" }
```

## Local Dev

```bash
export GEMINI_API_KEY="your-key"
pip install -r requirements.txt
uvicorn main:app --reload --port 8080
```

## Deploy to Cloud Run (from scratch)

1. Install [gcloud CLI](https://cloud.google.com/sdk/docs/install) and run `gcloud auth login`
2. Get your billing account ID:
   ```bash
   gcloud billing accounts list
   ```
3. Edit `deploy.sh` — fill in `PROJECT_ID`, `BILLING_ACCOUNT`, and `GEMINI_API_KEY`
4. Run:
   ```bash
   chmod +x deploy.sh && ./deploy.sh
   ```

## Deploy via GitHub (Cloud Build trigger)

1. Push this repo to GitHub
2. In GCP Console → Cloud Build → Triggers → **Connect Repository**
3. Select this repo, branch `main`, config file `cloudbuild.yaml`
4. Make sure Cloud Build service account has these roles:
   - `Cloud Run Admin`
   - `Storage Admin`
   - `Service Account User`
5. Push any commit to `main` → Cloud Build auto-deploys

## Changing the Detection Behavior

Edit `LIVE_SYSTEM_PROMPT` in `main.py` to change what Gemini watches for and when it triggers UI generation.
