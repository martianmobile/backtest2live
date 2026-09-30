"""
backtest2live · convergence — variant-sweep convergence analyzer.

Reads a table of variant backtest results (one row per variant), measures
convergence on a chosen metric, and emits a verdict: CONVERGED / ITERATE / KILL.

Pure Python 3 standard library — no third-party dependencies, no pip install.
CSV in, Markdown report out. For Parquet/SQLite inputs, export to CSV first.

Usage:
    bt2live convergence results.csv --metric sharpe_oos
    bt2live convergence results.csv --metric sharpe --top-k 5 --save
    bt2live convergence results.csv --json

The math is deterministic and lives here. The qualitative framing of an
ITERATE result (which next variants to try) is left to the calling agent.
"""

import csv
import json
import math
import statistics
import sys
from datetime import datetime

# --- column-role heuristics ------------------------------------------------

ID_KEYWORDS = ("variant", "run", "config", "params", "param", "id", "name", "trial")
METRIC_KEYWORDS = (
    "sharpe", "sortino", "calmar", "pnl", "profit", "return", "ret", "win",
    "hit", "drawdown", "dd", "mdd", "loss", "score", "metric", "alpha", "ir",
    "pf", "expectancy", "cagr",
)
SAMPLE_KEYWORDS = (
    "n_trades", "num_trades", "trades", "n_samples", "samples", "n_obs",
    "nobs", "count", "n",
)
LOWER_BETTER_KEYWORDS = ("drawdown", "dd", "mdd", "loss", "vol", "variance", "stdev", "std")

OOS_SUFFIXES = ("_oos", "_out", "_test", "_val", "_holdout", "_oss")
IS_SUFFIXES = ("_is", "_in", "_train", "_insample", "_ins")


def _to_float(s):
    if s is None:
        return None
    s = s.strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def read_table(path):
    """Return (headers, rows) where rows is a list of dict[str, str]."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            headers = reader.fieldnames
            if not headers:
                raise SystemExit(f"error: no header row found in {path!r}")
            rows = [r for r in reader]
    except FileNotFoundError:
        raise SystemExit(f"error: file not found: {path!r}")
    except OSError as e:
        raise SystemExit(f"error: could not read {path!r}: {e}")
    if not rows:
        raise SystemExit(f"error: no data rows in {path!r}")
    return headers, rows


def numeric_columns(headers, rows):
    """Columns where every non-empty cell parses as a float."""
    out = []
    for h in headers:
        vals = [_to_float(r.get(h)) for r in rows]
        present = [v for v in vals if v is not None]
        if present and all(_to_float(r.get(h)) is not None or (r.get(h) or "").strip() == "" for r in rows):
            out.append(h)
    return out


def pick_id_column(headers, numeric):
    non_numeric = [h for h in headers if h not in numeric]
    for h in headers:
        if any(k in h.lower() for k in ID_KEYWORDS):
            return h
    if non_numeric:
        return non_numeric[0]
    return headers[0]


def pick_sample_column(numeric):
    # Prefer the most specific match (longest keyword hit) to avoid grabbing a
    # bare param named "n" before a real "n_trades".
    best, best_len = None, 0
    for h in numeric:
        for k in SAMPLE_KEYWORDS:
            if k in h.lower() and len(k) > best_len:
                best, best_len = h, len(k)
    return best


def _strip_suffix(name):
    low = name.lower()
    for suf in OOS_SUFFIXES + IS_SUFFIXES:
        if low.endswith(suf):
            return name[: -len(suf)], suf
    return name, ""


def resolve_metrics(numeric, requested):
    """Return (rank_col, is_col, oos_col, base)."""
    if not numeric:
        raise SystemExit("error: no numeric metric columns detected")

    if requested:
        if requested in numeric:
            rank_col = requested
        else:
            cand = [c for c in numeric if c == requested or c.lower().startswith(requested.lower() + "_")]
            oos = [c for c in cand if c.lower().endswith(OOS_SUFFIXES)]
            if not cand:
                raise SystemExit(
                    f"error: metric {requested!r} not found. Numeric columns: {', '.join(numeric)}"
                )
            rank_col = oos[0] if oos else cand[0]
    else:
        metric_like = [c for c in numeric if any(k in c.lower() for k in METRIC_KEYWORDS)]
        oos = [c for c in metric_like if c.lower().endswith(OOS_SUFFIXES)]
        rank_col = oos[0] if oos else (metric_like[0] if metric_like else None)
        if rank_col is None:
            raise SystemExit(
                "error: could not auto-detect a metric. Pass --metric. "
                f"Numeric columns: {', '.join(numeric)}"
            )

    base, _ = _strip_suffix(rank_col)
    is_col = oos_col = None
    for c in numeric:
        b, suf = _strip_suffix(c)
        if b != base:
            continue
        if suf in IS_SUFFIXES:
            is_col = c
        elif suf in OOS_SUFFIXES:
            oos_col = c
    # If rank_col itself carries an IS/OOS suffix, slot it in.
    _, rank_suf = _strip_suffix(rank_col)
    if rank_suf in OOS_SUFFIXES:
        oos_col = rank_col
    elif rank_suf in IS_SUFFIXES:
        is_col = rank_col
    return rank_col, is_col, oos_col, base


def spearman(xs, ys):
    """Spearman rank correlation with average-rank tie handling. None if undefined."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 2:
        return None
    rx = _avg_ranks([p[0] for p in pairs])
    ry = _avg_ranks([p[1] for p in pairs])
    return _pearson(rx, ry)


