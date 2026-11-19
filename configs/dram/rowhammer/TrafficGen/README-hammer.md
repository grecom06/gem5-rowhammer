# hammer.py: quick guide

`hammer.py` runs a RowHammer attack in HammerSim using gem5's traffic
generators. It creates one generator per aggressor row. You set the attack
from the command line instead of editing a script, and each run writes a
`summary.json` with the settings and RowHammer stats, ready for sweeps.

## Before you start

- **Build** `build/X86/gem5.opt` from a tree that includes the stats fix in
  `src/mem/dram_interface.cc`, which registers the `rowHammer*` stats.
  Without it, flip counts never show up in `stats.txt`.
- **Run from the repository root.** Default paths are relative to it.
- **Unzip the default device map** once:
  `cd util/hammersim && unzip prob-005.json.zip`
  You can skip this if you always pass `--device-map`.

## Basic use

```bash
build/X86/gem5.opt -d m5out/<run-name> configs/dram/rowhammer/TrafficGen/hammer.py [options]
```

Give every run its own `-d` output directory, or runs overwrite each other.

```bash
# Double-sided attack on victim rows 293 and 7292 (bank 4)
build/X86/gem5.opt -d m5out/ds configs/dram/rowhammer/TrafficGen/hammer.py \
    --victims 293,7292 --pattern double

# 10-sided attack against a larger TRR table, with a lower threshold
build/X86/gem5.opt -d m5out/ms10 configs/dram/rowhammer/TrafficGen/hammer.py \
    --victims 1000 --pattern many-sided --sides 10 --trr-table 16 --rh-threshold 10000

# Show which rows/addresses would be hammered, without simulating
build/X86/gem5.opt configs/dram/rowhammer/TrafficGen/hammer.py \
    --victims 293 --pattern half-double --print-map
```

A 100 ms run takes a few seconds.

## Choosing the aggressor rows

Either give victim rows and a pattern, or list the aggressor rows yourself.

| Option | Aggressor rows for each victim row `v` |
|---|---|
| `--pattern double` (default) | `v-1`, `v+1` |
| `--pattern single` | `v-1` and a distant conflict row `v-1+64` (`--far-offset`) |
| `--pattern half-double` | `v-2`, `v+2`, plus `v-1` and `v+1` accessed 100× less often (`--hd-near-scale`) |
| `--pattern many-sided --sides N` | `v-1`, `v+1`, `v+3`, … (N rows; the rows in between are the victims) |
| `--aggressors 292,294:100,...` | Exactly these rows. `:100` makes that row's accesses 100× less frequent |

All aggressor rows are in one bank, set with `--bank` (default 4).

## Main options

Run `--help` for the full list. The RowHammer options keep the DRAM class
default unless you set them.

| Option | Default | Meaning |
|---|---|---|
| `--dram` | `DDR4_2400_8x8` | Any DRAM class, e.g. `DDR5_4400_4x8`. Addresses are worked out from its layout |
| `--mem-size` | `1GiB` | Memory size, which determines the number of rows per bank |
| `--duration` | `100ms` | Simulated time to hammer |
| `--period-min/--period-max` | `1230000/1600000` | Ticks (ps) between requests from each aggressor |
| `--device-map` | `util/hammersim/prob-005.json` | Weak-cell map (JSON) |
| `--rh-threshold` | 50000 | Activations needed before a neighbouring row can flip |
| `--trr-variant` | 0 (no TRR) | TRR model; the vendor scripts use 6 |
| `--trr-table`, `--trr-threshold` | class default | TRR table size and threshold |
| `--single/--double-sided-prob`, `--half-double-prob` | class default | Flip probability settings (see Known issues) |
| `--corruption` | off | Actually flip bits in the simulated memory |
| `--victim-reader` | off | Add a slow generator that reads the victim rows |
| `--ecc 1 --p-matrix FILE` | off | SECDED ECC |
| `--page-policy`, `--sched-policy` | class default | Memory-controller policies |
| `--dram-param NAME=VALUE` | | Set any other DRAM parameter; can be repeated |
| `--no-synthetic` | | Don't set `synthetic_traffic=True`, which reduces flips to make up for how fast generators hammer |

