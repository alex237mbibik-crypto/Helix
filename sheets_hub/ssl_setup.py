from __future__ import annotations

import os
import ssl
import sys
from pathlib import Path


def ca_bundle_path() -> str:
    try:
        import certifi

        certifi_path = certifi.where()
    except Exception:
        certifi_path = ""

    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", "") or "")
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend(
            [
                meipass / "certifi" / "cacert.pem",
                exe_dir / "_internal" / "certifi" / "cacert.pem",
                exe_dir / "certifi" / "cacert.pem",
                exe_dir / "cacert.pem",
            ]
        )
    if certifi_path:
        candidates.append(Path(certifi_path))

    for path in candidates:
        try:
            if path.is_file() and path.stat().st_size > 10_000:
                return str(path)
        except OSError:
            continue
    return certifi_path


def configure_tls() -> None:
    ca = ca_bundle_path()
    if not ca:
        return
    os.environ["SSL_CERT_FILE"] = ca
    os.environ["REQUESTS_CA_BUNDLE"] = ca
    os.environ["CURL_CA_BUNDLE"] = ca
    os.environ["GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"] = ca
    try:
        ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=ca)
    except Exception:
        pass