def _avg_ranks(vals):
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # ranks are 1-based
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def relative_dispersion(vals):
    """std / |mean| over the values. None if < 2 values or mean ~ 0."""
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return None
    mean = statistics.mean(vals)
    sd = statistics.stdev(vals)
    if abs(mean) < 1e-12:
        return None
    return sd / abs(mean)


def parameter_check(top_rows, all_rows, param_cols, getf):
    """For each param, see whether the top-K occupy a contiguous slice of the
    swept grid (plateau) or are scattered (isolated). Also flag edge peaks."""
    findings = {}
    for p in param_cols:
        grid = sorted({getf(r, p) for r in all_rows if getf(r, p) is not None})
        if len(grid) <= 1:
            findings[p] = {"grid": grid, "contiguous": True, "edge": False, "occupied": grid}
            continue
        idx = {v: i for i, v in enumerate(grid)}
        occ_vals = sorted({getf(r, p) for r in top_rows if getf(r, p) is not None})
        occ_idx = sorted(idx[v] for v in occ_vals)
        span = occ_idx[-1] - occ_idx[0]
        contiguous = span <= (len(occ_idx) - 1) + 1  # allow one gap
        top1_val = getf(top_rows[0], p)
        edge = top1_val in (grid[0], grid[-1])
        findings[p] = {
            "grid": grid,
            "occupied": occ_vals,
            "contiguous": contiguous,
            "edge": edge,
            "top1": top1_val,
        }
    return findings


def fmt_num(x, nd=2):
    if x is None:
        return "n/a"
    return f"{x:.{nd}f}"


def fmt_pct(x, nd=1):
    if x is None:
        return "n/a"
    return f"{x * 100:.{nd}f}%"


