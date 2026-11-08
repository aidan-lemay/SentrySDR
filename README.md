# SentrySDR

Self-hosted radio monitoring. SentrySDR tunes RTL-SDR receivers, records each transmission as a separate call, and gives approved users a PWA for live listening, recordings, schedules, and aircraft and ship maps.

It supervises existing tools instead of reimplementing them: `rtl_fm`, `ffmpeg`, `readsb` and tar1090, `AIS-catcher`, `dsd-fme` and `acarsdec`. It can also upload calls to Broadcastify Calls, RDIO Scanner and ClearCut, and share aircraft, ship and ACARS data with community services.

## How it fits together

- **One application, two roles.** `node.role` in the configuration file selects `master` or `slave`.
- **Master:** holds users, recordings and ACARS messages, serves the web UI, runs schedules, and uploads calls to Broadcastify Calls, RDIO Scanner and ClearCut. By default it listens on loopback only, so publish it through a tunnel or reverse proxy. Direct access by local IP is opt-in.
- **Slave:** opens no listening ports. It dials out to the master, takes its configuration from the master's UI, and sends everything back. If the link drops, it keeps recording its last assignment and uploads the calls when the link returns.
- **One target per SDR at a time.** A schedule takes over its source for the window, then the source returns to its assigned target.
- **Locks.** An administrator can tick **Lock this assignment** on the Sources page. Other users then can't change that SDR's assignment or schedules, its target, or its squelch profiles, and can't measure it. There's no password. Locks set with a password in an older version stay locked, and their passwords are deleted on upgrade.
- **Recordings stand alone.** Each keeps its own copy of the source and target names, so it stays searchable after that setup is deleted.

## Install

```sh
sudo ./scripts/install.sh master          # or: slave
sudo ./scripts/install.sh master --with-decoders   # also builds readsb, tar1090, AIS-catcher, dsd-fme, acarsdec
```

The script prints the remaining steps: choose SDR serials, edit `/etc/sentrysdr/sentrysdr.toml`, create the first admin, and start the service. There are no default credentials. Example configurations are in [config/](config/).

To add a slave, go to **Admin > Nodes** on the master to create a one-time token, then put the token in the slave's configuration. The token is swapped for a key that you can revoke per node.

