# Plan: the owner's own copy on his AWS server

## Decisions

**Same server as the demo, not a second one.** He asked for it there, and a second t3.micro would take about $12 a month from the same $100 credit. Measured first: about 1 GB of memory free, 12 GB of disk free. Reversible: the app's files can move to any other server.

**Same port 443 with its own name, not a new port.** Opening port 8443 to the whole internet was refused by the safety check, and this is the better answer anyway: no firewall change, a `caddy reload` with no downtime for the demo, a separate cookie origin, and office proxies that often allow only 443. The name is `monitor.100-53-110-244.sslip.io`, free and needing no account. It got a Let's Encrypt certificate in 2 seconds. sslip.io is not on the Public Suffix List, so a first certificate can hit a shared limit. This one is issued, and renewals are exempt. Own domain or DuckDNS later if he wants a name that survives an IP change.

**Plain `docker run`, as the demo does.** The server has no Compose plugin, and one script is enough.

**Code from the laptop, not from GitHub.** The repo went private, and the server should hold no GitHub token. `git archive HEAD | ssh ... tar -x`. Checked by md5.

**Limits so it cannot hurt the demo.** App 500 MB and 1 CPU, database 300 MB and 1 CPU, both with a raised OOM score, so the kernel picks them before the demo. Docker logs capped. Pictures on their own 6 GB disk file.

**Docker ordered after that disk, without requiring it.** `nofail` means the boot does not wait for the mount, so a container could otherwise start on the empty folder under it. `After=` and `Wants=` in a drop-in, not `RequiresMountsFor`: if the disk ever fails to mount, the demo must still start.

**MariaDB 10.11**, the version the rig's test suites already ran on. Smaller than MySQL 8.

**Pakistan time in both PHP and the database.** The console shows stored times as they are, and "today" comes from the server's clock.

**`KeepAlive Off` in Apache.** With prefork, each idle connection from Caddy would hold a worker.

## Differences from Sir's server worth knowing

Cookie marked Secure (`X-Forwarded-Proto` to `HTTPS=on`), `display_errors` off, nightly cleanup scheduled, GD and OPcache built into the image rather than installed at each start. No code differences.

## Risks

1. The address contains the IP: stopping the instance changes it. Don't stop it.
2. The whole server lives on the $100 credit, about 5 months at $20 a month.
3. A real reboot is untested, to keep the demo up during judging. Do it after judging.
