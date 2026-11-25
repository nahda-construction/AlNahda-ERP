"""Strict CONNECT-only policy; synthetic pilot, not an authenticated worker."""
import asyncio
import ipaddress
import re
import socket

ALLOWED = frozenset({"auth.openai.com", "chatgpt.com"})
MAX_HEADER = 8192
MAX_BYTES = 8 * 1024 * 1024
TIMEOUT = 30

def parse_header(raw):
    if len(raw) > MAX_HEADER or not raw.endswith(b"\r\n\r\n"):
        raise ValueError("invalid header")
    lines = raw.decode("ascii").split("\r\n")
    m = re.fullmatch(r"CONNECT ([a-z0-9.-]+):443 HTTP/1\.[01]", lines[0])
    if not m or m[1] not in ALLOWED:
        raise ValueError("destination denied")
    host = m[1]
    seen = set()
    for line in lines[1:-2]:
        key, sep, val = line.partition(":")
        key = key.lower()
        if not sep or key in seen or not re.fullmatch(r"[a-z0-9-]+", key):
            raise ValueError("invalid field")
        seen.add(key)
        if key in ("content-length", "transfer-encoding", "proxy-authorization"):
            raise ValueError("body or credentials denied")
        if key == "host" and val.strip() != host + ":443":
            raise ValueError("host mismatch")
        if any(ord(c) < 32 and c != "\t" or ord(c) == 127 for c in val):
            raise ValueError("invalid value")
    return host

def public_ip(value):
    ip = ipaddress.ip_address(value)
    return ip.version == 4 and ip.is_global and not ip.is_multicast and not ip.is_reserved

async def connect_public(host):
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(i[4][0] for i in infos))
    if not addresses or not all(public_ip(a) for a in addresses):
        raise ValueError("DNS destination denied")
    # Connect to the vetted literal, never resolve the hostname twice.
    return await asyncio.open_connection(addresses[0], 443, family=socket.AF_INET)

async def relay(reader, writer):
    total = 0
    while True:
        data = await reader.read(16384)
        if not data:
            return
        total += len(data)
        if total > MAX_BYTES:
            raise ValueError("tunnel byte limit")
        writer.write(data)
        await writer.drain()

async def handle(reader, writer, connector=connect_public):
    upstream = None
    tasks = []
    established = False
    try:
        async with asyncio.timeout(TIMEOUT):
            raw = await reader.readuntil(b"\r\n\r\n")
            host = parse_header(raw)
            other, upstream = await connector(host)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            established = True
            tasks = [asyncio.create_task(relay(reader, upstream)),
                     asyncio.create_task(relay(other, writer))]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (ValueError, OSError, TimeoutError, asyncio.IncompleteReadError,
            asyncio.LimitOverrunError):
        if not established:
            writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for w in (upstream, writer):
            if w is not None:
                w.close()

# Deliberately no CLI listener yet. Only callable from credential-free tests.
