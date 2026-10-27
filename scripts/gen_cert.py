"""生成局域网 HTTPS 自签证书（给手机 / 其他设备调用摄像头用）。

为什么需要：浏览器只在「安全上下文」里允许 getUserMedia，
http://192.168.x.x 不是安全上下文，摄像头会直接不可用；https 才是。

用法：
    python scripts/gen_cert.py                 # 自动带上本机局域网 IP / localhost
    python scripts/gen_cert.py --host 192.168.2.100 --host facepass.local
    python scripts/gen_cert.py --force         # 覆盖已有证书

生成后把下面两行写进 .env，重启服务即可：
    SSL_CERTFILE=certs/dev-cert.pem
    SSL_KEYFILE=certs/dev-key.pem
    HTTPS_PORT=8443
"""
from __future__ import annotations

import argparse
import ipaddress
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.netinfo import lan_ipv4_addresses  # noqa: E402


def build_sans(extra_hosts):
    names = {"localhost", socket.gethostname()}
    ips = {"127.0.0.1", "::1"}
    ips.update(lan_ipv4_addresses())

    for host in extra_hosts:
        host = host.strip()
        if not host:
            continue
        try:
            ips.add(str(ipaddress.ip_address(host)))
        except ValueError:
            names.add(host)

    sans = []
    for name in sorted(names):
        sans.append(x509.DNSName(name))
    for ip in sorted(ips):
        sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
    return sans, sorted(names), sorted(ips)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 FacePass 局域网自签证书")
    parser.add_argument("--host", action="append", default=[], help="额外域名或 IP，可重复")
    parser.add_argument("--days", type=int, default=825, help="有效期（天）")
    parser.add_argument("--force", action="store_true", help="覆盖已有证书")
    parser.add_argument("--out-dir", default="certs", help="输出目录")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    cert_path = out_dir / "dev-cert.pem"
    key_path = out_dir / "dev-key.pem"

    if (cert_path.exists() or key_path.exists()) and not args.force:
        print(f"[提示] 证书已存在：{cert_path}")
        print("       如需重新生成，请加 --force")
        return 0

    sans, names, ips = build_sans(args.host)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FacePass Dev"),
            x509.NameAttribute(NameOID.COMMON_NAME, "FacePass Dev Certificate"),
        ]
    )
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=args.days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))

    print("=" * 66)
    print("证书已生成")
    print(f"  证书：{cert_path}")
    print(f"  私钥：{key_path}")
    print(f"  有效期：{args.days} 天")
    print(f"  域名：{'、'.join(names)}")
    print(f"  IP：{'、'.join(ips)}")
    print("=" * 66)
    print("把下面几行加入 .env，然后重启服务：")
    print("  SSL_CERTFILE=certs/dev-cert.pem")
    print("  SSL_KEYFILE=certs/dev-key.pem")
    print("  HTTPS_PORT=8443")
    print()
    print("手机访问 https://<上面的IP>:8443 ，浏览器提示证书不受信任时选择「继续前往」。")
    print("如果手机系统强制要求可信证书，请改用 mkcert 生成并在手机上安装根证书。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())
