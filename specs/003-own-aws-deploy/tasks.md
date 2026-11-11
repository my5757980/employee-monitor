# Tasks: the owner's own copy on his AWS server

Files: `deploy/aws/` (git-ignored). Tests: `E:\cw\empmon-aws\` (made-up computers, wiped after).

## Server

- [x] T1 SSH rule set to his current IP
- [x] T2 Hostname on the demo's Caddy, certificate issued, demo and its http redirect checked after the reload
- [x] T3 `Dockerfile`, `vhost.conf`, `apache-limits.conf`, `php.ini`
- [x] T4 `up.sh`: secrets once, capped data disk, fstab, Docker ordering, two limited containers, migrate, nightly cleanup
- [x] T5 Exact code on the server, md5 equal to the local files
- [x] T6 Owner sign-in from a private file on the laptop, never printed

## Tests on the deployed server

- [x] T7 `deploy_check.py`: 98/98. Sign-in and cookie flags, closed ports, files not served, Pakistan time, thumbnails, held ping through the proxy (0.27 s), live stream through the proxy, every route, notices at 6 MB + 12 MB, read-only code, limits, cleanup, restart, the demo beside it
- [x] T8 `exe_check.py`: 14/14. The real `.exe` over the real certificate: action 0.7 s, first live frame 1.7-2.1 s, no errors in its log
- [x] T9 `ui_check.py`: 46/46. A real browser: sign-in, grid, panel, tabs, CSV, Live on the stream, Fullscreen, playback, controls, restrictions, notice with microphone and picture, rename, add, cameras, service worker, phone width, remove, sign out, no script errors, no 5xx
- [x] T10 `load_check.py`: 30 computers, 354 held pings, 79 uploads at three times the real picture size, 0 errors. Button to computer 0.3 s median. Demo 75 of 75 answered, slowest 22 ms. Memory free never under 993 MB

## Handover

- [x] T11 Test data wiped: database volume recreated, pictures folder emptied
- [x] T12 Real enrollment key in the private file, checked to exist once
- [x] T13 Demo's container never restarted, Caddy never restarted (reload only)
- [ ] T14 A real reboot, after the hackathon's judging, then check all four containers and the data disk
- [ ] T15 Owner signs in himself and installs on his first computer
