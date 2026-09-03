from sheets_hub.ssl_setup import configure_tls

configure_tls()

if __name__ == "__main__":
    import os

    if (os.environ.get("SHEETS_HUB_UI") or "").strip().lower() == "ctk":
        from sheets_hub.app import main as ctk_main

        ctk_main()
    else:
        from sheets_hub.web_main import main as web_main

        web_main()
