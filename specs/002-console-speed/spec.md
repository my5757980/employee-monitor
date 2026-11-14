# Spec: console speed

> This is the record of one pass of work (4 Nov 2026), kept as it was written.
> Since then: remote control was added as a visible, announced feature, so "No
> remote control" under Constraints describes this pass only; live frames are
> shown but only one in ten seconds is kept; and a one‑process server (PHP's
> built‑in one) no longer holds pings or streams. The README has the present
> state.

## Why

The owner watches office computers from far away. Measured on the live site (4 Nov 2026):

- The server answers in 3-5 ms. Ten thumbnails in parallel take 18 ms on the server itself.
- The road between the owner and the server takes 300-400 ms per round trip, and every new connection takes about 1 s.
- So every wait the owner sees is network distance multiplied by how many round trips a click makes.

Faster code on the server cannot help. The screen must stop waiting for the network, and each action must make fewer, smaller trips.

## Measured before

| Action | Before |
|---|---|
| Open the page on a new connection, pictures shown | 8.9 s |
| Reload the page, computers shown | 2.0-2.2 s |
| Click a computer, panel shown | 0.36-0.52 s |
| Click a computer, picture shown | up to 1.8 s |
| Report tab | 0.36-0.55 s |
| Apps tab | 0.45-0.62 s |
| Controls tab | 0.78-1.95 s |
| Click an old screenshot | 1.5-2.7 s |
| Move the playback bar | 0.5-1.1 s |
| Live view, press to first frame | about 4-5 s, estimated from the agent's loop |

## Requirements

- FR-1: Opening the page again paints the computers from the last known state with no network wait, then updates.
- FR-2: The page has no third-party script. Its styles are compiled ahead of time and inlined.
- FR-3: Page start makes one data request, not three.
- FR-4: A click on a computer opens the panel at once with the name and a picture, then fills in.
- FR-5: All four tabs fill from one reply, so moving between tabs makes no request.
- FR-6: A click on an old screenshot shows its small copy at once and sharpens when the large copy is ready.
- FR-7: Moving the playback bar never queues picture downloads. Play loads ahead and never shows half a frame.
- FR-8: Every button answers on screen at once. The result follows, and a failure is shown.
- FR-9: Recent actions update by themselves until the computer has answered.
- FR-10: Live frames reach the browser as they arrive, with no polling, and fall back to polling if a network holds the stream back.
- FR-11: A new action reaches a waiting computer at once, with no change or reinstall on the computer.
- FR-12: Closing live view stops the computer sending frames.
- FR-13: Thumbnails and large copies are smaller on the wire.
- FR-14: Cameras: a saved address opens with one click.
- FR-15: Notices: pictures and voice clips are made small before they are sent. A file too large to accept gives a clear error.

## Constraints

- No change to what the agent does and no reinstall on any computer.
- The dashboard stays one file and the server stays plain PHP and MySQL, with no new service.
- Nothing is hidden from the employee. The monitoring notice and disclosure rules are unchanged.
- No remote control. No camera restreaming.
- Existing API replies only gain fields, so a saved copy of the page keeps working for one version.

## What waits on the agent

A held ping must never hurt monitoring:

- It never holds while frames are flowing (live or record).
- It never holds on a computer with blocked applications, because the block is enforced on each ping.
- It always ends before the next activity sample is due, so a network that cuts held pings cannot stop samples.
- An action handed to a computer that dropped off is offered again on its next ping, for 3 minutes, except Restart and Shut down. Those are never run twice: the owner sees they never said done.
- If a network cuts a held ping, that computer goes back to quick pings for an hour.
- `EM_PING_HOLD=0` turns the hold off completely.

## Acceptance

- [ ] FR-1 to FR-15 pass on a local rig that adds the real round-trip delay.
- [ ] The installed agent `.exe`, unchanged, works against the new server: commands arrive, samples stay on time, live frames flow.
- [ ] The page looks the same as before at 320, 768, 1024, 1440 and 2560 px, apart from the Live button on the picture, the Cameras gear and the solid header background.
- [ ] Live site: before and after numbers measured the same way.
