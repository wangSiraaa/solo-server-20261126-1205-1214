"""Run the changeover HTTP service.

    python3 -m changeover.serve --port 8080 --store /var/lib/changeover/events.jsonl

State is durable: every request appends to the JSONL log and is replayed on
start-up, so restarting reproduces exactly what visitors could walk before.
"""

import argparse
import sys

from .engine import build_engine
from .server import make_server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Shared-office changeover backend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--store", default="/tmp/changeover-events.jsonl")
    args = parser.parse_args(argv)

    engine = build_engine(args.store)
    httpd = make_server(engine, host=args.host, port=args.port)
    host, port = httpd.server_address[:2]
    print("changeover service on http://%s:%s  (store=%s)" % (host, port, args.store), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        engine.store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
