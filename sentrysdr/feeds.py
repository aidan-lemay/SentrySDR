"""Sharing ADS-B and AIS data with outside services.

ADS-B aggregators are fed by readsb itself with an outbound connection. The
addresses come from each service's own feed setup script, and are fixed here:
a master can only switch them on or off, never point a slave somewhere else.

FlightRadar24 and FlightAware need their own programs (fr24feed, piaware). For
those, readsb can offer Beast data on a port bound to 127.0.0.1 only.

AIS is sent by AIS-catcher over UDP to the address each service assigns.
"""
from __future__ import annotations

import os
import re
import socket
import uuid

# key: (label, net-connector without the uuid). Primary host,port,protocol, then a failover host,port.
ADSB_AGGREGATORS = {
    # From ADSBexchange/feedclient configure.sh.
    "adsbx": ("ADS-B Exchange", "feed1.adsbexchange.com,30004,beast_reduce_out,feed2.adsbexchange.com,64004"),
    # From airplanes-live/feed configure.sh.
    "airplanes_live": ("airplanes.live", "feed.airplanes.live,30004,beast_reduce_plus_out,feed.airplanes.live,64004"),
    # From the adsblol/feed README, which adds the feeder's uuid to the connector.
    "adsb_lol": ("adsb.lol", "feed.adsb.lol,30004,beast_reduce_plus_out,in.adsb.lol,1337"),
    # From adsbfi/adsb-fi-scripts configure.sh.
    "adsb_fi": ("adsb.fi", "feed.adsb.fi,30004,beast_reduce_plus_out,feed.adsb.fi,64004"),
}
UUID_IN_CONNECTOR = {"adsb_lol"}

AIS_SERVICES = {"marinetraffic": "MarineTraffic", "aishub": "AISHub", "vesselfinder": "VesselFinder"}
MAX_AIS_DESTINATIONS = 6

HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
DEFAULT_BEAST_PORT = 30005


class FeedError(ValueError):
    """A feed setting that can't be used. The message is shown to the user."""


def _port(value, low: int = 1) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise FeedError("A port must be a number") from None
    if not low <= port <= 65535:
        raise FeedError(f"A port must be between {low} and 65535")
    return port


def clean(kind: str, raw) -> dict:
    """Check a target's feed settings. They also arrive at slaves over the network."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise FeedError("Feed settings must be an object")
    if kind == "adsb":
        aggregators = sorted(set(raw.get("aggregators") or []))
        unknown = [a for a in aggregators if a not in ADSB_AGGREGATORS]
        if unknown:
            raise FeedError(f"Unknown aggregator: {', '.join(map(str, unknown))}")
        beast = raw.get("beast_port")
        # Above 1024: the service runs unprivileged, and low ports are other services' anyway.
        return {"aggregators": aggregators, "beast_port": None if beast in (None, "") else _port(beast, 1025)}
    if kind == "ais":
        destinations = []
        for item in list(raw.get("udp") or [])[:MAX_AIS_DESTINATIONS + 1]:
            if not isinstance(item, dict) or item.get("service") not in AIS_SERVICES:
                raise FeedError("Unknown AIS service")
            host = str(item.get("host") or "").strip()
            if not HOST_RE.match(host):
                raise FeedError(f"{AIS_SERVICES[item['service']]} needs a host name or IP address")
            destinations.append({"service": item["service"], "host": host, "port": _port(item.get("port"))})
        if len(destinations) > MAX_AIS_DESTINATIONS:
            raise FeedError(f"At most {MAX_AIS_DESTINATIONS} AIS destinations")
        return {"community": bool(raw.get("community")), "udp": destinations}
    if raw and any(raw.values()):
        raise FeedError("Only ADS-B and AIS targets can feed other services")
    return {}


def valid_uuid(value) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        return None


def readsb_args(feeds: dict, feeder_uuid: str | None) -> list[str]:
    """Outbound connections, plus the local-only Beast port. Nothing listens on the network."""
    aggregators = feeds.get("aggregators") or []
    beast = feeds.get("beast_port")
    if not aggregators and not beast:
        return []
    # Every listening port defaults to 0 (off); bind to loopback in case one is ever on.
    args = ["--net", "--net-bind-address", "127.0.0.1", "--net-heartbeat", "60"]
    if feeder_uuid:
        args += ["--uuid", feeder_uuid]
    for key in aggregators:
        connector = ADSB_AGGREGATORS[key][1]
        if key in UUID_IN_CONNECTOR and feeder_uuid:
            connector += f",uuid={feeder_uuid}"
        args += ["--net-connector", connector]
    if beast:
        args += ["--net-bo-port", str(beast)]
    return args


def ais_args(feeds: dict) -> list[str]:
    args = []
    for destination in feeds.get("udp") or []:
        args += ["-u", destination["host"], str(destination["port"])]
    if feeds.get("community"):
        args.append("-X")
    return args


def aggregator_hosts(key: str) -> list[tuple[str, int]]:
    parts = ADSB_AGGREGATORS[key][1].split(",")
    return [(parts[0], int(parts[1])), (parts[3], int(parts[4]))]


def resolve(hosts: list[tuple[str, int]]) -> set[str]:
    addresses = set()
    for host, port in hosts:
        try:
            for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
                addresses.add(info[4][0])
        except OSError:
            continue
    return addresses


def _socket_inodes(pid: int) -> set[str]:
    inodes = set()
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
    except OSError:
        pass
    return inodes


def _decode_address(text: str) -> tuple[str, int]:
    host, port = text.split(":")
    raw = bytes.fromhex(host)
    if len(raw) == 4:
        address = socket.inet_ntop(socket.AF_INET, raw[::-1])
    else:
        words = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
        address = socket.inet_ntop(socket.AF_INET6, words)
        if address.startswith("::ffff:"):
            address = address[7:]
    return address, int(port, 16)


def established(pid: int) -> list[tuple[tuple[str, int], tuple[str, int]]]:
    """(local, remote) addresses of a process's established TCP connections. Linux only; empty elsewhere."""
    inodes = _socket_inodes(pid)
    found = []
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table) as handle:
                next(handle, None)
                for line in handle:
                    fields = line.split()
                    if len(fields) > 9 and fields[3] == "01" and fields[9] in inodes:
                        found.append((_decode_address(fields[1]), _decode_address(fields[2])))
        except OSError:
            continue
    return found