Before you rely on it, read [USB connections](#usb-connections), and [Storing recordings on another drive](#storing-recordings-on-another-drive) if recordings won't stay on the system disk.

## Storing recordings on another drive

Set `recordings_dir` in the master's `[storage]` section. Slaves only hold calls until the master has them, so the drive belongs on the master.

The service runs as the `sentrysdr` user inside a sandbox, so a new location needs **both** of these, or the service won't start:

1. **File ownership.** Create the folder yourself and give it to the service user. Do this before the first start, and again if you create or copy anything there as root or your own account:

   ```sh
   sudo install -d -o sentrysdr -g sentrysdr -m 750 /media/external/SentrySDR
   sudo chown -R sentrysdr:sentrysdr /media/external/SentrySDR
   sudo -u sentrysdr mkdir -p /media/external/SentrySDR/recordings/.tmp && echo OK
   ```

2. **Sandbox access.** Run `sudo systemctl edit sentrysdr` and add the lines below. An override survives re-running `install.sh`, while edits to the unit file itself don't.

   ```ini
   [Unit]
   RequiresMountsFor=/media/external

   [Service]
   ReadWritePaths=/media/external/SentrySDR
   ```

The error tells you which one is missing:

| Error | Cause | Fix |
|---|---|---|
| `Permission denied` | The `sentrysdr` user doesn't own the folder or can't reach it | Step 1. If it still fails, `namei -l <path>` shows which parent folder blocks it |
| `Read-only file system` | The sandbox is blocking the path | Step 2 |

`create-admin` and other commands run from a shell are outside the sandbox, so they can work while the service still fails. A traceback from `create-admin` means it stopped before saving anything, so run it again after the fix.

- **Mount it first.** Check with `findmnt -T /media/external`. If it shows `/`, the drive isn't mounted, and files would go to the system disk. Mount it by UUID in `/etc/fstab` with `nofail`. With `RequiresMountsFor`, the master then won't start without the drive instead of quietly filling the system disk.
- **Use ext4 if you can.** exFAT and NTFS don't store Linux ownership, so `chown` won't work on them. Set the owner when mounting instead, with `uid=sentrysdr,gid=sentrysdr,umask=027`. Keep `data_dir` off those filesystems, because the database can be unreliable there.
- **Moving existing recordings.** Stop the service, copy them with `sudo rsync -a`, and update `recordings_dir`. Recordings are stored by relative path, so they keep working.
- **If the drive disconnects while running,** saving and playing recordings fail until it's back and the service is restarted, and calls recorded in that time may be lost.
- **Back up `data_dir` separately,** including `secret.key`. Recordings can't be found without the database.

## Publishing the master

| Method | Example | Inbound ports |
|---|---|---|
| Cloudflare Tunnel | [cloudflared.example.yml](deploy/cloudflared.example.yml) | None |
| Tailscale | `tailscale serve --bg 8642`, and add the `https://….ts.net` address to `extra_origins` | None on the public internet |
| Caddy | [Caddyfile.example](deploy/Caddyfile.example) | 80, 443 |
| nginx | [nginx.example.conf](deploy/nginx.example.conf) | 80, 443 |
| Cloudflare Tunnel, then nginx on another machine | [nginx-tunnel.example.conf](deploy/nginx-tunnel.example.conf) | 8642, from your network only |

**Set `[proxy]` to match your method.** Bans, rate limits and audit logs need the real client address. With this unset, every visitor appears to come from the proxy's address, and the rate limit on sign-ins is shared by everyone.

[nftables.example.conf](deploy/nftables.example.conf) is a default-deny firewall. Review it before loading it.

### Using a local IP address

A tunnel or proxy is the better choice, but you can also open the master directly at `http://<ip>:8642` from your own network:

```toml
[server]
bind_host = "0.0.0.0"
local_access = true
```

- **Who can connect:** only private, link-local, loopback and Tailscale addresses, plus anything in `trusted_proxies`. Other addresses get a 403. Everyone still has to sign in.
- **Which addresses work:** any private or Tailscale IP of the machine, with no further setup. To use a name such as `minipc.lan`, add `http://minipc.lan:8642` to `extra_origins`.
- **What you give up:** plain http isn't encrypted, so anyone on your network who can see the traffic can read passwords and session cookies. Don't use it on a shared network, and never forward the port from the internet. Browsers also won't install the PWA without HTTPS; the site itself still works.
- **Slaves:** a slave can use `http://<ip>:8642` as `master.url`. Its enrolment token and device key travel unencrypted too, unless the address is on Tailscale or another VPN.
- **`allow_public_bind`:** setting this turns off the address restriction, so leave it off.

This can run alongside a proxy. Requests through the proxy still get Secure cookies, and those through the local address don't.

## Users and permissions

Anyone can request an account at the sign-in page, unless **Let people request accounts** is off under **Admin > Settings**. A request does nothing until an administrator approves it under **Admin > Users**. From there an administrator can also change a user's role, see their sessions and history, reset their password or two-factor login, disable them, and ban their email or address. Disabling a user or resetting their password signs them out. **Admin > Bans** takes addresses and ranges too. Everyone can turn on two-factor login and change their password under **My account**.

| Permission | Listener | Operator | Admin |
|---|---|---|---|
| Listen live, play and download recordings, see maps and messages | Yes | Yes | Yes |
| Assign SDRs, make schedules, measure noise, save squelch profiles | | Yes | Yes |
| Add and edit targets | | Yes | Yes |
| Protect recordings, request ClearCut transcripts | | Yes | Yes |
| Lock assignments, delete recordings, manage SDRs, antennas, nodes, users and settings | | | Yes |

Each permission except the admin-only ones can be allowed or denied per user, under **Admin > Users > Manage**. A protected recording is never deleted automatically, and has to be unprotected before an admin can delete it. **Admin > Audit log** records sign-ins, changes and refusals, with the user and address.

## Admin settings

| Section | Holds |
|---|---|
| Users, Bans, Audit log | Accounts and access, as above |
| SDR sources | Each SDR's name, description, antenna, gain, frequency correction and capabilities |
| Antennas | Antennas and their frequency ranges |
| Nodes | Slaves: create a one-time enrolment token, or revoke a slave |
| Settings | How long recordings, ACARS messages and audit logs are kept (0 keeps them forever); sign-ups; RDIO and ClearCut servers and keys; the Broadcastify upload address and systems; the airframes.io station ID; the map tile server |

Upload API keys are never shown again once saved. Leave a key's box blank to keep it.

## Security model

- **Access:** signing up only requests an account. Every data route needs a signed-in, approved user, and a test fails if a new route forgets this.
- **Passwords:** Argon2id, with limits on sign-in attempts per account and per address, and optional TOTP 2FA. Session tokens, device keys and enrolment tokens are stored only as hashes.
- **Secrets at rest:** TOTP seeds and upload API keys are encrypted with `secret.key` in the data folder. Back up that file with the database, and keep both private.
- **Browser protections:** strict CSP, `HttpOnly` and `SameSite=Strict` cookies, and Host and Origin checks. tar1090 is the one exception: it needs inline scripts, so it gets a looser CSP on its own path.
- **Radio tools:** run as argv lists, never through a shell, with all values validated first, including those arriving from the network. Decoders open no ports, with one opt-in exception: the local data port below.
- **Outbound connections:** only the uploads and feeds you turn on. Aggregator addresses are fixed in the code, so a master can switch a slave's feeds on or off but can't point them anywhere else. The airframes.io feed sends only acarsdec's own message fields and your station ID.
- **Local data port:** the optional port for fr24feed and piaware is bound to 127.0.0.1 on the node with the SDR. readsb opens no other ports.
- **SDRs:** only serials in `sdr.serials` are ever opened. Other dongles stay free for other software. Duplicate serials are refused; fix them with `rtl_eeprom -s`.
- **Service:** runs unprivileged under a systemd sandbox, with only its data and log folders writable. If you move `recordings_dir` or `log_dir`, see [Storing recordings on another drive](#storing-recordings-on-another-drive).
- **Encryption on the air:** encrypted digital traffic isn't decoded. Check the rules on recording and sharing radio traffic where you operate.

## Decoder status

| Tool | Status |
|---|---|
| `rtl_fm` (NFM, AM, WFM) | Run on a real RTL-SDR, including recording and live audio |
| `readsb`, tar1090 | Run on a real RTL-SDR with real aircraft |
| `AIS-catcher` | Run on a real RTL-SDR, and its output checked with a known message; no live ship received yet |
| `acarsdec` | Not yet run on a real stick: tested against a stand-in that follows acarsdec's documented options and JSON output |
| `dsd-fme` | DMR tested on air: a real simplex transmission recorded and played back clearly, with correct speed and call splitting, and live audio in step. P25, NXDN, D-STAR and YSF use the same pipeline but haven't been tested on air |

Squelch, calls and noise measurement have been tested on air with NFM on three tuners: R820T (NESDR Mini), R828D and FC0012. Noise readings and squelch numbers differ between them, so measure each stick.

## USB connections

> **Warning: avoid USB 3.0 hubs and stacked hubs.** Use the computer's own ports or a powered USB 2.0 hub, with one hub between the computer and the stick.

RTL-SDRs are USB 2.0 devices, and a bad connection can look like a radio problem. With three sticks behind a powered USB 3.0 hub and two further levels of hub, we saw:

- failed USB resets, and a stick dropping off the bus
- `rtl_fm` taking about 4 seconds to start instead of about 1, or failing with `Failed to set center freq`
- `rtl_fm` not responding when told to stop
- noise readings around 60 times too high, which would suggest a squelch that misses real calls (this one lined up with the reset errors, but we couldn't reproduce it)

The same sticks ran with no USB errors when plugged straight into the computer. We didn't separate USB 3.0 from the stacked hubs as the cause, so avoid both. Short extension cables were fine.

To check for trouble, including on a slave:

```sh
journalctl -k | grep -i -E "cannot reset|not accepting address|descriptor read"
```

Any of those lines means the connection needs attention, so fix it before you measure noise or trust a squelch setting. `USB disconnect` on its own is normal when you unplug something.

A stick that was last closed uncleanly, for example when a tool was killed, can come up with USB errors and then send nothing. When an SDR shows USB errors as it starts, SentrySDR restarts it once so it opens cleanly, and the Sources page notes the time. If it happens again within a minute, it's left running with the note. Seeing this often points to the hub or cable.

If a stick is unplugged, its source shows a fault, and recording resumes by itself when it's back. Other SDRs keep running, and a call in progress is saved. A stick that stays connected but stops working may not show a fault, because it looks the same as a closed squelch.

## Antennas

Describe each antenna under **Admin > Antennas**: name, type, length and the frequency ranges it works on. Use **+ Add** for each band, so a dual-band antenna gets two ranges. Then choose the antenna on each SDR under **Admin > SDR sources**. One antenna can feed several SDRs, for example through a splitter.

If a target is outside every range of an SDR's antenna, the target pickers mark it **outside antenna range**, and assigning or scheduling it shows a warning. It still works, because an antenna often hears more than its rated bands. An antenna with no ranges isn't checked. Removing an antenna leaves its SDRs with none set.

An older database's antenna type and frequency range on each source are turned into antennas on first start. Sources with the same type and range share one.

## Stick capabilities

Under **Admin > SDR sources**, each SDR can record what it can do: its tuner, frequency range, gain steps and uses. **Detect from the stick** reads the tuner and gain steps from the stick itself, on the master or a slave, and fills in that tuner's usual range. It pauses the stick for a few seconds and saves nothing until you select **Save**. Every field can be edited by hand.

- A target outside the stick's range, or of a kind the stick isn't ticked for, is marked in the target pickers and warns when assigned or scheduled. It still runs. Analog voice, digital voice, ADS-B, AIS and ACARS are checked; Broadcast FM also counts for WFM targets.
- The sticks only support their own gain steps: anything in between is rounded to the nearest. Once a stick's steps are known, its gain boxes become a list of those steps plus AGC, and other values are refused. A gain saved before the steps were known stays until changed, shown with the step it really gets. Detect moves the gain to the nearest step.
- Without steps, for example before Detect or when the stick is open in another program, the gain box takes any number.
- Anything left blank isn't checked. The description shows on the Sources page under the SDR's name.

## ACARS

ACARS is the short text messages aircraft send on VHF. An ACARS target lists the channels to decode: **North America** and **Europe** fill in the usual ones, and **+ Add** adds others. One stick decodes all of a target's channels at once with `acarsdec`, so they must fit within 2 MHz. North America's fifth channel, 129.125 MHz, doesn't fit with the other four, so it needs its own target and stick. Gain matters: acarsdec's AGC usually sits at maximum, so set a fixed gain on the stick.

Messages appear on the **Messages** page, newest first, and update by themselves. Search by flight, registration, label or text. Messages with no text, mostly link checks, are hidden unless you tick **Show messages with no text**. On the ADS-B map, an aircraft that sent messages in the last hour links to them, matched by registration through tar1090's aircraft database. Flight numbers aren't matched, because ACARS and ADS-B use different airline codes. If two sticks hear the same message, it's kept once.

To feed [airframes.io](https://airframes.io), set a **Station ID** under **Admin > Settings**, for example `KE-KSEA-ACARS`: your initials, the nearest airport's ICAO code, then ACARS. Then tick **Feed messages to airframes.io** on the target. No account is needed. Messages are kept for 30 days by default, set under **Admin > Settings**. Messages from a slave are lost while its link to the master is down.

acarsdec needs cJSON to build: `libcjson-dev` on Debian and Raspberry Pi OS, which `install.sh --with-decoders` installs.

## Sharing ADS-B and AIS

Turn sharing on per target, on the Targets page.

Sharing runs wherever the target runs, on the master or a slave, and stops when the SDR moves to another target. A slave whose link to the master drops keeps sharing for its last assignment.

**ADS-B aggregators:** tick ADS-B Exchange, airplanes.live, adsb.lol or adsb.fi under **Share with** on an ADS-B target. readsb connects out to each one, using the addresses from each service's own setup script; no account or sign-up is needed. Each SDR has its own random ID, which the services use to tell feeders apart. The Sources page shows whether each one is connected; it checks every 10 seconds. To confirm data is arriving, use the feeder check on each service's website, which finds your feed by your public IP address.

**FlightRadar24 and FlightAware** need their own programs, `fr24feed` and `piaware`, installed on the machine with the ADS-B stick (the master or a slave). Tick **Offer data to fr24feed or piaware** on the ADS-B target, and choose a port (30005 is the usual one). SentrySDR then offers Beast data on `127.0.0.1` only. The Sources page shows how many programs are reading it.

- **FlightAware:** install the `piaware` package with FlightAware's instructions, then run `sudo piaware-config receiver-type other`, `sudo piaware-config receiver-host 127.0.0.1` and `sudo piaware-config receiver-port 30005`, and restart piaware. Claim the feeder on FlightAware's site.
- **FlightRadar24:** install `fr24feed` with FlightRadar24's instructions. In its sign-up, choose a Beast receiver over TCP (network), host `127.0.0.1`, port `30005`.
- **Don't let either install its own decoder.** `dump1090-fa`, or a dump1090 that fr24feed sets up, would open an RTL-SDR itself and take a stick from SentrySDR. If one got installed, stop and disable it.
- MLAT isn't set up by SentrySDR. Both programs do their own MLAT if you give them your antenna's location.
- The programs keep running when the SDR moves to another target, and reconnect when it comes back. SentrySDR doesn't start or stop them.
- Each ADS-B target on one machine needs its own port. If two running at once share one, the second can't open it and shows a fault on the Sources page.

**AIS:** on an AIS target:

- **aiscatcher.org:** tick **Share with the aiscatcher.org community feed**. It's sent without a station key, so it isn't linked to an aiscatcher.org account.
- **MarineTraffic, AISHub and VesselFinder:** register a station with the service first. Each gives you a UDP host and port. Tick **Send to** for that service and enter both. Up to six destinations per target.
- UDP can't confirm delivery, so the Sources page only lists where data is being sent. Check the service's station page to confirm it's arriving.

## Tuning call detection

A call is whatever passes the target's **squelch level**. If the level is below the noise, the squelch never closes, and you get back-to-back `max_call_seconds` recordings of static. After 10 minutes open, the Sources page shows a warning. A squelch of `0` records continuously, split into `max_call_seconds` pieces.

The noise level changes a lot with stick, frequency, mode, gain and antenna, so one number doesn't suit every target. On an R828D stick with AGC, for example, noise read about 64 at 444.85 MHz but about 540 at 154 MHz.

**Squelch profiles** hold this per stick and target. On the Sources page, open **Squelch profiles** on an SDR, choose a target and gain, and select **Measure noise** while the channel is quiet. Measuring works for SDRs on slaves too, and pauses that SDR for about 10 seconds. Save the suggestion, then raise it if noise still records or lower it if weak calls are missed.

- A profile replaces the target's squelch and the source's gain when that target runs on that SDR. Other SDRs and targets keep their own settings.
- Changing a target's frequency or mode removes its profiles, because the readings no longer apply.
- Saving a profile applies straight away if that target is running.
- Measuring and saving need the `assign` permission. If the SDR's assignment is locked, only an administrator can do either.
- Master and slaves must run the same version for measuring to work on a slave.

A new source's gain box starts at 40.2 dB. Leave it empty for automatic gain (AGC). 0 is a real 0 dB, not automatic, and may miss most calls. Existing sources keep their setting.

Noise also changes with how a stick is connected, and rises when other sticks run nearby. Measure each SDR in its final position with the others running, and measure again if it records noise by itself.

With nothing else running, `sentrysdr measure --serial <serial> --mhz 444.85 --mode nfm` takes the same reading from the terminal.

## Uploads

Set server URLs and keys in **Admin > Settings**, then turn uploads on per target, on the Targets page. Uploads go from the master, including calls recorded on slaves. Failed uploads retry with increasing delays, up to 12 attempts, and the result shows on each recording.

A target's **Volume** setting applies to its recordings, uploads and live audio. It doesn't affect squelch. Any setting other than 0 dB also removes DC, which stops a tuning offset from clipping the start of a call.

### Broadcastify Calls

Replaces trunk-recorder's `broadcastify_uploader` for conventional channels. Broadcastify must already have approved your node: apply at broadcastify.com/apply/calls, and each frequency needs a matching RadioReference entry. See Broadcastify's [upload API](https://support.broadcastify.com/hc/en-us/articles/360049971311-Broadcastify-Calls-Ingest-Upload-API) and [production status page](https://www.broadcastify.com/calls/status/).

- **Keep the clock right.** Broadcastify requires accurate time, and it rejects or wrongly deduplicates calls with bad timestamps. Each call's time comes from the machine that recorded it, so check the master and every slave with `timedatectl`, which should say `System clock synchronized: yes`. A Pi usually has no battery-backed clock, so it needs network time.
- **Credentials are separate per environment.** Production credentials only work on `https://api.broadcastify.com/call-upload`. Development credentials are issued separately and only work on `https://api.broadcastify.com/call-upload-dev`. Don't try one on the other.

1. In **Admin > Settings > Broadcastify Calls systems**, add each node's name, system ID and API key, then select **Check credentials**. This sends no call.
2. On each target, turn on **Upload calls to Broadcastify Calls**, then choose the system and the frequency's slot. Slots are on Broadcastify's Manage Calls Systems page, under Node Frequencies.

All uploads go from the master, including calls recorded on slaves. A call Broadcastify skips counts as finished, and a call it rejects, or refuses for a bad API key, is not retried. The reason shows on the recording, and other calls keep uploading. Broadcastify refuses calls that arrive late (64 seconds was too late in testing), so other failures retry every 10 seconds, and calls held during an outage will probably be rejected. They stay in SentrySDR.

`No-Freq-Table-Entry-For-Upload` means Broadcastify can't match the call to a frequency on that node. Check the system, the target's slot and its frequency against **Node Frequencies** on the Manage Calls Systems page. Stop any other uploader on the same slot before you switch.

### RDIO Scanner

1. In your RDIO Scanner admin, create an API key, and note the system and talkgroup IDs the channel should appear under.
2. In **Admin > Settings > Uploads**, enter the RDIO server's address, such as `https://rdio.example.com`, and the API key. SentrySDR posts calls to its `/api/call-upload`.
3. On each target, turn on **Upload calls to RDIO** and enter the system and talkgroup.

### Duplicate channels

- A Broadcastify slot can only belong to one target, and saving a second is refused.
- Saving a target with the same frequency and mode as another, or giving an SDR a channel that another SDR can also record, shows a warning.
- If two SDRs do record the same target at once, both recordings are kept, but only the first copy of each call is uploaded. The other shows as skipped.

### ClearCut transcripts

ClearCut makes the transcript itself after a call is uploaded, either automatically or on request, and adds it to its copy of the call.

- **Setup:** a target needs the ClearCut system ID (such as `us-ny-monroe`) and the talkgroup, as well as the server and key in settings.
- **Automatic:** once a minute, for talkgroups with calls still waiting, SentrySDR reads ClearCut's call list and copies any finished transcripts. It keeps looking for 24 hours after each call.
- **On request:** **Get transcript** under a recording asks ClearCut to transcribe that call. Admins and operators can do this by default, limited to 30 requests per user per hour, because each one uses transcription time on the ClearCut server.
- **Matching:** a recording is matched to its ClearCut call by system, talkgroup and start time. ClearCut only lists recent calls, so on a very busy talkgroup a call can scroll off the list before it's matched.

ClearCut has no published API docs. The upload format comes from the `clearcut_uploader` plugin in [galenguyer/trunk-recorder](https://github.com/galenguyer/trunk-recorder/tree/clearcut), and the read and transcribe calls from [ClearCut_Mobile](https://github.com/aidan-lemay/ClearCut_Mobile).

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
npm --prefix web ci && npm --prefix web run build
./scripts/build-decoders.sh     # optional; builds into the git-ignored .vendor/
```

Create an admin with `sentrysdr -c <config> create-admin`. Run with `sentrysdr -c <config> run`.

Tests use fake radio tools, so they need `ffmpeg` but no SDR.
