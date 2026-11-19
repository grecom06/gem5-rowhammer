"""
Parameterised RowHammer traffic-generator experiment for HammerSim.

Replaces the hard-coded per-experiment scripts in this directory with one
script driven by command-line options. One PyTrafficGen is created per
aggressor row; each repeatedly reads a small range inside its row so that,
with several aggressors in the same bank, every access forces an ACTIVATE.

Physical addresses are computed from the DRAM geometry (row-buffer size,
banks, ranks, rows per bank) of the chosen DRAM class, so the script stays
correct when the DRAM type or memory size changes.

Run from the repository root (the default device map path is relative to
the current directory):

  # Double-sided hammering of victim rows 293 and 7292 in bank 4
  build/X86/gem5.opt -d m5out/ds configs/dram/rowhammer/TrafficGen/hammer.py \\
      --bank 4 --victims 293,7292 --pattern double

  # TRRespass-style 10-sided attack with a larger TRR table
  build/X86/gem5.opt -d m5out/ms10 configs/dram/rowhammer/TrafficGen/hammer.py \\
      --victims 1000 --pattern many-sided --sides 10 --trr-table 16

  # Only print the aggressor -> address map, do not simulate
  build/X86/gem5.opt configs/dram/rowhammer/TrafficGen/hammer.py \\
      --victims 293 --print-map

Results: the usual stats.txt plus <outdir>/summary.json holding the full
configuration, the aggressor map and every rowHammer* stat, for sweeps.
"""

import argparse
import json
import os
import re
import sys

import m5
from m5.objects import *
from m5.util.convert import toLatency, toMemorySize

# Address mappings for which "row is the highest field, column is inside a
# row" holds and which the HammerSim corruption logic supports.
SUPPORTED_MAPPINGS = ("RoRaBaCoCh", "RoRaBaChCo")


def fail(msg):
    """Print an error and exit. gem5 aborts if sys.exit() is given a
    string, so always exit with an integer code."""
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def parse_int_list(text):
    return [int(x) for x in text.split(",") if x.strip()]


