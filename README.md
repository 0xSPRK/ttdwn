# ttdwn

A strict, minimal TikTok downloader for the terminal. No watermark, real
quality selection, no dependencies.

```
ttdwn 1.0.0  ·  no watermark · high quality

──────────────────────────────────────────────────────────────────────────────
7106594312292453675  @tiktok  TikTok
how many frogs did you find? 🐸 check out tiktok's #Minecraft community to…
00:24 · 2022-06-07 20:15 UTC · 592.6K plays · 98.9K likes · via web+mobile

  #   quality  resolution    bitrate  codec      size
  ─────────────────────────────────────────────────────
  →1  1080p    1080×1920   1094 kbps  h265     3.2 MB
   2  720p     720×1280     641 kbps  h265     1.9 MB
   3  540p     576×1024    1071 kbps  h264     3.1 MB
   4  540p     576×1024     556 kbps  h265     1.6 MB
   5  540p     576×1024     548 kbps  h264     1.6 MB

  selected: 1080p · 1080×1920 · 1094 kbps · h265 · 3.2 MB
  5 rendition(s) without watermark
```

---

## Why the quality is actually high

Most "TikTok downloaders" grab `video.download_addr` — the copy the app saves
to your camera roll, which is **720×720, watermarked and re-encoded**. ttdwn
never picks that stream unless you ask for it (`--watermark`). It reads the
rendition ladders instead:

| Source | How it is reached | What it gives |
| --- | --- | --- |
| Mobile feed API | `/aweme/v1/feed/?aweme_id=…` with an Android app User-Agent (Pixel 6a profile by default) | `video.bit_rate[]` — 540p/720p, **watermark-free**, no signature needed |
| Web video page | `/@author/video/<id>` + the `__UNIVERSAL_DATA_FOR_REHYDRATION__` JSON island | `video.bitrateInfo[]` — the 1080p gears, **watermark-free** |
| Embed player | `/embed/v2/<id>` | one extra stream, and the author handle when you only have an id |

All three are queried and merged, so a post typically shows both ladders at
once. Everything runs through a single cookie-jar session, because the CDN
links the web page hands out are signed for the session that requested them —
open them from a cold jar and Akamai answers `403 Access Denied`.

Watermark-free is not a post-processing trick here: nothing is cropped, blurred
or re-encoded. The file you get is the file TikTok serves.

---

## Install

Nothing to install — Python 3.9+, standard library only.

```sh
git clone <this repo>  &&  cd ttdwn
./ttdwn.py --version
```

Put it on your `PATH`:

```sh
pipx install .          # or: pip install --user .
ttdwn --version
```

Optional extras: `ffmpeg` on `PATH` enables `--audio`, `--remux` and codec
probing. Everything else works without it.

---

## Use

```sh
ttdwn https://www.tiktok.com/@user/video/7106594312292453675
ttdwn https://vm.tiktok.com/ZMabc123/            # short links are followed
ttdwn 7106594312292453675                        # bare id works too
ttdwn -q 720 <url>                               # cap it at 720p
ttdwn --list <url>                               # see the ladder, download nothing
ttdwn --info <url>                               # metadata only
ttdwn -i links.txt --sleep 2 -o ~/Videos         # batch, politely
```

### Quality

`--quality` accepts:

| Value | Meaning |
| --- | --- |
| `best` *(default)* | highest resolution available without a watermark |
| `worst` | smallest file |
| `1080`, `1080p`, `720`, `480` … | exact height, otherwise the closest lower one, with a note |
| `2`, `#2` | the row with that number from `--list` |

Resolution names use the short side of the frame, the way TikTok does: a
1080×1920 vertical clip is *1080p*, a 576×1024 clip is *540p*.

At equal resolution ttdwn prefers H.264 (plays everywhere) over H.265, then the
higher bitrate. Force either with `--codec h264` / `--codec h265`, and never go
above a ceiling with `--max-quality 1080`.

### Output

```
-o DIR             destination directory (default: .)
-f TPL             filename template (default: {author}_{id}_{quality})
--out-file PATH    exact path, single link only
--force            overwrite
--no-resume        ignore .part files
--max-size 200MB   refuse huge files
```

Template tokens:

```
{id} {author} {nickname} {desc} {quality} {resolution} {width} {height}
{bitrate} {gear} {codec} {date} {duration} {index} {ext} {source}
```

```sh
ttdwn -f '{date}-{author}-{id}-{resolution}' <url>
ttdwn -f '{desc}' -o ~/Videos <url>
```

The final path is printed to stdout; everything human-readable goes to stderr,
so this works:

```sh
ttdwn --quiet <url> | xargs mpv
```

### Sidecars

```sh
--thumbnail     save the cover as .jpg
--metadata      save the full post metadata as .json
--music         save the original sound
--audio mp3     extract audio with ffmpeg (mp3, m4a, opus, wav)
--audio-only    keep only the audio
--remux         rewrite the MP4 with +faststart (instant playback)
--images / --no-images    download / skip photo carousels
```