## Output

Everything goes in the `-d` directory:

- `summary.json`: the command, all settings, DRAM layout, aggressor rows
  and addresses, and every `rowHammer*` stat. **Use this for analysis.**
- `stats.txt`: all gem5 stats. Search for `rowHammer`, `readRowHits`, etc.
- `config.ini`: every parameter the simulated system actually used.

Main stats:
- `rowHammerTotalBitflips`, plus `rowHammerSingleSidedBitflips`,
  `rowHammerDoubleSidedBitflips`, `rowHammerHalfDoubleBitflips` by attack type
- `rowHammerSamplerTriggers` and `rowHammerInhibitorTriggers` for TRR activity

## Running a sweep

Runs only use one core each, so run them in parallel:

```bash
OUT=m5out/sweep; mkdir -p $OUT
for trr in 0 6; do for n in 2 4 6 8 10 12; do for r in 1 2 3; do echo "$trr $n $r"; done; done; done | \
xargs -P 12 -L 1 sh -c 'build/X86/gem5.opt -d '$OUT'/trr$0-n$1-r$2 \
  configs/dram/rowhammer/TrafficGen/hammer.py --bank 4 --victims 50 \
  --pattern many-sided --sides $1 \
  --device-map util/hammersim/row_experiment_vendor_b/dimm0.bank-4.json \
  --trr-variant $0 --trr-threshold 16834 --trr-table 6 --rh-threshold 45000 \
  > /dev/null 2>&1'
```

Then collect the results:

```python
import json, glob
for f in sorted(glob.glob("m5out/sweep/*/summary.json")):
    d = json.load(open(f))
    print(d["args"]["trr_variant"], d["args"]["sides"],
          d["rowhammer_stats"]["rowHammerTotalBitflips"])
```

Expected result: with TRR (variant 6, 6-entry table), there are no flips up
to 6 aggressors, and flips start at 8.

## Checking that you hit the right rows

1. `--print-map` shows the rows and addresses.
2. A short run with DRAM debug output shows the bank and row of every access:
   ```bash
   build/X86/gem5.opt --debug-flags=DRAM --debug-file=dram.log -d m5out/check \
       configs/dram/rowhammer/TrafficGen/hammer.py --victims 293 --duration 1ms
   grep -o "bank [0-9]*, row [0-9]*" m5out/check/dram.log | sort | uniq -c
   ```

## Known issues

- **Flip counts vary between identical runs.** The model picks which weak
  cell flips using a seed from the wall-clock time. Repeat every
  configuration (3 or more times) and report the spread.
- **The request rate only just crosses the threshold.** The default period
  gives each aggressor about 45K activations per 64 ms, so small changes
  matter. For example, 2-sided attacks get about 3% row-buffer hits and fall
  below a 45K threshold. Lower `--period-min/max` for a clear margin.
- **`double_sided_prob` has no effect.** Once a double-sided attack is
  detected, a weak cell always flips (`checkRowHammer()` in
  `dram_interface.cc`).
- **`rowHammerEccCorrected` and `rowHammerEccDetected` are always 0.** The
  ECC code doesn't increment them yet.
- **Only one rank, one channel, and row-high/column-low address mappings**
  (`RoRaBaCoCh`, `RoRaBaChCo`) are supported. The device-map lookup is
  hardcoded to rank 0.
- **Weak cells only exist where the device map lists them.** The vendor-B
  maps (`util/hammersim/row_experiment_vendor_b/`) cover bank 4 only.
  Victims elsewhere will never flip.
- **The order of requests between aggressors isn't controlled.** Each
  generator runs on its own, and the memory bus interleaves them. Patterns
  that need an exact sequence (Blacksmith-style, RowPress) need a
  trace-based generator.
- **Don't use gem5's `-re` flag when you need the script's output.** It
  sends the printed output to a file in the output directory.
