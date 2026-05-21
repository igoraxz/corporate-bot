#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Inner proxy forwarder for sandbox network bridge.

Runs INSIDE the bwrap sandbox. Bridges localhost:3128 (TCP) to a Unix
domain socket (the external proxy). This lets HTTP_PROXY=http://localhost:3128
work for curl/pip/urllib.

Usage: sandbox-inner-forwarder.py <unix_socket_path>
"""
import os
import socket
import sys
import threading


def relay(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
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


def handle(client: socket.socket, sock_path: str) -> None:
    try:
        unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        unix.connect(sock_path)
        t1 = threading.Thread(target=relay, args=(client, unix), daemon=True)
        t2 = threading.Thread(target=relay, args=(unix, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    except OSError:
        pass
    finally:
        try:
            unix.close()
        except Exception:
            pass
        client.close()


def main() -> None:
    sock_path = sys.argv[1]
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 3128))
    srv.listen(32)
    # Signal ready
    open("/tmp/.proxy_inner_ready", "w").close()
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c, sock_path), daemon=True).start()


if __name__ == "__main__":
    main()
