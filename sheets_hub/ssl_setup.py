from __future__ import annotations

import os
import sys


def configure_tls() -> None:
    try:
        import certifi
    except Exception:
        return
    ca = certifi.where()
    if getattr(sys, "frozen", False):
        bundled = os.path.join(getattr(sys, "_MEIPASS", ""), "certifi", "cacert.pem")
        if os.path.exists(bundled):
            ca = bundled
    os.environ["SSL_CERT_FILE"] = ca
    os.environ["REQUESTS_CA_BUNDLE"] = ca
    os.environ["CURL_CA_BUNDLE"] = ca
