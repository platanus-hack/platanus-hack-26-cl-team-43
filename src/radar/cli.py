import argparse

from radar import db
from radar.settings import Settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="radar")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply pending SQL migrations")
    sub.add_parser("seed-corpus", help="load the known in-the-wild jailbreak corpus")
    args = parser.parse_args(argv)

    if args.command == "migrate":
        conn = db.connect(Settings().database_url)
        db.migrate(conn)
        conn.close()
        print("migrations applied")
    elif args.command == "seed-corpus":
        from radar.pipeline import seed_corpus

        print(f"seed corpus: {seed_corpus.run()} prompts ingested")


if __name__ == "__main__":
    main()
