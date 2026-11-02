# SentrySDR

Self-hosted radio monitoring. SentrySDR tunes RTL-SDR receivers, records each transmission as a separate call, and gives approved users a PWA for live listening, recordings, schedules, and aircraft and ship maps.

It supervises existing tools instead of reimplementing them: `rtl_fm`, `ffmpeg`, `readsb` and tar1090, `AIS-catcher`, and `dsd-fme`.

## How it fits together

- **One application, two roles.** `node.role` in the configuration file selects `master` or `slave`.
- **Master:** holds users and recordings, serves the web UI, runs schedules, and forwards calls to RDIO and ClearCut. It listens on loopback only, so publish it through a tunnel or reverse proxy.
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

**Set `[proxy]` to match your method.** Bans, rate limits and audit logs need the real client address. Tunnels and proxies connect from loopback, so with this unset every visitor appears to be `127.0.0.1`, and the rate limit on sign-ins is shared by everyone.

[nftables.example.conf](deploy/nftables.example.conf) is a default-deny firewall. Review it before loading it.

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
| `dsd-fme` | Runs in the pipeline on a real RTL-SDR, and stays silent on noise; decoding a real digital signal is not yet tested |

## Tuning call detection

A call is whatever passes the target's **squelch level**. Raise it if noise keeps recording, or lower it if calls are missed. The right value depends on your antenna and site, so expect to adjust it. A squelch of `0` records continuously, split into `max_call_seconds` pieces.

## Uploads

Set server URLs and keys in **Admin > Settings**, then enable uploads per target. Failed uploads retry with increasing delays, up to 12 attempts.

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
