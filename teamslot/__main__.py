import argparse
import logging

from .server import TeamSlotServer
from .service import Scheduler


def main():
    parser = argparse.ArgumentParser(description="Run the TeamSlot local scheduling demo.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--database", default="data/teamslot.db")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    scheduler = Scheduler(arguments.database)
    with TeamSlotServer((arguments.host, arguments.port), scheduler) as server:
        print(f"TeamSlot: http://{arguments.host}:{server.server_port}", flush=True)
        print("Local portfolio demo. Do not expose this server directly to the internet.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
