# HammerSim + Memory Band-Aid — CAEN Build & Run Runbook

_Last updated: 2026-11-23_

This is the setup we worked out the hard way. There are **two separate jobs**: you **BUILD** `gem5.opt` once (needs the full toolchain), then you **RUN** experiments (needs only a tiny runtime environment — *no venv*). The build steps below target UMich **CAEN Linux**; teammates on their own machines see section 7.

## TL;DR
- **Build** once per person — section 1. The venv / SCons 4.8.1 / zlib / compiler-wrapper / HDF5 steps are all genuinely required on CAEN.
- **Run** anytime — section 3. Just `module load` + one `LD_LIBRARY_PATH`, then the scripts. No venv.
- **Switching branches** reverts the HDF5 patch (it edits a tracked file), so re-apply it (step 1.8) and rebuild — incremental, minutes.
- **SCons must be 4.8.1** on *any* machine — newer SCons is broken for this fork (see section 7).

## Why each step exists (the gotchas)
Each build step fixes a specific CAEN failure:
- pip-latest **SCons (4.11) is buggy** for gem5 — it mangles the zlib configure test into `-l(` junk. Pin **4.8.1** in a venv.
- CAEN has **no zlib dev headers** → build zlib into `~/.local`.
- SCons **scrubs the environment**, so `CPATH`/`LIBRARY_PATH` don't reach the compiler → use a **compiler wrapper**.
- CAEN headers trip a **`__bitwise` redefinition** that `-Werror` turns fatal → `-Wno-error`.
- CAEN's **HDF5 C++ library is broken** (missing `H5::DataSet::operator=`) → disable HDF5.

## 1. First-time BUILD on CAEN (once per person)

Connect to CAEN in VS Code (Remote-SSH to `login.engin.umich.edu`), open a terminal, then:

**1.1 Clone and pick the branch**
    git clone https://github.com/grecom06/gem5-rowhammer
    cd gem5-rowhammer
    git switch mem_band_aid

**1.2 Load the toolchain modules**
    module load gcc/12.2.1 python/3.11

**1.3 SCons 4.8.1 in a venv** (NOT system/pip-latest — broken for gem5)
    python3 -m venv ~/gem5-venv
    source ~/gem5-venv/bin/activate
    pip install 'scons==4.8.1'

**1.4 Build zlib into `~/.local`** (CAEN lacks dev headers)
    cd ~ && git clone https://github.com/madler/zlib && cd zlib
    ./configure --prefix=$HOME/.local
    make -j4 && make install

**1.5 Export include/lib/runtime paths**
    export CPATH=$HOME/.local/include:$CPATH
    export LIBRARY_PATH=$HOME/.local/lib:$LIBRARY_PATH
    export LD_LIBRARY_PATH=/usr/um/python-3.11/lib:$HOME/.local/lib:$LD_LIBRARY_PATH

