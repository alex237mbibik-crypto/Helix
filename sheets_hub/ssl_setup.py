from __future__ import annotations

import os
import ssl
import sys
from pathlib import Path

_tls_configured = False
_os_trust_available: bool | None = None


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


def _inject_os_trust_store() -> bool:
    global _os_trust_available
    if _os_trust_available is not None:
        return _os_trust_available
    if sys.platform not in ("win32", "darwin"):
        _os_trust_available = False
        return False
    try:
        import truststore

        truststore.inject_into_ssl()
        _os_trust_available = True
    except Exception:
        _os_trust_available = False
    return _os_trust_available


def session_verify_target() -> bool | str:
    """What to pass as requests.Session.verify."""
    if _inject_os_trust_store():
        return True
    ca = ca_bundle_path()
    return ca or True


def configure_tls() -> None:
    global _tls_configured
    if _tls_configured:
        return
    _tls_configured = True

    os_trust = _inject_os_trust_store()
    ca = ca_bundle_path()
    if ca:
        os.environ["SSL_CERT_FILE"] = ca
        os.environ["REQUESTS_CA_BUNDLE"] = ca
        os.environ["CURL_CA_BUNDLE"] = ca
        os.environ["GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"] = ca

    try:
        if os_trust:
            ssl._create_default_https_context = ssl.create_default_context
        elif ca:
            ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=ca)
    except Exception:
        pass
