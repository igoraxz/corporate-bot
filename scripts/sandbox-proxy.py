#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""HTTP proxy for sandbox network isolation.

Listens on a Unix domain socket and forwards HTTP/HTTPS requests to the
public internet. Enforces port-level filtering: only allowed destination
ports are forwarded. All other connections are rejected.

Used by sandbox-exec.sh to provide controlled internet access inside
bwrap's --unshare-net namespace.

Architecture:
  Inside bwrap (no network):
    curl/pip/python → HTTP_PROXY=http://localhost:3128 →
    inner forwarder (localhost:3128 → Unix socket) →
    [Unix socket bind-mounted from outside] →
  Outside bwrap (full network):
    This proxy (Unix socket listener) → real TCP connection → internet

Port filtering:
  Only TCP connections to ports in ALLOWED_PORTS are forwarded.
  DNS (UDP 53) is handled by the proxy resolving hostnames on behalf
  of the client — the sandboxed process never does DNS directly.
"""

import ipaddress
import os
import signal
import socket
import sys
import threading

# Allowed destination ports (TCP only)
ALLOWED_PORTS = {80, 443}

# Connection timeout (seconds)
CONNECT_TIMEOUT = 15

# Relay buffer size
BUFFER_SIZE = 65536

# Blocked destination IP ranges (private networks, loopback, cloud metadata)
# Prevents SSRF via DNS rebinding / internal network access
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),       # Docker internal networks
    ipaddress.ip_network("172.16.0.0/12"),     # Docker bridge networks
    ipaddress.ip_network("192.168.0.0/16"),    # Private LAN
    ipaddress.ip_network("127.0.0.0/8"),       # Loopback
    ipaddress.ip_network("169.254.0.0/16"),    # Link-local / cloud metadata (AWS/GCP/Azure)
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
]


def _resolve_and_check(host: str, port: int) -> str | None:
    """Resolve hostname and check SSRF. Returns IP to connect to, or None if blocked.

    Resolves once and returns the IP — caller connects to the IP directly,
    preventing TOCTOU DNS rebinding attacks.
    """
    try:
        for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
            addr = info[4][0]
            ip = ipaddress.ip_address(addr)
            if any(ip in net for net in _BLOCKED_NETWORKS):
                return None
            return addr  # Return first non-blocked resolved IP
    except socket.gaierror:
        return None  # Unresolvable hostname = block
    return None


def relay(src: socket.socket, dst: socket.socket) -> None:
    """Relay data between two sockets until one closes."""
    try:
        while True:
            data = src.recv(BUFFER_SIZE)
            if not data:
                break
            dst.sendall(data)
    except (OSError, BrokenPipeError):
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle_connect(client: socket.socket, host: str, port: int) -> None:
    """Handle HTTP CONNECT tunnel (for HTTPS)."""
    if port not in ALLOWED_PORTS:
        client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        return

    resolved_ip = _resolve_and_check(host, port)
    if resolved_ip is None:
        client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        return

    try:
        remote = socket.create_connection((resolved_ip, port), timeout=CONNECT_TIMEOUT)
    except (OSError, TimeoutError):
        client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 21\r\n\r\nConnection refused.\r\n")
        return

    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

    # Bidirectional relay
    t1 = threading.Thread(target=relay, args=(client, remote), daemon=True)
    t2 = threading.Thread(target=relay, args=(remote, client), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    remote.close()


def handle_http(client: socket.socket, method: str, url: str, rest: bytes) -> None:
    """Handle plain HTTP proxy request (non-CONNECT)."""
    # Parse host:port from URL
    if url.startswith("http://"):
        url = url[7:]
    slash = url.find("/")
    if slash == -1:
        hostport = url
        path = "/"
    else:
        hostport = url[:slash]
        path = url[slash:]

    if ":" in hostport:
        host, port_str = hostport.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            port = 80
    else:
        host = hostport
        port = 80

    if port not in ALLOWED_PORTS:
        client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        return

    resolved_ip = _resolve_and_check(host, port)
    if resolved_ip is None:
        client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        return

    try:
        remote = socket.create_connection((resolved_ip, port), timeout=CONNECT_TIMEOUT)
    except (OSError, TimeoutError):
        client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 21\r\n\r\nConnection refused.\r\n")
        return

    # Forward the request — strip original Host header to avoid duplicates
    if rest:
        lines = rest.split(b"\r\n")
        lines = [l for l in lines if not l.lower().startswith(b"host:")]
        rest = b"\r\n".join(lines)
    request = f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n".encode()
    # Append remaining headers from original request
    if rest:
        # rest contains everything after the first line
        request += rest
    remote.sendall(request)

    # Relay response back
    relay(remote, client)
    remote.close()


def handle_client(client: socket.socket) -> None:
    """Handle one proxy client connection."""
    try:
        client.settimeout(30)
        data = b""
        while b"\r\n" not in data and len(data) < 8192:
            chunk = client.recv(4096)
            if not chunk:
                return
            data += chunk

        first_line_end = data.index(b"\r\n")
        first_line = data[:first_line_end].decode("utf-8", errors="replace")
        rest = data[first_line_end + 2:]

        parts = first_line.split(" ", 2)
        if len(parts) < 2:
            client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return

        method = parts[0].upper()
        target = parts[1]

        if method == "CONNECT":
            # CONNECT host:port HTTP/1.1
            if ":" in target:
                host, port_str = target.rsplit(":", 1)
                try:
                    port = int(port_str)
                except ValueError:
                    port = 443
            else:
                host = target
                port = 443
            # Read remaining headers (consume until empty line)
            while b"\r\n\r\n" not in rest and len(rest) < 8192:
                chunk = client.recv(4096)
                if not chunk:
                    break
                rest += chunk
            handle_connect(client, host, port)
        else:
            handle_http(client, method, target, rest)
    except Exception:
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass


def main() -> None:
    """Start the proxy on a Unix domain socket."""
    if len(sys.argv) < 2:
        print("Usage: sandbox-proxy.py <socket_path> [ready_signal_path]", file=sys.stderr)
        sys.exit(1)

    sock_path = sys.argv[1]
    ready_path = sys.argv[2] if len(sys.argv) > 2 else None

    # Clean up stale socket
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    os.chmod(sock_path, 0o777)  # Accessible from bwrap's user namespace
    server.listen(32)

    # Signal readiness
    if ready_path:
        with open(ready_path, "w") as f:
            f.write(str(os.getpid()))

    # Graceful shutdown on SIGTERM
    def shutdown(signum, frame):
        server.close()
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while True:
        try:
            client, _ = server.accept()
            threading.Thread(target=handle_client, args=(client,), daemon=True).start()
        except OSError:
            break


if __name__ == "__main__":
    main()
