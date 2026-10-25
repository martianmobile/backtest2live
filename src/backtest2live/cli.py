"""`bt2live` command line.

Exit codes: 0/1/2 carry the verdict of the evaluator that ran (see each
subcommand's help); 3 means the run failed (bad input or usage), so a failed
run never reads as a verdict in CI.
"""

import argparse
import sys

from backtest2live import __version__, convergence, costgap, fillgap, importers, mcp, preflight

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

    f = sub.add_parser(
        "fill-gap",
        help="Your backtest's passive fills vs a queue proxy on market data",
        description="Re-run your backtest's resting orders under a queue proxy on top-of-book and trades. "
                    "Exit code: 0 CONSISTENT, 1 OVERSTATED, 3 error.",
    )
    fillgap.add_arguments(f)
    f.set_defaults(run=fillgap.run)

    g = sub.add_parser(
        "cost-gap",
        help="Your backtest's taker orders re-priced on the real book",
        description="Re-price market orders and marketable limits against top-of-book at arrival, with latency. "
                    "Exit code: 0 CONSISTENT or REPORTED, 1 OVERSTATED, 3 error.",
    )
    costgap.add_arguments(g)
    g.set_defaults(run=costgap.run)

    i = sub.add_parser(
        "import",
        help="Convert an engine's export (Nautilus, LEAN, backtrader, vectorbt, freqtrade, Hummingbot) "
             "into the order-log schema",
        description="Write a backtest engine's native order or trade export as the order log fill-gap and "
                    "cost-gap read. Exit code: 0 written, 3 error.",
    )
    importers.add_arguments(i)
    i.set_defaults(run=importers.run)

    m = sub.add_parser(
        "mcp",
        help="Serve the checks to an AI assistant over MCP (stdio)",
        description="Model Context Protocol server on stdin/stdout: tools convergence, fill_gap, cost_gap. "
                    "Start it from an MCP client's config; it runs until stdin closes.",
    )
    mcp.add_arguments(m)
    m.set_defaults(run=mcp.run)

    pf = sub.add_parser(
        "preflight",
        help="Every available check in one report: out-of-sample, execution realism, capacity, risk",
        description="Run convergence, fill-gap and cost-gap on what you pass and write one report, each "
                    "section pass / warn / fail / not run. Exit code: 0 pass, 1 warn, 2 fail, 3 error or no check ran.",
    )
    preflight.add_arguments(pf)
    pf.set_defaults(run=preflight.run)
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
