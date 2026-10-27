"""网络信息小工具。"""
from __future__ import annotations

import socket


def lan_ipv4_addresses() -> list:
    """尽量只返回当前上网网卡的 IPv4（避免把虚拟网卡都列出来）。"""
    ips = set()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.3)
        sock.connect(("8.8.8.8", 80))
        ips.add(sock.getsockname()[0])
        sock.close()
    except OSError:
        pass
    if not ips:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith("127."):
                    ips.add(ip)
        except OSError:
            pass
    return sorted(ips)
