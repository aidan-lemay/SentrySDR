# SentrySDR

Self-hosted radio monitoring. SentrySDR tunes RTL-SDR receivers, records each transmission as a separate call, and gives approved users a PWA for live listening, recordings, schedules, and aircraft and ship maps.

It supervises existing tools instead of reimplementing them: `rtl_fm`, `ffmpeg`, `readsb` and tar1090, `AIS-catcher`, and `dsd-fme`.

## How it fits together

- **One application, two roles.** `node.role` in the configuration file selects `master` or `slave`.
- **Master:** holds users and recordings, serves the web UI, runs schedules, and forwards calls to RDIO and ClearCut. By default it listens on loopback only, so publish it through a tunnel or reverse proxy. Direct access by local IP is opt-in.
- **Slave:** opens no listening ports. It dials out to the master, takes its configuration from the master's UI, and sends everything back. If the link drops, it keeps recording its last assignment and uploads the calls when the link returns.
- **One target per SDR at a time.** A schedule takes over its source for the window, then the source returns to its assigned target.
- **Recordings stand alone.** Each keeps its own copy of the source and target names, so it stays searchable after that setup is deleted.

## Install

```sh
sudo ./scripts/install.sh master          # or: slave
sudo ./scripts/install.sh master --with-decoders   # also builds readsb, tar1090, AIS-catcher, dsd-fme
```

The script prints the remaining steps: choose SDR serials, edit `/etc/sentrysdr/sentrysdr.toml`, create the first admin, and start the service. There are no default credentials. Example configurations are in [config/](config/).

To add a slave, go to **Admin > Nodes** on the master to create a one-time token, then put the token in the slave's configuration. The token is swapped for a key that you can revoke per node.

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

## Security model

- **Access:** signing up only requests an account. Every data route needs a signed-in, approved user, and a test fails if a new route forgets this.
- **Passwords:** Argon2id, with limits on sign-in attempts per account and per address, and optional TOTP 2FA. Session tokens, device keys and enrolment tokens are stored only as hashes.
- **Secrets at rest:** TOTP seeds and upload API keys are encrypted with `secret.key` in the data folder. Back up that file with the database, and keep both private.
- **Browser protections:** strict CSP, `HttpOnly` and `SameSite=Strict` cookies, and Host and Origin checks. tar1090 is the one exception: it needs inline scripts, so it gets a looser CSP on its own path.
- **Radio tools:** run as argv lists, never through a shell, with all values validated first, including those arriving from the network. Decoders are started with no network options, so they open no ports.
- **SDRs:** only serials in `sdr.serials` are ever opened. Other dongles stay free for other software. Duplicate serials are refused; fix them with `rtl_eeprom -s`.
- **Service:** runs unprivileged under a systemd sandbox, with only its data and log folders writable. If you move `recordings_dir` or `log_dir`, add the new folder to `ReadWritePaths` in the unit.
- **Encryption on the air:** encrypted digital traffic isn't decoded. Check the rules on recording and sharing radio traffic where you operate.

## Decoder status

| Tool | Status |
|---|---|
| `rtl_fm` (NFM, AM, WFM) | Run on a real RTL-SDR, including recording and live audio |
| `readsb`, tar1090 | Run on a real RTL-SDR with real aircraft |
| `AIS-catcher` | Run on a real RTL-SDR, and its output checked with a known message; no live ship received yet |
| `dsd-fme` | DMR tested on air: a real simplex transmission recorded and played back clearly, with correct speed and call splitting, and live audio in step. P25, NXDN, D-STAR and YSF use the same pipeline but haven't been tested on air |

Squelch, calls and noise measurement have been tested on air with NFM on three tuners: R820T (NESDR Mini), R828D and FC0012. Noise readings and squelch numbers differ between them, so measure each stick.

## USB connections

RTL-SDRs are USB 2.0 devices and can be sensitive to how they're connected. Three sticks behind a stacked, powered USB 3.0 hub repeatedly failed USB resets, took about four times as long to start, gave wrong noise readings, and eventually dropped off the bus. Plugged straight into the computer they ran with no errors. Use the computer's own ports or a powered USB 2.0 hub, and look for trouble with:

```sh
journalctl -k | grep -i -E "cannot reset|not accepting address|descriptor read"
```

If a stick is unplugged, its source shows a fault, and recording resumes by itself when it's back. Other SDRs are not affected. Calls in progress are saved.

## Tuning call detection

A call is whatever passes the target's **squelch level**. If the level is below the noise, the squelch never closes, and you get back-to-back `max_call_seconds` recordings of static. After 10 minutes open, the Sources page shows a warning. A squelch of `0` records continuously, split into `max_call_seconds` pieces.

The noise level changes a lot with stick, frequency, mode, gain and antenna, so one number doesn't suit every target. On an R828D stick with AGC, for example, noise read about 64 at 444.85 MHz but about 540 at 154 MHz.

**Squelch profiles** hold this per stick and target. On the Sources page, open **Squelch profiles** on an SDR, choose a target and gain, and select **Measure noise** while the channel is quiet. Measuring works for SDRs on slaves too, and pauses that SDR for about 10 seconds. Save the suggestion, then raise it if noise still records or lower it if weak calls are missed.

- A profile replaces the target's squelch and the source's gain when that target runs on that SDR. Other SDRs and targets keep their own settings.
- Changing a target's frequency or mode removes its profiles, because the readings no longer apply.
- Saving a profile applies straight away if that target is running.
- Measuring and saving need the `assign` permission. If a recorder on the SDR is locked, only an administrator can do either.
- Master and slaves must run the same version for measuring to work on a slave.

Noise also changes with how a stick is connected, and rises when other sticks run nearby. Measure each SDR in its final position with the others running, and measure again if it records noise by itself.

With nothing else running, `sentrysdr measure --serial <serial> --mhz 444.85 --mode nfm` takes the same reading from the terminal.

## Uploads

Set server URLs and keys in **Admin > Settings**, then enable uploads per target. Failed uploads retry with increasing delays, up to 12 attempts.

A target's **Volume** setting applies to its recordings, uploads and live audio. It doesn't affect squelch. Any setting other than 0 dB also removes DC, which stops a tuning offset from clipping the start of a call.

### Broadcastify Calls

Replaces trunk-recorder's `broadcastify_uploader` for conventional channels. Broadcastify must already have approved your node: apply at broadcastify.com/apply/calls, and each frequency needs a matching RadioReference entry.

1. In **Admin > Settings > Broadcastify Calls systems**, add each node's name, system ID and API key, then select **Check credentials**. This sends no call.
2. On each target, turn on **Upload calls to Broadcastify Calls**, then choose the system and the frequency's slot. Slots are on Broadcastify's Manage Calls Systems page, under Node Frequencies.

All uploads go from the master, including calls recorded on slaves. A call Broadcastify skips counts as finished, and a call it rejects is not retried. The reason shows on the recording, and other calls keep uploading. Stop any other uploader on the same slot before you switch.

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
