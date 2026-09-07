from sheets_hub.ssl_setup import configure_tls

configure_tls()

if __name__ == "__main__":
    import os
    import sys

    # web — браузер (по умолчанию из исходников);
    # desktop — окно pywebview (по умолчанию в .exe);
    # ctk — старый CustomTkinter.
    mode = (os.environ.get("SHEETS_HUB_UI") or "").strip().lower()
    if not mode:
        mode = "desktop" if getattr(sys, "frozen", False) else "web"
    if mode in {"ctk", "tk", "desktop-ctk"}:
        from sheets_hub.app import main as ctk_main

        ctk_main()
    elif mode in {"desktop", "webview", "pywebview"}:
        from sheets_hub.web_main import main as web_main

        web_main()
    else:
        from sheets_hub.web_server import main as server_main

        server_main()