def analyze(path, args):
    headers, rows = read_table(path)
    numeric = numeric_columns(headers, rows)
    id_col = args.id_column or pick_id_column(headers, numeric)
    sample_col = args.sample_column or pick_sample_column(numeric)
    rank_col, is_col, oos_col, base = resolve_metrics(numeric, args.metric)

    lower_better = (
        args.lower_is_better
        or (not args.higher_is_better and any(k in rank_col.lower() for k in LOWER_BETTER_KEYWORDS))
    )

    def getf(r, col):
        return _to_float(r.get(col)) if col else None

    ranked = [r for r in rows if getf(r, rank_col) is not None]
    if len(ranked) < 2:
        raise SystemExit(f"error: need >=2 variants with a value for {rank_col!r}; got {len(ranked)}")
    ranked.sort(key=lambda r: getf(r, rank_col), reverse=not lower_better)

    k = min(args.top_k, len(ranked))
    top = ranked[:k]

    # --- metrics ---
    disp_rank = relative_dispersion([getf(r, rank_col) for r in top])
    disp_is = relative_dispersion([getf(r, is_col) for r in top]) if is_col else None
    disp_oos = relative_dispersion([getf(r, oos_col) for r in top]) if oos_col else None

    rho = None
    if is_col and oos_col:
        rho = spearman([getf(r, is_col) for r in ranked], [getf(r, oos_col) for r in ranked])

    all_rank_vals = [getf(r, rank_col) for r in ranked]
    median_all = statistics.median(all_rank_vals)
    stdev_all = statistics.pstdev(all_rank_vals) if len(all_rank_vals) > 1 else 0.0
    best = getf(top[0], rank_col)
    if stdev_all > 0:
        separation = (median_all - best) / stdev_all if lower_better else (best - median_all) / stdev_all
    else:
        separation = 0.0

    param_cols = [
        c for c in numeric
        if c not in (rank_col, is_col, oos_col, sample_col, id_col)
        and not any(kw in c.lower() for kw in METRIC_KEYWORDS)
    ]
    params = parameter_check(top, ranked, param_cols, getf)
    any_isolated = any(not f["contiguous"] for f in params.values())
    any_edge = any(f["edge"] for f in params.values())
    plateau = bool(params) and not any_isolated

    samples_ok = None
    low_sample_variants = []
    if sample_col:
        samples_ok = True
        for r in top:
            n = getf(r, sample_col)
            if n is not None and n < args.min_samples:
                samples_ok = False
                low_sample_variants.append((r.get(id_col), n))

    # --- verdict ---
    disp = disp_rank
    kill = False
    kill_reasons = []
    if rho is not None and rho <= 0.0:
        kill = True
        kill_reasons.append(f"IS/OOS rank correlation {fmt_num(rho)} ≤ 0 — in-sample ordering does not survive out-of-sample")
    if any_isolated and disp is not None and disp > 2 * args.dispersion_threshold:
        kill = True
        kill_reasons.append("top variants are scattered points in parameter space with high dispersion — no stable region")
    if separation < 0.5:
        kill = True
        kill_reasons.append(f"best variant is ~indistinguishable from the median ({fmt_num(separation)}σ separation)")

    converged = (
        not kill
        and disp is not None and disp < args.dispersion_threshold
        and (rho is None or rho > args.rank_threshold)
        and plateau
        and (samples_ok is None or samples_ok)
        and not any_edge
    )

    if kill:
        verdict = "KILL"
    elif converged:
        verdict = "CONVERGED"
    else:
        verdict = "ITERATE"

    ctx = dict(
        path=path, id_col=id_col, rank_col=rank_col, is_col=is_col, oos_col=oos_col,
        sample_col=sample_col, k=k, n=len(ranked), top=top, lower_better=lower_better,
        disp_rank=disp_rank, disp_is=disp_is, disp_oos=disp_oos, rho=rho,
        params=params, plateau=plateau, any_edge=any_edge, any_isolated=any_isolated,
        samples_ok=samples_ok, low_sample_variants=low_sample_variants,
        verdict=verdict, kill_reasons=kill_reasons, separation=separation,
        best=best, median_all=median_all, getf=getf, args=args,
    )
    return ctx


def build_report(ctx):
    a = ctx["args"]
    run_name = a.run_name or ctx["path"].split("/")[-1]
    date = a.date or datetime.now().strftime("%Y-%m-%d")
    getf = ctx["getf"]
    rank_col, is_col, oos_col = ctx["rank_col"], ctx["is_col"], ctx["oos_col"]
    thr = a.dispersion_threshold

    L = []
    L.append(f"# Iteration Check | {run_name} | {date}")
    L.append("")
    L.append(f"## Verdict: {ctx['verdict']}")
    L.append("")
    L.append(f"**{_summary_line(ctx)}**")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## Convergence Analysis")
    L.append("")
    L.append(f"Ranking metric: `{rank_col}` ({'lower is better' if ctx['lower_better'] else 'higher is better'}) · "
             f"Top-{ctx['k']} of {ctx['n']} variants")
    L.append("")
    L.append("| Metric | Top-K Mean | Top-K Std | Dispersion | Threshold |")
    L.append("|--------|-----------|-----------|------------|-----------|")
    seen = set()
    for col, disp in [(rank_col, ctx["disp_rank"]), (is_col, ctx["disp_is"]), (oos_col, ctx["disp_oos"])]:
        if not col or col in seen:
            continue
        seen.add(col)
        vals = [getf(r, col) for r in ctx["top"] if getf(r, col) is not None]
        if not vals:
            continue
        mean = statistics.mean(vals)
        sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
        flag = "—" if disp is None else ("✓" if disp < thr else "✗")
        label = col + (" *(rank)*" if col == rank_col else "")
        L.append(f"| {label} | {fmt_num(mean)} | {fmt_num(sd)} | {fmt_pct(disp)} | < {fmt_pct(thr,0)} {flag} |")
    L.append("")
    if ctx["rho"] is not None:
        ok = "✓" if ctx["rho"] > a.rank_threshold else "✗"
        L.append(f"**IS→OOS rank stability (Spearman):** {fmt_num(ctx['rho'])} "
                 f"(threshold > {fmt_num(a.rank_threshold)} {ok})")
        L.append("")
    L.append(f"**Read:** {_read_line(ctx)}")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## Parameter-Space Check")
    L.append("")
    if not ctx["params"]:
        L.append("No parameter columns detected — cannot assess clustering. "
                 "Pass parameter columns or check column inference.")
    else:
        clusters = []
        for p, f in ctx["params"].items():
            occ = ", ".join(fmt_num(v, 4).rstrip("0").rstrip(".") for v in f["occupied"])
            edge = " **(edge of swept range)**" if f["edge"] else ""
            clusters.append(f"- `{p}`: top-K at [{occ}]{edge} — {'contiguous' if f['contiguous'] else 'scattered'}")
        L.extend(clusters)
        L.append("")
        L.append(f"[{'x' if not ctx['plateau'] else ' '}] Single isolated peak — fragile, retest with more samples")
        L.append(f"[{'x' if ctx['plateau'] else ' '}] Plateau region — robust, safe to deploy")
    L.append("")
    if ctx["sample_col"]:
        if ctx["samples_ok"]:
            L.append(f"Sample sizes OK — all top-{ctx['k']} variants ≥ {a.min_samples} on `{ctx['sample_col']}`.")
        else:
            offenders = ", ".join(f"{vid} ({fmt_num(n,0)})" for vid, n in ctx["low_sample_variants"])
            L.append(f"⚠ Below `--min-samples` ({a.min_samples}) on `{ctx['sample_col']}`: {offenders}")
        L.append("")
    L.append("---")
    L.append("")
    L.append("## Recommendation")
    L.append("")
    L.extend(_recommendation(ctx))
    L.append("")
    return "\n".join(L)


