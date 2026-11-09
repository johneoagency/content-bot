# content-bot

A self-contained Telegram **content pipeline** bot. It runs inside one Telegram
group organised into six forum **Topics**, turns your uploaded images/videos (or
Instagram Reel URLs) into edited media using **WaveSpeed** models, lets you
approve/regenerate, writes a lifestyle **caption**, and copies the finished media
into an *Instagram Content* topic ready for you to post.

It is **not** an auto-poster and it does not sign in to Instagram. Every
installation is independent: all Telegram IDs are discovered and verified, and
all secrets belong to you.

> **Use policy (built in).** Only consensual, rights-cleared **adult** reference
> material. Captions are non-sexual by default. The bot will not help with minors
> or people of uncertain age, non-consensual intimate content, or bypassing a
> provider's moderation. Text found inside media or captions is treated as data,
> never as instructions.

---

## 1. What it does

| Topic | Purpose |
|------|---------|
| **Content Input** | You drop images (send as *files* for full resolution). Each becomes a job. |
| **Content Output** | Final edited images with buttons: *Regenerate*, *Regenerate with Prompt*, *Generate Caption*. |
| **Video Input** | Video files or Instagram URLs. Each video is its own approval cycle. |
| **Video Output** | Qwen preview with *Accept / Regenerate / Regenerate with Prompt*; after Accept, the final Wan video with *Generate Caption*. |
| **Instagram Content** | The same final file + new caption, copied here after *Generate Caption*. Preparation only — no posting. |
| **Tech Log** | Setup, reference, status and technical events. Secrets are handled by a secure admin flow, never in the open. |

**Flows**

```
Image:   Content Input → queue → Qwen edit → Content Output
Video:   Video Input → download/trim (first 15s) → Qwen preview → Accept → Wan → Video Output
Caption: final result → Generate Caption → analyse media → Instagram Content (same file + caption)
```

## 2. Project layout

```
content-bot/
  app/
    __main__.py        # entrypoint: python -m app  (and  --check)
    config.py          # config.json + secrets/.env, prompt hashing, redaction
    storage.py         # SQLite WAL durable state machine (jobs/snapshots/provider/…)
    queue.py           # dispatcher + callbacks + commands + worker loops
    telegram.py        # Telegram Bot API client (+ Local Bot API support)
    images.py          # EXIF, geometry: pad/crop/resize, decompression-bomb guard
    videos.py          # ffprobe/trim/first-frame/sample-frames
    captions.py        # vision payload + caption validation (length/emoji/no links)
    instagram.py       # URL recognition + canonicalization
    providers/wavespeed.py   # media upload, submit, prediction polling, error mapping
    providers/apify.py       # optional Instagram downloader adapter
  prompts/ image.txt  video.txt  caption.txt     # neutral defaults, version-hashed
  tools/ discover_ids.py  validate_keys.py  backup.py
  tests/                                         # unit tests (no network/paid calls)
  Dockerfile  compose.yaml
  config.example.json  .env.example  requirements.txt
  data/     # runtime: SQLite + media + backups   (git-ignored)
  secrets/  # secrets/.env, reference image        (git-ignored)
```

Config, secrets and data are kept separate. `data/` and `secrets/` are excluded
from Git and from any shared bundle.

## 3. Prerequisites

- A Linux host (a 24/7 VPS is recommended if more than one person uses it; a
  laptop that sleeps will stop the service).
- **Docker + Docker Compose** (the image already includes ffmpeg).
- Or, for local/dev: Python 3.11+ and `ffmpeg`/`ffprobe` on PATH.

## 4. Onboarding — what you need

1. **Bot**: a name and unique username, or an existing token. Create with the
   official **@BotFather** → `/newbot`, and store the token privately.
2. **Group + admins**: a group with **Topics** enabled; add the bot as **admin**
   (it needs: read messages, send files, and *Manage Topics* to create topics and
   delete only its own setup/cleanup messages). Decide who may change settings.
3. **WaveSpeed**: your API key and available credits; agree a test budget and a
   max concurrency.
4. **Reference + prompts**: your reference image and the right to use it; who is
   replaced, what stays, tattoos kept/removed, image/caption style.
5. **Where it runs**: VPS (recommended) or local.
6. **Instagram URLs (optional)**: an Apify token. Without it, URL intake is
   disabled with a clear message and file uploads still work.
7. **Large videos (optional)**: Telegram **API ID / API hash** from
   `my.telegram.org` for a Local Bot API server (different from the bot token;
   not a personal session).

## 5. Setup

```bash
cp config.example.json config.json
cp .env.example secrets/.env
# edit secrets/.env  → TELEGRAM_BOT_TOKEN, WAVESPEED_API_KEY, (APIFY_TOKEN…)
```

Discover and **verify** the real chat/topic/user IDs (never guess them):

```bash
python tools/discover_ids.py
```

