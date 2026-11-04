"""Compare two run measurements.

    python3 -m prwarden.compare_metrics before.json after.json

Produce the inputs with `--metrics PATH` on two runs over the same pull requests,
one before a prompt, model, or setting change and one after. The numbers that moved
are listed first.

A comparison is evidence about the numbers, not about quality: two runs over
different pull requests, or over heads that moved in between, compare nothing. The
report names the engine and model of each side so an accidental mismatch is visible.
"""

import sys

from .metrics import compare, load


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2:
        print(__doc__.strip())
        return 2
    try:
        before = load(arguments[0])
        after = load(arguments[1])
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(compare(before, after))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
