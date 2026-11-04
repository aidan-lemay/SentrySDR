"""Command line: run the configured role, create an admin, or list SDRs."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from .config import SERIAL_RE, ConfigError, load_config
from .pipelines import ANALOG, DIGITAL

MEASURABLE_MODES = ANALOG | set(DIGITAL)


async def run_slave(slave) -> None:
    """Run until stopped. SIGTERM must unwind normally, or the call in progress is lost."""
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    await slave.run()


def cmd_run(cfg) -> int:
    from .context import setup_audit_file, setup_logging
    setup_logging(cfg)
    setup_audit_file(cfg)
    if cfg.role == "slave":
        from .slave import Slave
        try:
            asyncio.run(run_slave(Slave(cfg)))
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        return 0

    import uvicorn
    from .context import Context
    from .web import create_app
    app = create_app(Context(cfg))
    if cfg.local_access:
        logging.getLogger("sentrysdr").warning(
            "local_access is on: plain http on %s:%s is unencrypted, including passwords. "
            "Don't forward this port from the internet.", cfg.bind_host, cfg.bind_port,
        )
    root = Path(__file__).resolve().parent.parent
    for candidate in (os.environ.get("SENTRYSDR_TAR1090"), root / ".vendor/share/tar1090", cfg.data_dir / "tar1090"):
        if candidate and Path(candidate).is_dir():
            app.state.tar1090_dir = Path(candidate)
            break
    uvicorn.run(
        app, host=cfg.bind_host, port=cfg.bind_port, log_level="warning",
        server_header=False, proxy_headers=False,  # the client address is resolved by security.client_ip
    )
    return 0


def cmd_create_admin(cfg, email: str | None) -> int:
    from sqlalchemy import select

    from . import security
    from .context import Context
    from .models import User
    if cfg.role != "master":
        print("create-admin only applies to a master", file=sys.stderr)
        return 2
    email = (email or input("Email: ")).strip().lower()
    password = getpass.getpass("Password: ")
    if getpass.getpass("Repeat password: ") != password:
        print("Passwords don't match", file=sys.stderr)
        return 1
    problem = security.password_problem(password)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    ctx = Context(cfg)
    with ctx.db() as session:
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(email=email, name=email.split("@")[0], password_hash="")
            session.add(user)
        user.password_hash = security.hash_password(password)
        user.role, user.status, user.must_change_password = "admin", "active", False
        session.commit()
    ctx.audit("cli.admin_created", email=email, ip="local-cli")
    print(f"Administrator {email} is ready.")
    return 0


def cmd_list_sdrs(cfg) -> int:
    from .sdr import enumerate_serials
    devices = enumerate_serials()
    if not devices:
        print("No RTL-SDR devices found.")
    for index, serial in enumerate(devices):
        note = "configured" if serial in cfg.serials else "not configured, will not be used"
        if devices.count(serial) > 1:
            note = "DUPLICATE SERIAL, fix with rtl_eeprom -s"
        print(f"{index}: {serial or '(no serial)'}  [{note}]")
    usable = [s for s in devices if SERIAL_RE.match(s) and devices.count(s) == 1]
    if usable:
        print("\nTo add all of them, put this in the [sdr] section of your configuration file:")
        print(f"serials = {json.dumps(usable)}")
    if len(usable) < len(devices):
        print("\nLeft out of that line: sticks with a blank or duplicate serial. Set unique serials with rtl_eeprom -s.")
    return 0


def cmd_measure(cfg, serial: str, mhz: float, mode: str, gain: float | None, ppm: int, seconds: float) -> int:
    from . import pipelines
    from .sdr import enumerate_serials, index_for_serial

    if serial not in cfg.serials:
        print(f"{serial} isn't in sdr.serials, so it won't be opened.", file=sys.stderr)
        return 2
    if not pipelines.FREQ_MIN_HZ <= round(mhz * 1e6) <= pipelines.FREQ_MAX_HZ:
        print("Frequency out of range.", file=sys.stderr)
        return 2
    index = index_for_serial(serial, enumerate_serials())
    if index is None:
        print(f"SDR {serial} isn't connected, or its serial is duplicated.", file=sys.stderr)
        return 1
    rate = pipelines.measure_rate(mode)
    print(f"Measuring {mhz:.4f} MHz, {mode.upper()}, gain {'auto' if gain is None else gain} for {seconds:g} s...", flush=True)
    args = pipelines.measure_args(cfg, index, round(mhz * 1e6), mode, gain, ppm)
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    timer = threading.Timer(seconds + 1, proc.terminate)  # the first second is discarded
    timer.start()
    try:
        pcm, stderr = proc.communicate()
    finally:
        timer.cancel()
    levels = pipelines.signal_levels(pcm, rate)
    if not levels:
        detail = stderr.decode(errors="replace").strip().splitlines()
        print("No samples read. If this SDR has a target, clear it or stop the service first.", file=sys.stderr)
        print(detail[-1] if detail else "rtl_fm gave no error.", file=sys.stderr)
        return 1
    summary = pipelines.measure_summary(levels)
    print(f"Level: lowest {summary['low']}, typical {summary['typical']}, highest {summary['high']}")
    print(f"Suggested squelch: {summary['suggested']}")
    if summary["moved"]:
        print("The level moved a lot, so something may have transmitted. Measure again if this looks wrong.")
    print("This is only valid for this frequency, mode and gain, and with this antenna.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="sentrysdr")
    parser.add_argument("-c", "--config", default=os.environ.get("SENTRYSDR_CONFIG", "sentrysdr.toml"))
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("run", help="run the role set in the configuration file")
    admin = commands.add_parser("create-admin", help="create an administrator, or reset one's password")
    admin.add_argument("--email")
    commands.add_parser("list-sdrs", help="show connected SDRs without claiming them")
    measure = commands.add_parser("measure", help="read a frequency's noise level and suggest a squelch")
    measure.add_argument("--serial", required=True)
    measure.add_argument("--mhz", type=float, required=True)
    measure.add_argument("--mode", default="nfm", choices=sorted(MEASURABLE_MODES))
    measure.add_argument("--gain", type=float, help="fixed gain in dB; leave out to match a target using AGC")
    measure.add_argument("--ppm", type=int, default=0)
    measure.add_argument("--seconds", type=float, default=10.0)
    args = parser.parse_args()
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.command == "create-admin":
        return cmd_create_admin(cfg, args.email)
    if args.command == "list-sdrs":
        return cmd_list_sdrs(cfg)
    if args.command == "measure":
        return cmd_measure(cfg, args.serial, args.mhz, args.mode, args.gain, args.ppm, args.seconds)
    return cmd_run(cfg)


if __name__ == "__main__":
    sys.exit(main())
