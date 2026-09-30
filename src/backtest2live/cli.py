"""`bt2live` command line.

Exit codes: 0/1/2 carry the verdict of the evaluator that ran (see each
subcommand's help); 3 means the run failed (bad input or usage), so a failed
run never reads as a verdict in CI.
"""

import argparse
import sys

from backtest2live import __version__, convergence

EXIT_ERROR = 3


class _Parser(argparse.ArgumentParser):
    # argparse exits 2 on a usage error, which would read as KILL.
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_ERROR, f"{self.prog}: error: {message}\n")


def build_parser():
    p = _Parser(
        prog="bt2live",
        description="Check a backtest against what live execution would do.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<command>", parser_class=_Parser)
    sub.required = True

    c = sub.add_parser(
        "convergence",
        help="Variant-sweep convergence: CONVERGED / ITERATE / KILL",
        description="Convergence analysis for variant backtest sweeps. "
                    "Exit code: 0 CONVERGED, 1 ITERATE, 2 KILL, 3 error.",
    )
    convergence.add_arguments(c)
    c.set_defaults(run=convergence.run)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except SystemExit as e:
        # Evaluators raise SystemExit("error: ...") on bad input.
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            return EXIT_ERROR
        raise
    except Exception as e:  # a crash must not read as a verdict
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
