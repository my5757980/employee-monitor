# Tasks: console speed

## Server

- [x] T1 `db_close()`, so a waiting request holds no database connection
- [x] T2 Signal file helpers, one fast `device_rows()`, `report_data()`, `setting()`
- [x] T3 `/api/admin/boot`, and `login` returns the same data
- [x] T4 `/api/admin/device` gains all tab data
- [x] T5 `/api/admin/live-stream`
- [x] T6 Held `/api/agent/ping` with its guards
- [x] T7 Thumbnails: lower quality, progressive, written atomically, `immutable`
- [x] T8 Page route: `ETag`, `X-Shell`, `no-cache`
- [x] T9 Upload limits in `.htaccess`, clear error when a file is too large

## Dashboard

- [x] T10 `build_css.py`, compiled CSS, no CDN, inline favicon
- [x] T11 `sw.js` and registration
- [x] T12 Draw from last known state, one boot call, sign-out wipes it
- [x] T13 Panel opens at once, hover warms, tabs paint from one reply
- [x] T14 Big picture: small first, sharp after, no flicker. Scrub and Play load ahead
- [x] T15 Live: stream, fallback when nothing arrives, stop on close, button on the picture, status text
- [x] T16 Buttons answer at once, recent actions refresh themselves
- [x] T17 Notice: shrink picture and voice, list comes from memory
- [x] T18 Cameras: one click when saved, gear to change it

## Proof

- [x] T19 Local rig with real round-trip delay, seeded with made-up data
- [x] T20 API tests, including every hold guard (91 passed)
- [x] T21 UI tests on every option, timed (129 passed)
- [x] T22 The installed `.exe`, unchanged, against the new server (14 passed)
- [x] T23 Same look at five widths, zero sideways overflow (80 passed)
- [x] T24 Live deploy with byte checks, before and after numbers, test computers removed

## Measured on the live site, 4 Nov 2026

| Action | Before | After |
|---|---|---|
| Open the page again, computers shown | 1.42-1.54 s | 0.04-0.08 s |
| Sign in, computers shown | 1.50 s | 0.96 s |
| Click a computer, panel shown | 0.32-0.38 s | under 10 ms |
| Click a computer, sharp picture | 0.70-1.33 s | 0-80 ms |
| Report, Apps, Controls tab | 0.34, 0.34, 0.55 s | under 5 ms |
| Click an old screenshot | 0.39-0.79 s | under 10 ms, sharp 0.34-0.52 s |
| Move the playback bar | 0.56-0.82 s | under 15 ms |
| Button to computer | about 2 s (rig) | 0.37-0.78 s |
| Live, first frame taken after the click | 4.75-6.37 s | 2.1-4.3 s, from a laptop uploading at 22 KB/s |
| Live, age of a frame on arrival | 2.77-3.03 s | 1.87 s, same laptop |
| 80 KB after a pause, with BBR and no slow start after idle | 0.83 s | 0.38 s |

Two real office computers ran on the new server from the first minutes: sample gaps 60-62 s, held pings working, no cut pings, no PHP errors.

Frame rate did not change: it is set by the agent's loop, about 1.1 frames a second on the office computers.
