# Component: Media Handling

**Purpose**: Incoming media (photos, audio, video, documents) is cached permanently,
auto-described by Gemini Flash, and made searchable by content.

**Key files**: `bot/storage/media.py`, `bot/storage/media_cache.py`, `integrations/gemini.py`

---

## Design Principles

- **Permanent cache** — `media-cache` Docker volume, 99-year retention (36135 days), never auto-purged
- **AI descriptions for everything** — Gemini Flash describes all media asynchronously; descriptions FTS5-indexed
- **Voice transcription is SYNCHRONOUS** — transcribed BEFORE agent processing; transcript reused as media description (no duplicate Gemini call)
- **Two storage locations**: `data/tmp/` (ephemeral — lost on restart) vs `data/media_cache/` (permanent)
- **TG re-fetch** — `tg_file_id` stored; missing files re-downloaded from Telegram if session is active

## Supported Types (for AI description)

Images (jpg/png/gif/webp/heic), PDFs, video (mp4/mov/webm), audio/voice (ogg/m4a/mp3),
text/CSV, office docs (docx/xlsx)

## Auto-Description Flow

```
Incoming media
  → download to data/tmp/
  → copy to data/media_cache/ with stable filename
  → background Gemini Flash description (semaphore: max 3 concurrent)
    → description saved to media_cache.description
    → FTS5 UPDATE trigger auto-indexes it → content-searchable immediately
```

Startup backfill: `_backfill_media_descriptions()` processes media with empty descriptions
on every container restart (cap: `MEDIA_DESCRIPTION_BACKFILL_LIMIT`, default 20).

## Voice Transcription Flow

```
Voice message arrives
  → download to data/tmp/
  → SYNCHRONOUS Gemini Flash transcription (before agent processes the message)
  → transcript injected into agent prompt: Voice transcript: "..."
  → transcript saved as media_cache.description (no second Gemini call)
```

## Config Values

| Var | Default | Purpose |
|-----|---------|---------|
| MEDIA_RETENTION_DAYS | 36135 | 99-year retention |
| MEDIA_DESCRIBE_MAX_SIZE_MB | 100 | Skip AI description for files larger than this |
| MEDIA_DESCRIBE_INLINE_SIZE_MB | 20 | Files above this use Gemini File API (not inline) |
| MEDIA_DESCRIBE_TIMEOUT_S | 120 | Per-description call timeout |
| MEDIA_DESCRIBE_MAX_CONCURRENT | 3 | Semaphore cap for parallel Gemini description calls |
| VOICE_TRANSCRIBE_TIMEOUT_S | 60 | Voice transcription timeout |
| VOICE_TRANSCRIBE_MAX_CHARS | 20000 | Transcript length cap |

## Anti-Patterns

- **NEVER** use the Read tool on files >500KB — causes SDK 1MB JSON buffer crash (size guard catches browser MCP responses, but the Read tool does NOT have this protection)
- **NEVER** reference `data/tmp/` paths for anything persistent — ephemeral, lost on container restart
- **NEVER** send WA media from tmp/ path — use `search_media` to find the permanent `media_cache/` path
- **NEVER** assume AI description is available immediately — generation is async; may arrive seconds to minutes later

## Pitfalls

- **tmp/ vs media_cache/** — the `[PHOTO attached: /path]` tag in incoming messages points to the tmp/ download
  location. The permanent copy is in `media_cache/`. To find it: use `search_media` or check the
  `media_cache_id` foreign key in the messages table.
- **TG re-fetch requires active TG session** — if the bot is offline or the session is expired,
  re-fetch fails silently. The `media_cache/` volume is the safety net against this.
- **Large files skip description** — files above `MEDIA_DESCRIBE_MAX_SIZE_MB` have no AI description.
  They can still be found by filename/sender/date/caption via FTS5.
- **search_media auto-re-fetches** — if a file is missing from disk but `tg_file_id` exists,
  `search_media` triggers a re-download automatically. Status icons: ✅ available, 🔄 re-fetched, ❌ unavailable.
