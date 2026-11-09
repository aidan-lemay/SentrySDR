"""Configuration file loading and validation.

One codebase serves both roles: ``node.role`` selects master or slave. A slave
never opens a listening socket; it only dials out to ``master.url``.
"""
from __future__ import annotations

import ipaddress
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

SERIAL_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
TAILSCALE_NETWORK = ipaddress.ip_network("100.64.0.0/10")


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    role: str = "master"
    node_name: str = "sentrysdr"
    bind_host: str = "127.0.0.1"
    bind_port: int = 8642
    public_url: str = "http://localhost:8642"
    extra_origins: list[str] = field(default_factory=list)
    allow_public_bind: bool = False
    local_access: bool = False
    timezone: str = "UTC"
    trusted_proxies: list[str] = field(default_factory=list)
    client_ip_header: str = ""
    data_dir: Path = Path("./data")
    recordings_dir: Path = Path("./data/recordings")
    log_dir: Path = Path("./data/logs")
    serials: list[str] = field(default_factory=list)
    master_url: str = ""
    enrollment_token: str = ""
    spool_max_mb: int = 2048
    hang_seconds: float = 2.0
    min_call_seconds: float = 0.5
    max_call_seconds: int = 1800
    bin_dirs: list[Path] = field(default_factory=list)
    # rtl_tcp has no password: whoever reaches this address controls the stick.
    rtl_tcp_bind: str = "127.0.0.1"
    rtl_tcp_allow_public: bool = False

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def base_path(self) -> str:
        return urlsplit(self.public_url).path.rstrip("/") or "/"

    @property
    def allowed_origins(self) -> set[str]:
        return {_origin(u) for u in [self.public_url, *self.extra_origins]}

    @property
    def allowed_hosts(self) -> set[str]:
        hosts = {"localhost", "127.0.0.1", "::1"}
        for url in [self.public_url, *self.extra_origins]:
            host = urlsplit(url).hostname
            if host:
                hosts.add(host)
        return hosts

    @property
    def plain_http_hosts(self) -> set[str]:
        """Names configured for plain http, such as http://minipc.lan:8642."""
        urls = [urlsplit(u) for u in [self.public_url, *self.extra_origins]]
        return {u.hostname for u in urls if u.scheme == "http" and u.hostname}

    @property
    def trusted_networks(self) -> list:
        return [ipaddress.ip_network(c, strict=False) for c in self.trusted_proxies]

    def tool_env(self) -> dict[str, str]:
        """Environment for radio tools: privately built libraries sit beside their binaries."""
        env = dict(os.environ)
        libraries = [str((directory.parent / "lib").resolve()) for directory in self.bin_dirs]
        existing = env.get("LD_LIBRARY_PATH")
        env["LD_LIBRARY_PATH"] = ":".join(libraries + ([existing] if existing else []))
        return env

    def binary(self, name: str) -> str | None:
        for directory in self.bin_dirs:
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)
        return shutil.which(name)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_local_ip(value: str | None) -> bool:
    """True for loopback, private, link-local and Tailscale addresses. Names are not IPs."""
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    addr = getattr(addr, "ipv4_mapped", None) or addr
    return addr.is_loopback or addr.is_private or addr.is_link_local or addr in TAILSCALE_NETWORK


