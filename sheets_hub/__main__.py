from sheets_hub.ssl_setup import configure_tls

configure_tls()

from sheets_hub.app import main

if __name__ == "__main__":
    main()