### Network knobs

```sh
--ua android|iphone|chrome|'<literal UA>'   User-Agent for API calls
--device pixel6a|pixel7|s23|oneplus         Android profile the API sees
--no-mobile / --no-web                      use one source only
--proxy http://127.0.0.1:8080               or socks5://…
--cookies cookies.txt                       Netscape format, for restricted posts
--timeout 20  --retries 3  --sleep 0         pacing
```

### Automation

```sh
$ ttdwn --json <url> | jq -r '.saved'
$ ttdwn --info --json <url> | jq '.media.formats[].quality'
$ ttdwn -i - --quiet -o ./out < links.txt     # read links from stdin
$ ttdwn --check                               # endpoint diagnostics
```

`--json` prints one object per post on stdout:

```json
{
  "ok": true,
  "input": "https://vm.tiktok.com/ZMabc123/",
  "saved": "/home/me/tiktok_7106594312292453675_1080p.mp4",
  "size": 3360445, "seconds": 1.4,
  "quality": "1080p", "resolution": "1080×1920",
  "bitrate": 1094429, "codec": "h265", "watermark": false,
  "extras": [],
  "media": { "id": "…", "author": {…}, "formats": [ … ], "…": "…" }
}
```

---

## How it works

```
input ──▶ resolve.py ──▶ aweme id + author      (short links followed, ≤6 hops)
                │
                ├─▶ extract.py ─┬─ mobile API   → bit_rate[]        (540p/720p)
                │               ├─ web page     → bitrateInfo[]     (1080p)
                │               └─ embed        → 1 stream + author
                │        merge + dedupe by frame/codec/size
                │
                ▼
           formats.py ──▶ pick one rendition    (height, codec, watermark rules)
                │
                ▼
          download.py ──▶ <name>.part ──▶ verify ──▶ <name>.mp4
                          resume with Range, retry mirrors,
                          re-extract on 403 (expired signature)
```

* **Atomic writes.** Bytes land in `.part`; the rename happens only after the
  declared length is satisfied, so a killed process never leaves a broken
  `.mp4`.
* **Resume.** An interrupted transfer continues with `Range:` from the last
  byte. If the server ignores ranges, the file is refetched cleanly.
* **Expired links heal.** Each rendition carries several CDN mirrors, tried in
  order. If every mirror answers `403`, ttdwn re-extracts the post to get fresh
  signatures and continues the same transfer.
* **Deduplication.** `play_addr_h264` is literally `bit_rate[normal_540_0]`, and
  the mobile and web ladders overlap. Streams are matched by frame size, codec
  and byte count, so the table shows renditions rather than aliases.

---

## Limits, honestly

* **Rate limits.** Anonymous requests get throttled after a burst; that is what
  `--sleep`, `--proxy` and `--retries` are for. `--check` tells you which
  endpoint is unhappy.
* **1080p is not always there.** Some posts only exist at 540p/720p; some 1080p
  gears only open up for logged-in sessions — pass `--cookies` exported from a
  browser you are logged into.
* **Private, deleted, region-locked, age-restricted or live posts** cannot be
  downloaded anonymously. Each produces a clear error and a distinct exit code.
* **Photo carousels** are downloaded as numbered JPEGs into a folder.
* **No watermark output may still be re-encoded by TikTok.** A creator who
  uploaded 480p has no 1080p version; ttdwn reports what actually exists.
* Personal use only. Respect creators, copyright and TikTok's terms.

---

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success |
| 1 | one or more links failed in a batch |
| 2 | bad usage (quality, template, size, missing arguments) |
| 3 | the link is not a usable TikTok reference |
| 4 | no media could be extracted (private/deleted/region-locked/live) |
| 5 | download failed after retries |
| 6 | ffmpeg is required but missing, or refused the job |

## Troubleshooting

| Symptom | Try |
| --- | --- |
| `HTTP 403 … signed link expired` | usually transient; ttdwn re-extracts automatically. Otherwise `--retries 5` |
| `429` from an API host | `--sleep 3 --device s23`, or another `--proxy` |
| only 540p/720p listed | the post has no higher stream, or add `--cookies` |
| `no media` for something that plays in a browser | age/region gated → `--cookies` |
| H.265 file will not open in an editor | `--codec h264 --quality 720` |
| everything fails | `ttdwn --check` |

---

## Development

```sh
python -m pytest            # 61 tests, all offline (fake sessions + local HTTP server)
python -m ttdwn --check     # live endpoint diagnostics
```

Layout:

```
ttdwn/
  cli.py         arguments, orchestration, rendering
  resolve.py     input → aweme id (short-link following)
  extract.py     the three sources, normalisation, merging
  formats.py     quality selection
  download.py    resumable atomic transfers
  ffmpeg.py      optional remux / audio extraction
  http.py        urllib session: cookies, proxy, retries, streaming
  models.py      Format / Media
  ui.py          monochrome terminal output, progress bar
tests/           offline suite with payload fixtures
```

MIT licensed.
