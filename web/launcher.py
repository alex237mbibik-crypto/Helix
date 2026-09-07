#!/usr/bin/env python3
"""Запуск Sheets Hub Web."""

from sheets_hub.ssl_setup import configure_tls

configure_tls()

from sheets_hub.web_server import main

if __name__ == "__main__":
    main()
