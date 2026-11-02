"""Command line: run the configured role, create an admin, or list SDRs."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

from .config import ConfigError, load_config


def cmd_run(cfg) -> int:
    from .context import setup_audit_file, setup_logging
    setup_logging(cfg)
    setup_audit_file(cfg)
    if cfg.role == "slave":
        from .slave import Slave
        try:
            asyncio.run(Slave(cfg).run())
        except KeyboardInterrupt:
            pass
        return 0

    import uvicorn
    from .context import Context
    from .web import create_app
    app = create_app(Context(cfg))
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
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="sentrysdr")
    parser.add_argument("-c", "--config", default=os.environ.get("SENTRYSDR_CONFIG", "sentrysdr.toml"))
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("run", help="run the role set in the configuration file")
    admin = commands.add_parser("create-admin", help="create an administrator, or reset one's password")
    admin.add_argument("--email")
    commands.add_parser("list-sdrs", help="show connected SDRs without claiming them")
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
    return cmd_run(cfg)


if __name__ == "__main__":
    sys.exit(main())