def parse_args():
    p = argparse.ArgumentParser(
        description="Parameterised RowHammer traffic-generator experiment",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    g = p.add_argument_group("memory system")
    g.add_argument("--dram", default="DDR4_2400_8x8",
                   help="DRAM interface class from m5.objects")
    g.add_argument("--mem-size", default="1GiB",
                   help="Size of the memory address range")
    g.add_argument("--page-policy", default=None,
                   choices=["open", "open_adaptive", "close",
                            "close_adaptive"])
    g.add_argument("--sched-policy", default=None, choices=["frfcfs", "fcfs"])

    g = p.add_argument_group("attack pattern")
    g.add_argument("--bank", type=int, default=4,
                   help="Target bank (all aggressors are in this bank)")
    g.add_argument("--victims", type=parse_int_list, default=None,
                   help="Comma-separated victim rows; aggressors are derived "
                        "from --pattern")
    g.add_argument("--pattern", default="double",
                   choices=["double", "single", "half-double", "many-sided"])
    g.add_argument("--sides", type=int, default=4,
                   help="many-sided: number of aggressor rows per victim "
                        "group (rows v-1, v+1, v+3, ...)")
    g.add_argument("--far-offset", type=int, default=64,
                   help="single: distance of the conflict row used to close "
                        "the row buffer between hammers")
    g.add_argument("--hd-near-scale", type=float, default=100.0,
                   help="half-double: near aggressors (v+-1) are accessed this "
                        "many times less often than far aggressors (v+-2)")
    g.add_argument("--aggressors", default=None,
                   help="Explicit aggressor rows, overriding --victims/"
                        "--pattern. Format: row[:period_scale],...")
    g.add_argument("--period-min", type=int, default=1230000,
                   help="Minimum ticks between requests of one aggressor")
    g.add_argument("--period-max", type=int, default=1600000,
                   help="Maximum ticks between requests of one aggressor")
    g.add_argument("--access-bytes", type=int, default=1024,
                   help="Bytes accessed inside each aggressor row (cycled)")
    g.add_argument("--block-size", type=int, default=64)
    g.add_argument("--read-percent", type=int, default=100)
    g.add_argument("--duration", default="100ms",
                   help="Simulated time to hammer for")
    g.add_argument("--victim-reader", action="store_true",
                   help="Add a slow generator that reads the victim rows, so "
                        "corrupted data is actually returned to a requester")
    g.add_argument("--victim-period", type=int, default=1000000000,
                   help="Ticks between victim-reader requests")

    g = p.add_argument_group("RowHammer model (unset = DRAM class default)")
    g.add_argument("--device-map", default=None,
                   help="Path to the device (weak-cell) map JSON")
    g.add_argument("--rh-threshold", type=int, default=None)
    g.add_argument("--trr-variant", type=int, default=None)
    g.add_argument("--trr-threshold", type=int, default=None)
    g.add_argument("--trr-table", type=int, default=None,
                   help="counter_table_length")
    g.add_argument("--companion-table", type=int, default=None,
                   help="companion_table_length")
    g.add_argument("--double-sided-prob", type=float, default=None)
    g.add_argument("--single-sided-prob", type=float, default=None)
    g.add_argument("--half-double-prob", type=float, default=None)
    g.add_argument("--no-synthetic", action="store_true",
                   help="Do not set synthetic_traffic=True (it throttles "
                        "flips to compensate for traffic-generator speed)")
    g.add_argument("--corruption", action="store_true",
                   help="enable_memory_corruption=True")
    g.add_argument("--ecc", type=int, default=None,
                   help="ECC algorithm index (1 = SECDED); enables ECC")
    g.add_argument("--p-matrix", default=None, help="ECC parity matrix path")
    g.add_argument("--rh-trace", action="store_true",
                   help="rh_stat_dump=True (writes the RowHammer trace)")
    g.add_argument("--dram-param", action="append", default=[],
                   metavar="NAME=VALUE",
                   help="Set any other DRAM parameter; repeatable")

    g = p.add_argument_group("output")
    g.add_argument("--print-map", action="store_true",
                   help="Print the aggressor/address map and exit")
    g.add_argument("--summary", default="summary.json",
                   help="Summary file name, written inside the gem5 outdir")

    args = p.parse_args()
    if args.aggressors is None and not args.victims:
        p.error("give --victims (with --pattern) or --aggressors")
    if args.period_min > args.period_max:
        p.error("--period-min must not exceed --period-max")
    return args


def pv(value):
    """Plain int from a gem5 param value or a Python number."""
    return int(getattr(value, "value", value))


def dram_geometry(dram, mem_size_bytes):
    """Mirror DRAMInterface::setCtrl()/decodePacket() geometry."""
    row_buffer = pv(dram.device_rowbuffer_size) * pv(dram.devices_per_rank)
    banks = pv(dram.banks_per_rank)
    ranks = pv(dram.ranks_per_channel)
    # gem5 rounds the capacity up to a power of two before dividing.
    capacity = 1 << (mem_size_bytes - 1).bit_length()
    rows_per_bank = capacity // (row_buffer * banks * ranks)
    return {
        "row_buffer_bytes": row_buffer,
        "banks_per_rank": banks,
        "ranks_per_channel": ranks,
        "rows_per_bank": rows_per_bank,
        "addr_mapping": str(dram.addr_mapping),
    }


def row_address(geo, bank, row, rank=0):
    """Physical address of column 0 of (rank, bank, row) for Ro..Ra..Ba..Co
    mappings with a single channel."""
    return ((row * geo["ranks_per_channel"] + rank)
            * geo["banks_per_rank"] + bank) * geo["row_buffer_bytes"]


def build_aggressors(args):
    """List of (row, period_scale)."""
    if args.aggressors is not None:
        out = []
        for item in args.aggressors.split(","):
            item = item.strip()
            if not item:
                continue
            row, _, scale = item.partition(":")
            out.append((int(row), float(scale) if scale else 1.0))
        return out

    rows = {}  # row -> scale; dict keeps insertion order and dedups

    def add(row, scale=1.0):
        rows[row] = min(scale, rows.get(row, scale))

    for v in args.victims:
        if args.pattern == "double":
            add(v - 1)
            add(v + 1)
        elif args.pattern == "single":
            add(v - 1)
            add(v - 1 + args.far_offset)
        elif args.pattern == "half-double":
            add(v - 2)
            add(v + 2)
            add(v - 1, args.hd_near_scale)
            add(v + 1, args.hd_near_scale)
        elif args.pattern == "many-sided":
            for k in range(args.sides):
                add(v - 1 + 2 * k)
    return list(rows.items())


def set_param(obj, name, value):
    if not hasattr(obj, name):
        fail(f"{type(obj).__name__} has no parameter '{name}'")
    setattr(obj, name, value)


def configure_dram(dram, args):
    dram.ranks_per_channel = 1  # device-map lookup only supports rank 0
    if not args.no_synthetic:
        dram.synthetic_traffic = True
    optional = {
        "device_file": (os.path.abspath(args.device_map)
                        if args.device_map else None),
        "rowhammer_threshold": args.rh_threshold,
        "trr_variant": args.trr_variant,
        "trr_threshold": args.trr_threshold,
        "counter_table_length": args.trr_table,
        "companion_table_length": args.companion_table,
        "double_sided_prob": (int(args.double_sided_prob)
                              if args.double_sided_prob is not None else None),
        "single_sided_prob": (int(args.single_sided_prob)
                              if args.single_sided_prob is not None else None),
        "half_double_prob": (int(args.half_double_prob)
                             if args.half_double_prob is not None else None),
        "page_policy": args.page_policy,
        "p_matrix": (os.path.abspath(args.p_matrix)
                     if args.p_matrix else None),
    }
    for name, value in optional.items():
        if value is not None:
            set_param(dram, name, value)
    if args.corruption:
        dram.enable_memory_corruption = True
    if args.ecc is not None:
        dram.enable_ecc = args.ecc != 0
        dram.ecc_algorithm = args.ecc
    if args.rh_trace:
        dram.rh_stat_dump = True
        dram.rh_stat_file = os.path.join(m5.options.outdir,
                                         "rowhammer.trace")
    for item in args.dram_param:
        name, sep, value = item.partition("=")
        if not sep:
            fail(f"--dram-param expects NAME=VALUE, got '{item}'")
        set_param(dram, name.strip(), value.strip())


def collect_rh_stats(stats_path):
    """All rowHammer* scalars from the last dump in stats.txt."""
    result = {}
    if not os.path.exists(stats_path):
        return result
    pattern = re.compile(r"^(\S*\.rowHammer\w+)\s+(\S+)")
    with open(stats_path) as f:
        for line in f:
            if line.startswith("---------- Begin"):
                result = {}
            m = pattern.match(line)
            if m:
                try:
                    result[m.group(1).split(".")[-1]] = float(m.group(2))
                except ValueError:
                    pass
    return result


def main():
    args = parse_args()

    dram_cls = getattr(m5.objects, args.dram, None)
    if dram_cls is None or not issubclass(dram_cls, DRAMInterface):
        fail(f"'{args.dram}' is not a DRAMInterface class")

    mem_size = toMemorySize(args.mem_size)

    system = System()
    system.clk_domain = SrcClockDomain()
    system.clk_domain.clock = "4GHz"
    system.clk_domain.voltage_domain = VoltageDomain()
    system.mem_mode = "timing"
    system.mem_ranges = [AddrRange(args.mem_size)]

    system.mem_ctrl = MemCtrl()
    if args.sched_policy:
        system.mem_ctrl.mem_sched_policy = args.sched_policy
    system.mem_ctrl.dram = dram_cls(range=system.mem_ranges[0])
    dram = system.mem_ctrl.dram
    configure_dram(dram, args)

    geo = dram_geometry(dram, mem_size)
    if geo["addr_mapping"] not in SUPPORTED_MAPPINGS:
        fail(f"addr_mapping {geo['addr_mapping']} unsupported; "
                 f"use one of {SUPPORTED_MAPPINGS}")
    if not 0 <= args.bank < geo["banks_per_rank"]:
        fail(f"bank {args.bank} out of range "
                 f"(0..{geo['banks_per_rank'] - 1})")
    if args.access_bytes > geo["row_buffer_bytes"]:
        fail(f"--access-bytes {args.access_bytes} exceeds the "
                 f"row buffer ({geo['row_buffer_bytes']} B)")

    aggressors = build_aggressors(args)
    if not aggressors:
        fail("no aggressor rows")
    for row, _ in aggressors:
        if not 0 <= row < geo["rows_per_bank"]:
            fail(f"aggressor row {row} out of range "
                     f"(0..{geo['rows_per_bank'] - 1})")

    agg_map = []
    for row, scale in aggressors:
        start = row_address(geo, args.bank, row)
        agg_map.append({
            "row": row,
            "period_scale": scale,
            "start": start,
            "end": start + args.access_bytes,
            "period_min": int(args.period_min * scale),
            "period_max": int(args.period_max * scale),
        })

    print(f"DRAM {args.dram}: row buffer {geo['row_buffer_bytes']} B, "
          f"{geo['banks_per_rank']} banks, {geo['ranks_per_channel']} rank(s), "
          f"{geo['rows_per_bank']} rows/bank, mapping {geo['addr_mapping']}")
    print(f"Bank {args.bank}, {len(agg_map)} aggressor(s):")
    for a in agg_map:
        print(f"  row {a['row']:6d}  addr {a['start']:#012x}-{a['end']:#012x}"
              f"  period {a['period_min']}-{a['period_max']} ticks")
    if args.print_map:
        return

    system.membus = L2XBar()
    gens = []
    for i, a in enumerate(agg_map):
        gen = PyTrafficGen()
        setattr(system, f"aggressor{i}", gen)
        system.membus.cpu_side_ports = gen.port
        gens.append((gen, a))

    victim_rows = []
    if args.victim_reader:
        victim_rows = (args.victims if args.victims
                       else sorted({r - 1 for r, _ in aggressors}
                                   | {r + 1 for r, _ in aggressors}))
        victim_gens = []
        for i, v in enumerate(victim_rows):
            if not 0 <= v < geo["rows_per_bank"]:
                continue
            gen = PyTrafficGen()
            setattr(system, f"victim{i}", gen)
            system.membus.cpu_side_ports = gen.port
            start = row_address(geo, args.bank, v)
            victim_gens.append((gen, {
                "row": v, "start": start,
                "end": start + geo["row_buffer_bytes"],
                "period_min": args.victim_period,
                "period_max": args.victim_period,
            }))
        gens.extend(victim_gens)

    system.mem_ctrl.port = system.membus.mem_side_ports

    def traffic(tgen, a):
        yield tgen.createLinear(
            duration,
            a["start"],
            a["end"],
            args.block_size,
            a["period_min"],
            a["period_max"],
            args.read_percent,
            0,  # data_limit
        )
        yield tgen.createExit(0)

    root = Root(full_system=False, system=system)
    m5.instantiate()
    # The tick frequency is only fixed once the system is instantiated.
    duration = m5.ticks.fromSeconds(toLatency(args.duration))
    for gen, a in gens:
        gen.start(traffic(gen, a))

    exit_event = m5.simulate(duration + 1)
    print(f"Exiting @ tick {m5.curTick()}: {exit_event.getCause()}")

    # Write stats now so the summary can include them.
    m5.stats.dump()
    stats = collect_rh_stats(os.path.join(m5.options.outdir, "stats.txt"))

    summary = {
        "command": sys.argv,
        "args": vars(args),
        "geometry": geo,
        "aggressors": agg_map,
        "victim_reader_rows": victim_rows,
        "sim_ticks": m5.curTick(),
        "exit_cause": exit_event.getCause(),
        "rowhammer_stats": stats,
    }
    out = os.path.join(m5.options.outdir, args.summary)
    with open(out, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"RowHammer stats: {json.dumps(stats)}")
    print(f"Summary written to {out}")


if __name__ == "__m5_main__" or __name__ == "__main__":
    main()
