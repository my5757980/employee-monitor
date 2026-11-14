# Net Monitor for Employees Pro — every option, and where our custom stands

Taken from the installed product's own manual (`doc/help.qch`). This is the full
list of what that software does, mapped against our custom tool.

Legend: ✅ built · ◑ partly · ➖ not yet (can add) · ❌ left out on purpose

## 1. Add & manage computers
| Net Monitor option | Our custom |
|---|---|
| Add a computer (host/IP, nickname, agent password, TCP port 4495) | ✅ “Add a computer” → an install key (works over the internet, no VPN) |
| Connect automatically on start | ✅ agent auto-starts at logon, for every user of the computer, and starts itself again if it ever stops |
| Choose which monitor to show | ➖ (primary screen for now) |
| Scan the LAN for agents / add a group of LAN computers | ➖ (we enroll by key instead; internet-ready) |
| Organise computers into groups | ✅ a group label per computer (Rename) |
| RDP / Terminal Server multi-session | ➖ later |

## 2. See the screens (monitoring)
| | |
|---|---|
| All screens as thumbnails | ✅ live grid |
| Thumbnail size / refresh / aspect options | ◑ fixed sizes |

## 3. Tools & restrictions
| | |
|---|---|
| Turn off / restart remote computers | ✅ shutdown / restart |
| Lock computer | ✅ |
| Send a message / question | ✅ notice: text, picture and voice, to one / several / all; a computer that is off gets it when next on |
| Open a web page on the remote computer | ➖ (left for now — “run something on the PC” is sensitive) |
| Restrictions auto-applied at logon | ◑ blocks apply continuously |

## 4. Applications & processes
| | |
|---|---|
| See running applications | ✅ live list while watching (Apps tab) |
| Block an application (auto-stopped if started) | ✅ blocked apps are killed |
| See running processes and kill one | ✅ “Stop” on the Apps tab closes it and keeps it blocked |

## 5. Desktop recorder
| | |
|---|---|
| Record screens (JPG) | ✅ Record: about one picture a second for 1–30 minutes, saved as a recording |
| Play recordings | ✅ Recordings tab: play at 1×–8×, jump to any moment |
| Record to MPEG4 video | ◑ download as one video file (Motion‑JPEG AVI); MP4 later |
| Scheduled recording | ◑ record for N minutes on demand |
| Store on a hidden share | ➖ (we store on the server) |

## 6. Internet control
| | |
|---|---|
| Allow / block web sites | ✅ blocked sites (hosts file) |

## 7. Messaging
| Send message / question | ✅ |

## 8. Desktop sharing
| Share the admin’s screen to the employee; let them control it | ➖ niche, later |

## 9. Reporting & logging
| | |
|---|---|
| Web browsing history | ◑ the site is named from the browser’s window title (well‑known sites, and titles that contain an address); the exact URL needs a browser add‑on later |
| Application usage logging | ✅ a day’s report (minutes per app) |
| Reports: top sites, details | ✅ a day’s report of applications and sites, for any date, with CSV export |
| **Keylogger (keystrokes per app)** | ❌ **left out on purpose** (privacy/legal) |
| Filter by computer / user / time; export to file | ◑ per computer and per day, as CSV; a date range ➖ can add |

## 10. Remote control
| Take over the remote mouse/keyboard | ✅ built, **transparent**: it works only while Live view is on, the employee sees an on‑screen bar (“Your administrator is viewing and controlling this computer”) the whole time, and their own keyboard and mouse are held meanwhile. Clicks, dragging, the wheel, typing and shortcuts. It ends on Stop, when the live view or the owner’s page closes, and by itself if the owner’s page stops answering. If the bar cannot be shown, control is refused. Never silent. |

## 11. Setup / agent
| | |
|---|---|
| Console + Agent (two modules) | ✅ dashboard + agent |
| Manual / remote / MSI (Active Directory) install | ◑ .exe + install.bat per PC; MSI/AD ➖ later |
| **Stealth install (hidden, no icon, not in Add/Remove)** | ❌ **left out on purpose** — ours is transparent, shows a monitoring notice |

---

## What we deliberately do NOT copy
- **Keylogger** — recording keystrokes is a privacy/legal risk; left out.
- **Stealth / hidden agent** — ours is transparent (the employee sees a notice).
- **Silent/stealth remote control** — control is only ever done with a visible banner on the employee's screen, and only while Live view is on; it is never hidden.

## Our custom’s advantage over the original
- Works **over the internet from anywhere** (the original needs the admin on the
  LAN or a VPN). The owner opens a web dashboard; no software on the owner’s side.

## Safe options we can still add to get closer to parity
1. Choose **which monitor** to show (now: the primary screen).
2. A report over a **date range** (now: one day at a time, with CSV).
3. Thumbnail **refresh/size** settings.
4. The **exact web address** of a page, through a browser add‑on (now: the site, from the window title).
5. **MSI / Active Directory** install (now: `.exe` + `install.bat` per computer).
6. A recording as an **MP4** file (now: stored frames with playback).
