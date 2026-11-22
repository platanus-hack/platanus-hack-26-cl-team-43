import argparse
import logging
import os
import sys
from types import SimpleNamespace

from dotenv import load_dotenv

from radar import db

COLLECTOR_NAMES = ["github_discovery", "github_watchlist", "github_code_search",
                   "hf_datasets", "papers"]


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"{name} is not set")
    return value


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="radar")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply pending SQL migrations")
    sub.add_parser("seed-corpus", help="load the known in-the-wild jailbreak corpus")
    poll = sub.add_parser("poll", help="run one collector once and print the inserted count")
    poll.add_argument("name", choices=COLLECTOR_NAMES)
    sub.add_parser("run", help="scheduler + tester + web server (needs every setting)")
    args = parser.parse_args(argv)

    # only `run` builds the full Settings; the other commands read just what they need
    load_dotenv()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "migrate":
        conn = db.connect(_require("DATABASE_URL"))
        db.migrate(conn)
        conn.close()
        print("migrations applied")
    elif args.command == "seed-corpus":
        from radar.pipeline import seed_corpus

        n = seed_corpus.run(_require("DATABASE_URL"), os.environ.get("HF_TOKEN") or None)
        print(f"seed corpus: {n} prompts ingested")
    elif args.command == "poll":
        from radar import main as wiring

        github = args.name.startswith("github_")
        creds = SimpleNamespace(
            github_token=_require("GITHUB_TOKEN") if github else os.environ.get("GITHUB_TOKEN", ""),
            hf_token=os.environ.get("HF_TOKEN") or None,
        )
        conn = db.connect(_require("DATABASE_URL"))
        try:
            n = wiring.poll_once(conn, creds, wiring.load_sources(), args.name)
        finally:
            conn.close()
        print(f"{args.name}: {n} new raw items")
    elif args.command == "run":
        from radar import main as wiring
        from radar.settings import Settings

        wiring.run(Settings())


if __name__ == "__main__":
    main()
