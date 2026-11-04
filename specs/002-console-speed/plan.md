# Plan: console speed

## Cause

Server work is about 1% of each wait. The rest is round trips (300-400 ms each) and TCP starting slowly on a long road. The plan therefore counts round trips and bytes, not milliseconds of PHP.

## Decisions

### 1. Service worker for the page only

`sw.js` handles one thing: the navigation to `/`. It answers from a saved copy and fetches the newest copy behind it. API calls and pictures never go through its cache.

- Rejected: a long HTTP cache on the HTML, because a deploy could then stay invisible for as long as the cache lasts.
- Reversible: a `sw.js` that unregisters itself removes it. `/index.html` always bypasses it.

### 2. Last known state in localStorage

The computers list is drawn from it before any network call and is wiped on sign-out and on any 401.

### 3. Compiled CSS inlined in the page

`server/tools/build_css.py` runs the Tailwind CLI at the same version the CDN served, and writes the result between markers in `dashboard.html`. Removes a 130 KB script, a browser-side compile and a third-party dependency that office proxies can block.

### 4. One reply per panel

`/api/admin/device` gains `report`, `procs`, `policy`, `commands`, `ago` and `online`. Old routes stay.

### 5. Live frames as a stream

`/api/admin/live-stream` keeps one response open and writes each frame as `[u32 meta][u32 image][meta json][jpeg]`. A browser `fetch` reader parses it.

- Rejected: `multipart/x-mixed-replace`, because it gives no frame id, so no timestamp and no resume after a reconnect.
- Rejected: long polling, because each frame would still cost one round trip.
- A watchdog switches to the old poll if `/api/admin/live` sees a newer frame than the stream delivered, which means something on the path is buffering.

### 6. Signal files, not database polling

One small file per computer and purpose in `data_dir/signal`:

| File | Written when | Read by |
|---|---|---|
| `<id>.cmd` | an action is queued | held ping |
| `<id>.frame` | a frame is stored | live stream |
| `<id>.live`, `<id>.rec` | live or record is handed over | ping hold check |
| `<id>.sample` | an activity sample arrives | ping hold check |
| `<id>.hold` | a ping starts waiting | ping hold check |
| `<id>.nohold` | a held ping was cut short | ping hold check |

Waiting requests hold no database connection.

### 7. Held ping instead of an agent change

The agent's ping has a 30 s timeout and already blocks its loop, so the server can answer late with no change on the computer. The guards are listed in the spec. The agent is not rebuilt in this pass.

### 8. No WebP

GD here has no WebP. Adding it means a container recreate for about 25% off images that are already cached. Lower JPEG quality and progressive encoding give most of it with no downtime.

### 9. Host TCP settings: separate, owner's call

`tcp_slow_start_after_idle=0`, BBR and a larger initial window help every site on that server, and only the owner can agree to that. Kept apart from the code deploy and measured on its own.

## Risks

1. A saved page goes stale after a deploy. The service worker compares the `X-Shell` header and tells the page to reload once.
2. A held ping meets an office proxy that cuts idle requests. The `nohold` guard limits this to one lost beat per hour, and `EM_PING_HOLD=0` turns it off.
3. The stream is buffered by an inspecting proxy. The watchdog falls back to polling and the stream is labelled `text/event-stream` with `no-transform`.

## Rollback

Restore the previous `index.php`, `http.php`, `db.php`, `config.php` and `dashboard.html` from the `.bak-speed` copies, and replace `sw.js` with the self-unregistering version. No table changes other than the `settings` table the Cameras feature already needs.
