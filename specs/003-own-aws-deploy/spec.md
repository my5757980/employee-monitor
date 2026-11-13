# Spec: the owner's own copy on his AWS server

> This is the record of one pass of work (5 Nov 2026), kept as it was written.
> Remote control was added to the product afterwards, as a visible, announced
> feature, so "no remote control" under Not in scope describes this pass only.
> The README has the present state.

**Asked (5 Nov 2026):** the tool runs on Sir's 1Panel server, the owner has no copy of his own. Deploy it on his AWS, where the hackathon demo runs.

## Must

- The whole product works at a public HTTPS address: console, agent API, live view, notices, reports.
- Its own database and pictures, starting empty. Nothing is copied from Sir's server, and Sir's server is not touched.
- The hackathon demo on the same server keeps running while this is deployed and while this is used. Judging is under way.
- This app cannot take the demo's memory, CPU or disk beyond set limits.
- No new AWS cost.
- The real agent `.exe` works against it without being rebuilt.

## Not in scope

- Moving any office computer from Sir's server. That is a reinstall on each computer, the owner's step.
- Any change to the product's code or to its boundaries: the notice stays, no stealth, no keylogger, no remote control.
- The real-AnyDesk button (paused separately).

## Done when

- [x] Sign-in, every API route, live view, notices at their largest allowed size, and cleanup work on the deployed server
- [x] The real `.exe` enrols over the real certificate, reports, takes actions, shows live
- [x] The console works in a real browser, including phone width and the microphone
- [x] 30 computers at once with no errors, and the demo unaffected
- [x] Test data wiped, one enrollment key made, sign-in kept out of the repo and notes