def _lan_bindable(host: str) -> bool:
    """A wildcard is allowed because local_access refuses public peers per request."""
    try:
        if ipaddress.ip_address(host).is_unspecified:
            return True
    except ValueError:
        return False
    return is_local_ip(host)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text())
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    node = raw.get("node", {})
    server = raw.get("server", {})
    proxy = raw.get("proxy", {})
    storage = raw.get("storage", {})
    sdr = raw.get("sdr", {})
    master = raw.get("master", {})
    calls = raw.get("calls", {})
    rtl_tcp = raw.get("rtl_tcp", {})

    data_dir = Path(storage.get("data_dir", "./data"))
    cfg = Config(
        role=node.get("role", "master"),
        node_name=node.get("name", "sentrysdr"),
        bind_host=server.get("bind_host", "127.0.0.1"),
        bind_port=int(server.get("bind_port", 8642)),
        public_url=server.get("public_url", "http://localhost:8642"),
        extra_origins=list(server.get("extra_origins", [])),
        allow_public_bind=bool(server.get("allow_public_bind", False)),
        local_access=bool(server.get("local_access", False)),
        timezone=server.get("timezone", "UTC"),
        trusted_proxies=list(proxy.get("trusted_proxies", [])),
        client_ip_header=proxy.get("client_ip_header", ""),
        data_dir=data_dir,
        recordings_dir=Path(storage.get("recordings_dir", data_dir / "recordings")),
        log_dir=Path(storage.get("log_dir", data_dir / "logs")),
        serials=[str(s) for s in sdr.get("serials", [])],
        master_url=master.get("url", ""),
        enrollment_token=master.get("enrollment_token", ""),
        spool_max_mb=int(master.get("spool_max_mb", 2048)),
        hang_seconds=float(calls.get("hang_seconds", 2.0)),
        min_call_seconds=float(calls.get("min_call_seconds", 0.5)),
        max_call_seconds=int(calls.get("max_call_seconds", 1800)),
        bin_dirs=[Path(p) for p in raw.get("binaries", {}).get("search_dirs", [])],
        rtl_tcp_bind=str(rtl_tcp.get("bind", "127.0.0.1")),
        rtl_tcp_allow_public=bool(rtl_tcp.get("allow_public", False)),
    )
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if cfg.role not in ("master", "slave"):
        raise ConfigError("node.role must be 'master' or 'slave'")
    for serial in cfg.serials:
        if not SERIAL_RE.match(serial):
            raise ConfigError(f"invalid SDR serial {serial!r}")
    if len(set(cfg.serials)) != len(cfg.serials):
        raise ConfigError("sdr.serials contains duplicates")
    try:
        ZoneInfo(cfg.timezone)
    except Exception as exc:
        raise ConfigError(f"unknown timezone {cfg.timezone!r}") from exc
    _validate_rtl_tcp(cfg)

    if cfg.role == "slave":
        parts = urlsplit(cfg.master_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ConfigError("a slave needs master.url (http:// or https://)")
        if parts.scheme == "http" and not _private_transport(parts.hostname):
            raise ConfigError(
                "master.url uses plain http over a public address; use https, "
                "or a loopback/private/Tailscale address"
            )
        return

    parts = urlsplit(cfg.public_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError("server.public_url must be an http(s) URL")
    if parts.scheme == "http" and not _is_loopback_host(parts.hostname):
        if not (cfg.local_access and _private_transport(parts.hostname)):
            raise ConfigError(
                "server.public_url must use https unless it is a loopback address, "
                "or a private address with server.local_access = true"
            )
    if not _is_loopback_host(cfg.bind_host) and not cfg.allow_public_bind:
        if not cfg.local_access:
            raise ConfigError(
                "server.bind_host is not loopback. Serve through a reverse proxy or tunnel, "
                "set server.local_access = true for local network access, "
                "or set server.allow_public_bind = true if you really mean to expose it"
            )
        if not _lan_bindable(cfg.bind_host):
            raise ConfigError(
                "server.bind_host must be 0.0.0.0, ::, or a private address with server.local_access; "
                "set server.allow_public_bind = true if you really mean to expose it"
            )
    if cfg.client_ip_header and not cfg.trusted_proxies:
        raise ConfigError("proxy.client_ip_header requires proxy.trusted_proxies")
    try:
        cfg.trusted_networks
    except ValueError as exc:
        raise ConfigError(f"invalid proxy.trusted_proxies entry: {exc}") from exc


def _validate_rtl_tcp(cfg: Config) -> None:
    """Set here, on each machine, so the master's web interface can't expose a stick to the internet."""
    try:
        addr = ipaddress.ip_address(cfg.rtl_tcp_bind)
    except ValueError:
        raise ConfigError("rtl_tcp.bind must be an IP address, such as 127.0.0.1 or this machine's LAN or Tailscale address") from None
    if (addr.is_unspecified or not is_local_ip(cfg.rtl_tcp_bind)) and not cfg.rtl_tcp_allow_public:
        raise ConfigError(
            "rtl_tcp.bind would listen on every network or a public address, and rtl_tcp has no password. "
            "Use loopback, a private or Tailscale address, or set rtl_tcp.allow_public = true if you really mean it"
        )


def _private_transport(host: str) -> bool:
    """True for loopback, RFC1918 and Tailscale (100.64/10, *.ts.net) hosts."""
    return host == "localhost" or host.endswith(".ts.net") or is_local_ip(host)
