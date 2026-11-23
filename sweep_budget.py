#!/usr/bin/env python3
"""
sweep_budget.py — Memory Band-Aid budget sweep for HammerSim.


Runs the same double-sided RowHammer attack at several rate-limiter budgets
(plus an unlimited baseline), repeats each run to average out the
wall-clock-seeded weak-cell variance, and reports the full
security-vs-performance tradeoff:
  security    -> rowHammerTotalBitflips, maxRowActsInWindow, rlDenials
  performance -> victim-reader avg read latency (cost to legit traffic)

Run from the repo root, with a built binary and the runtime env (no venv):
    module load gcc/12.2.1 python/3.11
    export LD_LIBRARY_PATH=/usr/um/python-3.11/lib:$HOME/.local/lib:$LD_LIBRARY_PATH
    python3 sweep_budget.py
    python3 sweep_budget.py --budgets off 5000 20000 40000 --reps 5
    python3 sweep_budget.py --device-map util/hammersim/prob-005.json
"""

import argparse
import csv
import json
import os
import subprocess

TICKS_PER_NS = 1000.0  # gem5 default tick = 1 ps
SCALAR_STATS = ["rowHammerTotalBitflips", "maxRowActsInWindow", "rlDenials"]


def flatten(data):
    flat = {}
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                walk(v) if isinstance(v, (dict, list)) else flat.__setitem__(k, v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(data)
    return flat


def gen_avg_latency_ns(data, prefix):
    """Average avgReadLatency (ns) over generators named like `prefix`*
    ('victim' or 'aggressor'). None if absent/no reads."""
    gens = data.get("generator_stats")
    if not isinstance(gens, dict):
        return None
    vals = [float(s["avgReadLatency"]) / TICKS_PER_NS
            for n, s in gens.items()
            if n.startswith(prefix) and isinstance(s, dict)
            and s.get("avgReadLatency") is not None]
    return sum(vals) / len(vals) if vals else None


def parse_args():
    p = argparse.ArgumentParser(description="Memory Band-Aid budget sweep")
    p.add_argument("--gem5", default="build/X86/gem5.opt")
    p.add_argument("--hammer", default="configs/dram/rowhammer/TrafficGen/hammer.py")
    p.add_argument("--device-map",
                   default="util/hammersim/row_experiment_vendor_b/dimm0.bank-4.json")
    p.add_argument("--outdir", default="m5out/sweep")
    p.add_argument("--budgets", nargs="+",
                   default=["off", "5000", "10000", "20000", "40000"],
                   help="'off' = no limiter; otherwise integer budgets")
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--victims", default="50")
    p.add_argument("--pattern", default="double")
    p.add_argument("--period-min", default="1000000")
    p.add_argument("--period-max", default="1300000")
    p.add_argument("--victim-reader", action="store_true",
                   help="add a victim-reader for its latency; WARNING: in HammerSim "
                        "this activates the victim row and SUPPRESSES flips, so do "
                        "NOT use it for security/flip runs")
    return p.parse_args()


def main():
    a = parse_args()
    os.makedirs(a.outdir, exist_ok=True)

    attack = ["--victims", a.victims, "--pattern", a.pattern,
              "--period-min", a.period_min, "--period-max", a.period_max,
              "--device-map", a.device_map]
    if a.victim_reader:
        attack.append("--victim-reader")

    for b in a.budgets:
        for r in range(1, a.reps + 1):
            tag = f"{'off' if b == 'off' else 'b' + b}-r{r}"
            out = os.path.join(a.outdir, tag)
            cmd = [a.gem5, "-d", out, a.hammer] + attack
            if b != "off":
                cmd += ["--rl-budget", b]
            print(f">>> budget={b} rep={r} -> {out}")
            with open(out + ".log", "w") as lf:
                rc = subprocess.call(cmd, stdout=lf, stderr=subprocess.STDOUT)
            if rc != 0:
                print(f"    run FAILED (rc={rc}); see {out}.log")

    def avg(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else float("nan")

    header = ["budget", "reps"] + SCALAR_STATS + ["victimLat_ns", "aggrLat_ns"]
    widths = [8, 6] + [max(14, len(h)) for h in header[2:]]
    fmt = lambda cols: "".join(str(c).rjust(w) for c, w in zip(cols, widths))
    print("\n" + fmt(header))

    rows = []
    for b in a.budgets:
        summaries = []
        for r in range(1, a.reps + 1):
            tag = f"{'off' if b == 'off' else 'b' + b}-r{r}"
            sp = os.path.join(a.outdir, tag, "summary.json")
            if os.path.exists(sp):
                with open(sp) as f:
                    summaries.append(json.load(f))
        flats = [flatten(s) for s in summaries]
        row = [b, len(summaries)]
        for s in SCALAR_STATS:
            row.append(f"{avg([float(f.get(s, 0)) for f in flats]):.1f}")
        row.append(f"{avg([gen_avg_latency_ns(s, 'victim') for s in summaries]):.1f}")
        row.append(f"{avg([gen_avg_latency_ns(s, 'aggressor') for s in summaries]):.1f}")
        rows.append(row)
        print(fmt(row))

    csv_path = os.path.join(a.outdir, "results.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"\nwrote {csv_path}")


if __name__ == "__main__":
    main()