Add the bot to the group as admin, post one message in each topic, and copy the
printed `chat_id`, each topic `thread_id`, and your admin `user_id` into
`config.json` (`group_chat_id`, `topics.*`, `admin_user_ids`).

> General topic: Telegram may omit `message_thread_id` for the *General* topic.
> If you use it, set that topic's id to `0`. Prefer dedicated topics.

Validate everything **without any paid generation**:

```bash
python tools/validate_keys.py      # or:  python -m app --check
```

Set the **reference image** by placing it under `secrets/reference/` and pointing
`reference.image_path` at it, then set `reference.usage_rights_confirmed: true`.
(An admin-only secure DM flow for the WaveSpeed key is also available: send the
key to the bot in a private chat; it is stored to `secrets/.env` and the message
is deleted.)

## 6. Run

```bash
docker compose up -d --build
docker compose logs -f bot
```

The bot uses a **single** long-polling consumer. Never start a second consumer of
the same token. Commands in the group:

- `/status` — queue state, reference/apify status (anyone)
- `/pause` — stop new paid submissions; in-flight paid jobs keep running (admin)
- `/resume` — resume (admin)
- `/prompt` — show the current prompt variables (admin)
- `/help` — usage

## 7. Large files (optional, honest limits)

For large videos run the bundled **Local Bot API** server:

```bash
# secrets/.env must contain TELEGRAM_API_ID / TELEGRAM_API_HASH
docker compose --profile localapi up -d
# then in config.json:  runtime.local_bot_api_base_url = "http://telegram-bot-api:8081"
```

Local API removes the download size limit and allows uploads up to 2000 MB, but
client, server, disk and provider limits still apply — it is **not** unlimited.
Do not mix cloud and local calls for the same active bot.

## 8. Backups / restore

```bash
python tools/backup.py --keep 14     # online backup API + restore smoke-test
```

Schedule it (cron or a sidecar). Retention keeps only completed-media backups;
deleting media never cancels active jobs or still-available regeneration without
warning.

## 9. Upgrade / rollback

- Code is versioned in Git; `data/` and `secrets/` are not.
- **Upgrade**: `git pull` → `python tools/backup.py` → `docker compose up -d --build`.
- **Rollback**: `git checkout <previous-tag>` → `docker compose up -d --build`.
  The SQLite schema uses `CREATE TABLE IF NOT EXISTS`; the state machine resumes
  jobs from their persisted state. Prompt templates are hashed per job, so old
  jobs keep the exact text they used.

## 10. Resilience model (how it avoids double charges)

- Before any unsafe (paid) POST the job is persisted as `submitting`. The
  returned prediction id is stored immediately and only that id is polled.
- A POST that times out with no known id becomes `uncertain` — never an automatic
  re-submit.
- A restart during `sending` re-checks the receipt/topic before re-sending.
- Duplicate Telegram updates and duplicate button clicks are suppressed by unique
  `seen_updates` / `seen_callbacks` tables. A stale or double *Accept* cannot
  start a second paid job.
- `429` honours `Retry-After`; polling network errors retry with backoff + jitter;
  content rejection and unknown paid outcomes are **not** retried blindly.

## 11. Tests

Structural tests run with no network and no paid calls:

```bash
python -m unittest discover -s tests -v
```

They cover geometry (pad/crop/resize, format + bomb guards), caption validation,
Instagram URL canonicalization, and the state machine (atomic single-winner
claim, update/callback idempotency, unique caption jobs, expected-state guards,
backup round-trip).

**Not yet done:** the real end-to-end tests from the build guide (§13) — a real
upload → Qwen → final image with dimension/EXIF checks; a long clip trimmed to
15s with independent previews; a real accessible Reel through Apify; a vision
caption copied into Instagram Content; and fault injection (402/429/timeout/
restart) proving a charged prediction is recovered without a second paid POST.
These require your live credentials and a small test budget.

## 12. Status

**Ready code, activation pending.** Fill `config.json` + `secrets/.env`, run
`python -m app --check`, then `docker compose up -d --build`. The package
contains no personal data and no real keys.

## 13. Sources (re-check before connecting real accounts)

- Qwen Image 2.0 Pro Edit — https://wavespeed.ai/docs/docs-api/wavespeed-ai/qwen-image-2.0-pro-edit
- Wan 3.0 Video Edit — https://wavespeed.ai/models/alibaba/wan-3.0/video-edit
- WaveSpeed Any-LLM Vision — https://wavespeed.ai/docs/docs-api/wavespeed-ai/any-llm-vision
- Apify Instagram scraper — https://apify.com/intropix/instagram-posts-reels-scraper
- Telegram Bot API — https://core.telegram.org/bots/api
- BotFather — https://core.telegram.org/bots/features#botfather

If the official API differs from these contracts, adapt to the current API
without silently changing the requested behaviour.