def _summary_line(ctx):
    v = ctx["verdict"]
    if v == "CONVERGED":
        top_id = ctx["top"][0].get(ctx["id_col"])
        return (f"Top-{ctx['k']} cluster tightly ({fmt_pct(ctx['disp_rank'])} dispersion) on a parameter plateau"
                f"{'' if ctx['rho'] is None else f', OOS-stable (ρ={fmt_num(ctx['rho'])})'} — ship `{top_id}`.")
    if v == "KILL":
        return ctx["kill_reasons"][0] if ctx["kill_reasons"] else "No stable edge in the swept parameter space."
    return "Convergence not yet achieved — some criteria unmet (see below)."


def _read_line(ctx):
    bits = []
    d = ctx["disp_rank"]
    if d is not None:
        tight = d < ctx["args"].dispersion_threshold
        bits.append(f"top-{ctx['k']} are {'clustered tightly' if tight else 'spread out'} on the ranking metric ({fmt_pct(d)})")
    if ctx["rho"] is not None:
        bits.append(f"IS↔OOS rank correlation is {fmt_num(ctx['rho'])}")
    if not bits:
        return "Limited signal — too few variants or near-zero mean for a dispersion read."
    return "; ".join(bits).capitalize() + "."


def _recommendation(ctx):
    v = ctx["verdict"]
    if v == "CONVERGED":
        top_id = ctx["top"][0].get(ctx["id_col"])
        return [f"**[CONVERGED]** Top variant `{top_id}` is representative — its neighbors perform similarly "
                f"and the winning region is a plateau, not a lucky point. Safe to advance this variant to the next stage."]
    if v == "KILL":
        out = ["**[KILL]** No region of the swept parameter space shows a stable edge. Specifically:"]
        out += [f"- {r}" for r in ctx["kill_reasons"]]
        out.append("Recommend abandoning this strategy line, or rethinking the metric/feature set before re-sweeping.")
        return out
    # ITERATE
    out = ["**[ITERATE]** Convergence not yet achieved. Suggested next sweep:"]
    sugg = []
    for p, f in ctx["params"].items():
        if f["edge"]:
            sugg.append(f"- Expand `{p}` beyond its current edge (top variant sits at {fmt_num(f['top1'],4).rstrip('0').rstrip('.')}, "
                        f"the boundary of the swept grid)")
        elif not f["contiguous"]:
            sugg.append(f"- Refine the `{p}` grid around the top cluster — winners are currently scattered")
    if ctx["disp_rank"] is not None and ctx["disp_rank"] >= ctx["args"].dispersion_threshold:
        sugg.append(f"- Tighten dispersion: top-K spread is {fmt_pct(ctx['disp_rank'])} "
                    f"(> {fmt_pct(ctx['args'].dispersion_threshold,0)}) — narrow the grid or add samples")
    if ctx["rho"] is not None and ctx["rho"] <= ctx["args"].rank_threshold:
        sugg.append(f"- IS/OOS rank stability is weak (ρ={fmt_num(ctx['rho'])}) — likely undersampled; "
                    f"increase trades per variant or lengthen the OOS window")
    if ctx["samples_ok"] is False:
        sugg.append(f"- Rerun low-sample variants with ≥ {ctx['args'].min_samples} trades before trusting their rank")
    if not sugg:
        sugg.append("- Add samples and re-run; the signal is borderline on at least one criterion")
    out += sugg
    return out


