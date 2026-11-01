"""``kindle-dash-server`` / ``python -m kindle_dash_server``: run the server."""

from __future__ import annotations

import argparse
import logging
import os


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("KDS_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("KDS_PORT", "8080")))
    parser.add_argument("--data-dir", help="data volume (default: $KDS_DATA_DIR or /data)")
    args = parser.parse_args()
    if args.data_dir:
        os.environ["KDS_DATA_DIR"] = args.data_dir

    import uvicorn

    from .config import Env
    from .web.app import create_app

    env = Env()
    logging.basicConfig(
        level=env.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    uvicorn.run(
        create_app(env),
        host=args.host,
        port=args.port,
        log_level=env.log_level.lower(),
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