**1.6 Compiler wrappers** (force paths in; append `-Wno-error` LAST so it beats gem5's `-Werror`)
    mkdir -p ~/bin
    cat > ~/bin/gcc <<'EOF'
    #!/bin/bash
    exec /opt/rh/gcc-toolset-12/root/usr/bin/gcc -I"$HOME/.local/include" -L"$HOME/.local/lib" "$@" -Wno-error
    EOF
    cat > ~/bin/g++ <<'EOF'
    #!/bin/bash
    exec /opt/rh/gcc-toolset-12/root/usr/bin/g++ -I"$HOME/.local/include" -L"$HOME/.local/lib" "$@" -Wno-error
    EOF
    chmod +x ~/bin/gcc ~/bin/g++
    export PATH=$HOME/bin:$PATH

**1.7 Build the json dependency**
    cd ~/gem5-rowhammer/ext/json
    git clone https://github.com/nlohmann/json.git
    cd json && mkdir build && cd build
    cmake .. -DJSON_BuildTests=OFF && make -j4

**1.8 Disable HDF5** (CAEN's HDF5 C++ lib is broken; it's an optional stats format we don't need). Re-run after every `git switch`:
    python3 - <<'PY'
    import pathlib
    p = pathlib.Path.home() / "gem5-rowhammer/src/base/stats/SConsopts"
    s = p.read_text(); needle = "    if conf.env['CONF']['HAVE_HDF5']:"
    over = "    conf.env['CONF']['HAVE_HDF5'] = False\n\n"
    print("already patched" if "HAVE_HDF5'] = False" in s else (p.write_text(s.replace(needle, over+needle,1)) or "patched"))
    PY

**1.9 Build** (30–60 min; run in `tmux`)
    cd ~/gem5-rowhammer
    scons build/X86/gem5.opt -j4 CC=$HOME/bin/gcc CXX=$HOME/bin/g++ CCFLAGS_EXTRA="-Wno-error"

**1.10 Confirm**
    ls -la build/X86/gem5.opt
    ./build/X86/gem5.opt --help | head

## 2. Make your environment automatic (CAEN)
Add once to the bottom of `~/.bashrc`:
    # >>> gem5 env >>>
    module load gcc/12.2.1 python/3.11
    export PATH=$HOME/bin:$HOME/.local/bin:$PATH
    export CPATH=$HOME/.local/include:$CPATH
    export LIBRARY_PATH=$HOME/.local/lib:$LIBRARY_PATH
    export LD_LIBRARY_PATH=/usr/um/python-3.11/lib:$HOME/.local/lib:$LD_LIBRARY_PATH
    source ~/gem5-venv/bin/activate
    # <<< gem5 env <<<

## 3. RUN experiments (no venv)
If `.bashrc` has section 2 you're set. Otherwise the minimal CAEN runtime env is:
    module load gcc/12.2.1 python/3.11
    export LD_LIBRARY_PATH=/usr/um/python-3.11/lib:$HOME/.local/lib:$LD_LIBRARY_PATH

Unzip the default device map once:
    cd util/hammersim && unzip -o prob-005.json.zip && cd -

Smoke test (no simulation):
    ./build/X86/gem5.opt configs/dram/rowhammer/TrafficGen/hammer.py --victims 293 --print-map

First hammer run:
    ./build/X86/gem5.opt -d m5out/ds configs/dram/rowhammer/TrafficGen/hammer.py --victims 293,7292 --pattern double

A nonzero `rowHammerTotalBitflips` in `m5out/ds/summary.json` is a landed flip.

## 4. Memory Band-Aid test (baseline vs limited)
Always run WITHOUT the limiter first — otherwise "0 flips" proves nothing.
    ATTACK="--victims 50 --pattern double --period-min 1000000 --period-max 1300000 --device-map util/hammersim/row_experiment_vendor_b/dimm0.bank-4.json"
    ./build/X86/gem5.opt -d m5out/mba-off configs/dram/rowhammer/TrafficGen/hammer.py $ATTACK
    ./build/X86/gem5.opt -d m5out/mba-on  configs/dram/rowhammer/TrafficGen/hammer.py $ATTACK --rl-budget 20000

Compare across the two `summary.json`:
- `rowHammerTotalBitflips` — OFF flips, ON should be 0
- `maxRowActsInWindow` — security metric; ON should drop below `--rh-threshold`
- `rlDenials` — >0 on ON means the limiter is actually throttling

If `row_experiment_vendor_b/` is empty, use `--device-map util/hammersim/prob-005.json`.

## 5. Budget sweep
    python3 sweep_budget.py
    python3 sweep_budget.py --budgets off 5000 20000 40000 --reps 5
Output: a printed table plus `m5out/sweep/results.csv` (security + performance columns).
don't add --victim-reader to flip runs.

## 6. Troubleshooting
| Symptom | Cause | Fix |
|---|---|---|
| `scons: command not found` | SCons not installed | venv + `pip install scons==4.8.1` |
| zlib test fails with `-l(` junk | SCons too new (4.9+) | use SCons 4.8.1 |
| `Can't find a working Python installation` | libpython not on loader path | `LD_LIBRARY_PATH` includes `/usr/um/python-3.11/lib` |
| zlib "not found" though installed | SCons scrubs env | compiler wrapper (1.6) |
| `__bitwise redefined [-Werror]` | CAEN header clash | `-Wno-error` in wrapper + `CCFLAGS_EXTRA` |
| `undefined reference to H5::DataSet::operator=` | CAEN HDF5 broken | disable HDF5 (1.8) |
| `tmux: no sessions` after reconnect | CAEN load-balances login hosts | build persists on disk; just resume |
| binary won't run: missing `libpython`/`libz` | runtime env not set | section 3 env block |

## 7. Building on your own machine (not CAEN)
The scripts and run commands are identical everywhere — only the build differs.
- **Easiest: gem5's Docker image.** Clone the fork, build inside `ghcr.io/gem5/ubuntu-24.04_all-dependencies` (Mac/Linux/Windows). All deps pre-installed.
- **Native Ubuntu:** `apt install` the gem5 build deps, then `scons build/X86/gem5.opt`.
- **Still pin SCons 4.8.1** — the zlib-config bug is from this fork vs newer SCons, not CAEN-specific.
- On a clean system you typically do NOT need the zlib/HDF5/`-Werror` workarounds, and you run with no special `LD_LIBRARY_PATH`.