VERDICT_EXIT = {"CONVERGED": 0, "ITERATE": 1, "KILL": 2}


def add_arguments(p):
    p.add_argument("input", help="CSV file: one row per variant")
    p.add_argument("--metric", help="Ranking metric column (e.g. sharpe_oos). Auto-detected if omitted.")
    p.add_argument("--top-k", type=int, default=5, help="Number of top variants to assess (default 5)")
    p.add_argument("--dispersion-threshold", type=float, default=0.05,
                   help="Max relative dispersion for CONVERGED (default 0.05 = 5%%)")
    p.add_argument("--min-samples", type=int, default=200, help="Min samples/variant (default 200)")
    p.add_argument("--rank-threshold", type=float, default=0.6,
                   help="Min IS/OOS Spearman for CONVERGED (default 0.6)")
    p.add_argument("--id-column", help="Override the variant-id column")
    p.add_argument("--sample-column", help="Override the sample-size column")
    p.add_argument("--lower-is-better", action="store_true", help="Treat the metric as lower-is-better")
    p.add_argument("--higher-is-better", action="store_true", help="Force higher-is-better (override name heuristic)")
    p.add_argument("--run-name", help="Label for the report header")
    p.add_argument("--date", help="Date for the report header (default: today)")
    p.add_argument("--save", action="store_true",
                   help="Also write iteration_check_<timestamp>.md in the current directory")
    p.add_argument("--json", action="store_true", help="Print a machine-readable verdict instead of Markdown")


def to_json(ctx):
    """Machine-readable verdict: the numbers behind the Markdown report."""
    getf = ctx["getf"]
    return {
        "schema": "backtest2live.convergence/1",
        "input": ctx["path"],
        "verdict": ctx["verdict"],
        "exit_code": VERDICT_EXIT[ctx["verdict"]],
        "metric": ctx["rank_col"],
        "lower_is_better": ctx["lower_better"],
        "is_column": ctx["is_col"],
        "oos_column": ctx["oos_col"],
        "sample_column": ctx["sample_col"],
        "top_k": ctx["k"],
        "n_variants": ctx["n"],
        "top": [{"id": r.get(ctx["id_col"]), "value": getf(r, ctx["rank_col"])} for r in ctx["top"]],
        "dispersion": {"rank": ctx["disp_rank"], "is": ctx["disp_is"], "oos": ctx["disp_oos"]},
        "is_oos_spearman": ctx["rho"],
        "separation_sigma": ctx["separation"],
        "plateau": ctx["plateau"],
        "edge_peak": ctx["any_edge"],
        "parameters": {
            p: {"occupied": f["occupied"], "contiguous": f["contiguous"], "edge": f["edge"]}
            for p, f in ctx["params"].items()
        },
        "samples_ok": ctx["samples_ok"],
        "low_sample_variants": [{"id": vid, "n": n} for vid, n in ctx["low_sample_variants"]],
        "kill_reasons": ctx["kill_reasons"],
        "thresholds": {
            "dispersion": ctx["args"].dispersion_threshold,
            "rank": ctx["args"].rank_threshold,
            "min_samples": ctx["args"].min_samples,
        },
    }


def run(args):
    """CLI entry for `bt2live convergence`. Returns the verdict exit code."""
    ctx = analyze(args.input, args)
    report = build_report(ctx)
    if args.json:
        print(json.dumps(to_json(ctx), indent=2, ensure_ascii=False))
    else:
        print(report)

    if args.save:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = f"iteration_check_{stamp}.md"
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
        print(f"\n[saved report → {out_path}]", file=sys.stderr)

    # Exit code encodes the verdict for scripting: 0 CONVERGED, 1 ITERATE, 2 KILL.
    return VERDICT_EXIT[ctx["verdict"]]
