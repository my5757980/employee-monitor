# Net Monitor for Employees Pro — every option, and where our custom stands

Taken from the installed product's own manual (`doc/help.qch`). This is the full
list of what that software does, mapped against our custom tool.

Legend: ✅ built · ◑ partly · ➖ not yet (can add) · ❌ left out on purpose

## 1. Add & manage computers
| Net Monitor option | Our custom |
|---|---|
| Add a computer (host/IP, nickname, agent password, TCP port 4495) | ✅ “Add a computer” → an install key (works over the internet, no VPN) |
| Connect automatically on start | ✅ agent auto-starts at logon |
| Choose which monitor to show | ➖ (primary screen for now) |
| Scan the LAN for agents / add a group of LAN computers | ➖ (we enroll by key instead; internet-ready) |
| Organise computers into groups | ➖ can add |
| RDP / Terminal Server multi-session | ➖ later |

## 2. See the screens (monitoring)
| | |
|---|---|
| All screens as thumbnails | ✅ live grid |
| Thumbnail size / refresh / aspect options | ◑ fixed sizes |

## 3. Tools & restrictions
| | |
|---|---|
| Turn off / restart remote computers | ✅ shutdown / restart / log off |
| Lock computer | ✅ |
| Send a message / question | ✅ message |
| Open a web page on the remote computer | ➖ (left for now — “run something on the PC” is sensitive) |
| Restrictions auto-applied at logon | ◑ blocks apply continuously |

## 4. Applications & processes
| | |
|---|---|
| See running applications | ➖ can add (live list) |
| Block an application (auto-stopped if started) | ✅ blocked apps are killed |
| See running processes and kill one | ➖ can add |

## 5. Desktop recorder
| | |
|---|---|
| Record screens (JPG) | ✅ record action + stored frames |
| Play recordings | ✅ playback scrubber |
| Record to MPEG4 video | ➖ (we store JPG frames; MP4 later) |
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
| Web browsing history | ◑ active window captured; true URL needs a browser add-on later |
| Application usage logging | ✅ today’s report (minutes per app) |
| Reports: top sites, details | ◑ today’s report; date filter/export ➖ can add |
| **Keylogger (keystrokes per app)** | ❌ **left out on purpose** (privacy/legal) |
| Filter by computer / user / time; export to file | ➖ can add (CSV + date range) |

## 10. Remote control
| Take over the remote mouse/keyboard | ✅ built, **transparent**: it works only while Live view is on, and the employee sees an on‑screen banner (“Your administrator is viewing and controlling this computer”) the whole time. Never silent. |

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
1. Live **running apps / processes** list, with “stop” per computer.
2. Report **date range + CSV export**.
3. **Groups** / rename computers; remove a computer.
4. Thumbnail **refresh/size** settings.
