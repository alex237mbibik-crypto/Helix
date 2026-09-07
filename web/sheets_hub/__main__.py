"""Sheets Hub Web — только браузерный интерфейс."""

from sheets_hub.ssl_setup import configure_tls

configure_tls()

if __name__ == "__main__":
    from sheets_hub.web_server import main

    main()
