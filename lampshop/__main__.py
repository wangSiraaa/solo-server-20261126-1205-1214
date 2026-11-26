"""Entry point: ``python -m lampshop`` runs the HTTP API."""

from __future__ import annotations

import argparse
import os

from .api import make_server
from .db import Database
from .service import DomainService


def main(argv=None):
    parser = argparse.ArgumentParser(description="Lampshade workshop backend")
    parser.add_argument("--host", default=os.environ.get("LAMPSHOP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("LAMPSHOP_PORT", "8080")))
    parser.add_argument("--db", default=os.environ.get("LAMPSHOP_DB", "lampshop.db"))
    args = parser.parse_args(argv)

    db = Database(args.db)
    svc = DomainService(db)
    httpd = make_server(svc, args.host, args.port)
    host, port = httpd.server_address
    print(f"lampshop listening on http://{host}:{port} (db={args.db}, regime={svc.get_regime()})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
