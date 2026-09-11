from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "--worker":
        from landscape_culler.cli import main as worker_main

        sys.argv = [sys.argv[0], *sys.argv[2:]]
        worker_main()
        return

    from landscape_culler.web import main as web_main

    web_main()


if __name__ == "__main__":
    main()
