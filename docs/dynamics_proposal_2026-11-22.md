# Proposal: dynamics, the third insert type (2026-11-22)

**State: DECIDED 2026-11-22 (Q1–Q8, §8, §10). Step A done (§9). Step B built and verified in simulation the same day (§10.1: bit-exact in RTL and on the synthesized netlist, mutants 22/22, 11 DSPs, WNS +2.9 ns at 100 MHz; the library, local, not committed). Next: step C's plan, for the user's review.**

**Context:**
- The roadmap's Phase 7 lists "EQ, dynamics, delay". The EQ is done (complete but untested, `eq_proposal_2026-11-17.md` §7.9).
- Dynamics is the first block that needs a **second input** (a sidechain key). The library foundation planned for that (LF6, LF7: `library_foundation_proposal_2026-10-10.md` §5) before the insert architecture existed. §4 below revisits it in terms of the router.
- **From the insert work, unchanged:**
  - a pool of 32 slots behind the insert router (`insert_architecture_proposal_2026-10-31.md`);
  - pre-fader, any zone, any order, several on one channel;
  - an exact identity setting;
  - linked through virtual groups;
  - the controller treats it as a generic pool type (`inserts_controller_spec_2026-10-31.md`).

---

## 1. What a slot does

Each slot is one channel's **compressor, expander or gate** (the mode, Q1). It is the textbook log-domain design (Giannoulis, Massberg, Reiss, *Digital Dynamic Range Compressor Design*, JAES 2012): a feed-forward detector that smooths the **gain**, not the signal.

```
 main x ───────────────────────────────────────────────────────────►(×)──► out
                                                                     ▲
 key k ─► [key HPF] ─► |·| ─► log2 ─► [link: max of group] ─► static curve ─► ballistics ─► + makeup ─► exp2
 (x itself, or another channel)                                   (threshold,   (attack, hold,
                                                                   ratio, knee,   release)
                                                                   range)
```

- **Level:** `L = log2|k|` in fixed point (1 unit = 6.02 dB). Leading-zero count plus a small table: cheap, and every later step is then additions and one multiply each.
- **The static curve** turns the level into a target gain change `G` (≤ 0, in log2 units):
  - *Compressor:* above the threshold T, `G = (1/R − 1)(L − T)`. A soft knee of width W blends it in quadratically over T ± W/2.
  - *Expander:* below T, `G = (R − 1)(L − T)`, floored at the range (e.g. −40 dB).
  - *Gate:* an expander at ∞:1. The gain jumps to the range below T, with a fixed hysteresis (closes 3 dB below where it opens), so it doesn't chatter.
- **Ballistics:** a one-pole on `G` with two speeds: attack while the gain is falling, release while it rises. A **hold** keeps a gate open for its hold time before it may close. The coefficients come from the server in milliseconds' terms (`α = 1 − e^(−1/(fs·τ))`).
- **Makeup** adds a fixed gain. Then `exp2` turns the sum back into a linear gain and **one multiply** applies it to the main signal.
- **Peak detection only**, no RMS: with slow attack it behaves like RMS, and the log-domain smoothing doesn't pump on bass.
- **No lookahead, on purpose:** lookahead means delaying the channel, and the router's rule is that **no insert changes a channel's timing** (§3 of the insert proposal: it would comb-filter on a bus). So a "limiter" here is a compressor at ∞:1 with a fast attack. Its first few samples of a sudden peak can pass. A true brickwall limiter would have to delay a whole zone (the outputs, say) by a fixed amount. That's a separate feature, not part of this proposal.

## 2. Identity, inserting, and mode changes

- **Identity** is the compressor at **ratio 1:1 and makeup 0 dB**. The curve's slope is then exactly 0, so `G` = 0, the ballistic state stays 0, and `exp2(0)` is exactly 1.0 (the table's first entry). The multiply by an exact 1.0 is bit-exact. This holds whatever the input is, including silence (`log2 0` clamps to the bottom of the range).
- `dyn_on` 0 gives the identity coefficients (exact bypass), as with the EQ.
- **Inserting** is the router's usual sequence: identity, route in, then the user's settings, faded in by the smoother.
- **Mode changes go through identity.** A compressor turning straight into a gate would jump. The server first fades to identity, then switches the mode word, then fades to the new settings. This is the same rule as the EQ's type changes through flat.
- **Some words must not be smoothed:** the mode, the hold time and the link group. They're integers, and a glide between two of them means nothing. `coef_smooth` today smooths every word, so the library gets a small generic change: a per-lane **no-smooth mask** (default none, so every other user stays as it is). The alternative, a second unsmoothed bank, costs a BRAM and a second COMMIT path, so I'd go with the mask.

## 3. Stereo and group linking (Q4)

On a stereo pair, two independent compressors pull the image toward whichever side is louder. Consoles link the detectors so that both channels get the same gain.

- **In the block:** each slot carries a **link group** (0 = none, else 1 … 31). The block runs in two passes per frame:
  1. as the slots' inputs arrive, it computes each level and keeps the largest per group;
  2. after the last input, each slot takes its group's largest level, then the curve, ballistics and gain.

  Linked slots with the same settings then compute the **same** gain, sample for sample. For N members this is exact, not just for pairs.
- **The cost of two passes:** the inputs wait in a 32-entry buffer. Each sample is stored **raw** (24 bits) and widened on read: that's the lesson of `eq1`, where Vivado trimmed a RAM of widened words and lost the sign. The last output comes about 32 + 20 cycles after the last input, well inside the router's budget of 221. **So a dynamics insert adds no latency** beyond the router's fixed 9 frames, same as the EQ.
- **In the server:** dynamics slots on the channels of one virtual group share a link group automatically. Inserts already follow groups (the same settings, position by position), so a linked detector is the natural next step: *"the driving reason behind virtual groups"*.

## 4. The sidechain key (LF6, LF7 revisited; Q2)

LF7 planned a separate frame tap so a block could read another channel one frame late. **The router makes that unnecessary:** its history buffers already hold every zone channel's last K + 2 frames, and a SOURCE entry can already name "zone channel c at age a".

- **A KEY table** in the router, one entry per slot of each pool that declares a key input (only dynamics, for now). It uses the same entry format as SOURCE: none (the slot keys from its own input), a zone channel at an age, or another slot's output.
- **A second input stream** to that pool, `pool_key`, beat for beat beside `pool_in`. A pool without a key gets no table and no stream, so the gain pool and the EQ don't change.
- **The same bank and COMMIT** as the other tables (one more read port on the router's `coef_bank_ram`), so a chain and its key change in the same frame.
- **Alignment:** the server picks the key's age to line it up with the main sample the slot sees.
  - Within a zone (input keyed by input) this is **sample-exact**.
  - Across zones (a bus keyed by an input channel) the history may not reach far enough back. The key is then up to about 9 samples (0.2 ms) **early**, which is a touch of free lookahead for detection and harmless.
- **Choices, as LF6 decided:** the input channels and the buses. A key is taken at that channel's insert point: after the input patch (or the input matrix, for a bus), before that channel's own inserts and level. The classic uses all fit: ducking a music bus under a voice input, keying a gate from a kick drum channel.
- **The key filter (Q3):** a 2nd-order high-pass on the key, so the bass doesn't drive the compressor. It is one state-variable section, the same arithmetic as the EQ's high-pass band, at detection precision (18-bit coefficients, so about 3 DSPs instead of the EQ lane's 14). It's off by default.

## 5. The modules (the pool zone `dyn`, per slot; group `dyn`; all linked except `dyn_key`)

| Module | Type | Range | Default |
|---|---|---|---|
| `dyn_on` | bool | | 1 |
| `dyn_mode` | enum, labels | Compressor, Expander, Gate | Compressor |
| `dyn_threshold` | float, dBFS | −60 … 0 | −20 |
| `dyn_ratio` | float | 1 … 20, and ∞ (sent as 100) | **1.0** (identity) |
| `dyn_knee` | float, dB | 0 … 24 | 6 |
| `dyn_attack` | float, ms | 0.05 … 200 | 10 |
| `dyn_hold` | float, ms | 0 … 2000 | 0 (the gate's: 50) |
| `dyn_release` | float, ms | 5 … 5000 | 150 |
| `dyn_range` | float, dB | −80 … 0 | −40 (expander and gate only) |
| `dyn_makeup` | float, dB | 0 … +24 | 0 |
| `dyn_key` | enum, optionLabels | Self, Input 1 … N, Bus 1 … N | Self |
| `dyn_key_hpf` | float, Hz; 0 = off | 0, 20 … 500 | 0 |

- 12 modules per slot.
- **The defaults are the identity** (ratio 1:1, makeup 0), like the EQ's flat bands. Inserting a compressor changes nothing until the ratio is moved, and a fresh slot passes the router's bit-exact front edit. The threshold, knee and times have useful defaults, so turning the ratio up gives a usable compressor straight away.
- **`dyn_key` follows the channel counts.** Its labels are built like `source`'s (`optionLabels`), and they change with `system/inputCount` / `busCount` (`config/changed`).
- **`dyn_key` doesn't link through groups.** Each member of a stereo pair keys from its own side, or both from the same external key. That's each channel's choice, and the shared link group (§3) already gives them the same gain.

**The gain-reduction meter (Q5):** a compressor you can't see is hard to set. Each slot's current gain is readable in the block's window (32 words, read-only). The server streams it as a new meter zone, `dyn` (bit 3 of the subscribe mask, one value per slot, the existing blob format), at the meter rate. Controllers that don't ask for bit 3 see no change. **The app work is yours**, from an amended spec.

## 6. What it costs (estimates; step A's measured numbers are in §9: 11 DSPs, 14 words per slot)

| | |
|---|---|
| DSPs | about 8 for the slot path (log2 interpolation, knee, slope, ballistics, exp2 interpolation, the gain multiply), plus about 3 for the key filter. Today: 42 of 360 |
| BRAM | the coefficient bank (+ smoother), the key filter's states, the ballistic states: about 3 tiles. Today: 33 of 216 |
| Window | 4 KB at **0x8001_4000** (after the EQ's 8 KB at 0x8001_2000): about 18 words per slot plus the 32 meter words |
| Router | the KEY table (32 entries), one more read port, the key stream: a few hundred LUTs |

## 7. The steps

**A. The prototype (Python, library `model/`, on the PC).** It decides the number formats from measured numbers, and it becomes the block's reference model.
- A floating-point reference (the paper's equations), then a bit-true fixed-point model.
- **Measured:**
  - the static curve against the reference: target under 0.1 dB everywhere, knee included;
  - the time constants: do 0.05 ms and 5 s both come out right? The 5 s release needs `α` ≈ 4·10⁻⁶. That is the fixed-point trap: too few state bits and the release sticks short of its target, the deadband the EQ cured with error feedback;
  - the gain's quantization: no audible zipper on a slow release;
  - identity bit-exact;
  - linked slots bit-identical;
  - the key filter's response.
- **Milestone: I report the numbers and the chosen formats before any RTL**, as for the EQ.

**B. The block `lib_dynamics` (library, from the template).**
- The RTL, its window (ID, CONFIG, coefficients, the meter words), and its TB bit-exact against the model. The TB covers every mode, linked groups, the key, coefficient changes mid-run, overloads, and the pool budget.
- **The netlist run** (`scripts/netsim.ps1`: the TB on Vivado's synthesized netlist), now a rule for every block after `eq1`.
- Mutants, an OOC synthesis.
- `coef_smooth`'s no-smooth mask (§2), with its own TB case.

**C. FPGAmixer.**
- **The router:** the KEY table and the `pool_key` stream for the pools that declare a key, with its tests (keys aligned within a zone, a key change landing with its chain in one frame) and mutants.
- **The third pool:**
  - NP = 3;
  - the window on the second SmartConnect (a block-design change);
  - the device-tree node, derived by hand as for `eq1` and `tc1` if `sdtgen` is still blocked by Device Guard.
- **The server:**
  - `PoolType("dyn", …)`;
  - a `DynPoolDriver` and `mixer_dyn.py` (the coefficient maths, a copy checked against the library's model);
  - the key's ages, the link groups from virtual groups, mode changes through identity;
  - the `dyn` meter zone.
- **The OSC standard and the controller spec** amended (the modules, `dyn_key`, the meter zone).
- Then the regression, `tb_top_windows` with a dynamics insert, a Vivado check, and the image.

**D. The bench (yours), or into the batch test (Q7).**
- A flat (1:1) dynamics insert in the T1 path, bit-exact.
- By ear: compressor, expander and gate on program material; fast and slow times; a stereo pair in a group (the image must hold still); a bus ducked by a voice input; the key filter.
- The meter follows the gain reduction.

## 8. Questions for the user

- **Q1, one block with modes, or two pool types:**
  - **one `dyn` pool**, each slot a Compressor, Expander or Gate (recommended). A channel that wants a gate and a compressor takes two slots, in either order.
  - The catch: **32 slots shared** between gates and compressors. Twenty input channels with both would need 40.
  - The alternative is two pools, `gate` and `comp`, 32 each, at about twice the DSPs (still small). `S` is a build parameter, so the one pool could grow later instead, within the router's frame budget.
- **Q2, the key's choices:** input channels and buses, as LF6 decided (recommended). The alternative adds the output channels.
- **Q3, the key filter:** in, a 2nd-order high-pass, off by default (recommended: it's what a channel compressor gets asked for most, and the arithmetic exists). The alternative leaves it for later.
- **Q4, linking through virtual groups:** dynamics on grouped channels share one detector automatically (recommended). The alternative is independent detection, plus a `dyn_link` module so the user decides.
- **Q5, the gain-reduction meter:** a new meter zone `dyn` (recommended; the app work is yours). The alternative is a get-only module the app polls, which is coarser and puts more load on TCP.
- **Q6, the ranges and defaults** in §5, with ratio 1:1 by default so that a fresh insert is the identity (recommended). The alternative is a working default (4:1 at −20 dB): the server would then insert at identity and fade to it, so a fresh insert audibly compresses.
- **Q7, the bench:** fold step D into the batch test with the EQ and timecode (recommended, matching what you said on 2026-11-18), or bench it on its own image first.

**Decided (the user, 2026-11-22): all as recommended.**
- Q1: one `dyn` pool with modes (Compressor, Expander, Gate), 32 slots shared.
- Q2: keys from the input channels and the buses.
- Q3: the key high-pass is in, off by default.
- Q4: detection linked automatically through virtual groups.
- Q5: the gain-reduction meter as meter zone `dyn` (mask bit 3).
- Q6: the ranges and defaults of §5, ratio 1:1 by default (a fresh insert is the identity).
- Q7: step D goes into the batch test with the EQ and timecode.

---

## 9. Step A results (2026-11-22; the milestone before RTL)

**Where:** the library's `model/` (local, not pushed):
- `dynamics.py`: the settings, the design formulas, the slot's words, the float reference (`FloatPool`) and the bit-true fixed point (`FixedPool`);
- `dyn_study.py` and its output `dyn_study_report.txt` (about 10 s);
- `test_dynamics.py`, 20 tests, passing. The EQ's own tests still pass.

**The formats chosen:**

| What | Format |
|---|---|
| levels, threshold, knee half-width, range, hysteresis, makeup, the reduction | log2 units, **16 fraction bits** (1 LSB = 0.0001 dB) |
| slope `s`, the knee's `1/(2W)` | 12 fraction bits |
| the ballistic state | 32 fraction bits, plus a **snap**: when a step rounds to 0, the state takes its target exactly. That removes the deadband |
| attack and release | a small float: a 17-bit mantissa and a shift, so 0.05 ms and 5 s are both exact to 0.002 % |
| log2 and exp2 | **128 linear segments** (two 129-entry ROMs), one multiply each. 64 segments were measured too: they leave 0.0002 dB of gain error, which modulates as the gain moves (−93 dB instead of −102 dB) |
| the gain | a 17-bit mantissa and a shift: one 24 × 17 multiply. `exp2(0)` is exactly 1.0 |
| the key high-pass | the EQ's state-variable filter in its **g form**: 27-bit signals, 18-bit coefficients, **error feedback on all three roundings** (format B below) |

**A slot has 14 words**, 448 for 32 slots, which fits a 4 KB window with the meters. The proposal guessed about 18. Seven are smoothed (threshold, slope, knee ×2, range, hysteresis, makeup). Seven jump: the control word (mode, key filter on, link group), attack, release, hold, and the key filter's three. They jump because they are integers or packed floats, which a glide would garble, and jumping them is harmless: a time constant changes speed, not level, and the key filter only feeds the detector. **All-zero words are the identity**, so a bank's reset state is.

**Measured (`dyn_study_report.txt`):**

| | Result | Target |
|---|---|---|
| log2, exp2 | worst 0.00026 dB, 0.00015 dB | ≪ 0.01 dB |
| the static curve, level → gain, every mode, threshold, ratio, knee and makeup at the extremes (166,060 points) | compressor **0.006 dB**, expander 0.005 dB (hard knee) and **0.038 dB** (a 24 dB knee at 20:1, where `s` = 19 amplifies the rounding) | < 0.1 dB |
| time constants, 0.05 ms … 5 s | the 63 % point on the exact sample, apart from rounding to whole samples (0.05 ms is 2.4 samples) | |
| the 5 s release | reaches its target **exactly**; the float model takes about 75 % as long to get within 0.001 dB | no deadband |
| arithmetic noise under a 5 s release (gain zipper) | **101.8 dB** below the signal; the 24-bit output's own rounding alone gives 113 dB | inaudible |
| identity | bit-exact, 48,002 samples, full scale included | bit-exact |
| linked slots | identical gains in 48,000 / 48,000 samples (unlinked: 2) | identical |
| the key high-pass, B (20 … 500 Hz) | response within 0.015 dB of exact; stable (radius ≤ 0.998); **no DC residual**; 76 dB SNR at −60 dBFS | ~60 dB |
| the smoother (K = 8) | the worst gain step per sample: 0.09 … 0.20 dB (unsmoothed: up to 24 dB at once) | no clicks |

**Two findings that change the plan:**
1. **The key high-pass needs error feedback on all three roundings, not two.** With two, a DC key left a residual of up to −90 dBFS at a 20 Hz corner; with three, none at any corner. The remainders enter the DSPs' C inputs, so it costs no multiplies. The EQ's own a form (format A) is just as clean but costs 2 DSPs per product.
2. **A removal reaches bit-exact identity only once the reduction has fully released.** From 10:1 at −30 dBFS that takes **1.9 s at a 150 ms release**, 262 ms at 20 ms, 75 ms at 5 ms. So the server's removal (and mode-change) sequence should also switch the release to a fast one while it fades to identity. Release is a jumping word, so switching it doesn't click; it only makes the last of the gain come back sooner. **Q8 below.**

**The DSP count is 11, not the 8 + 3 estimated:** the key high-pass 4, log2 1, the knee 2, the slope 1, the ballistics 1, exp2 1, the gain 1. That's still less than one EQ lane (14). Today's design uses 42 of the 360.

## 10. Step B plan: the block `lib_dynamics` (for the user's review)

A library block from the template, generic: S slots, no knowledge of FPGAmixer, bit-exact against `FixedPool`.

**Ports:** the pool contract's stream in and out, plus a **key stream** (beat for beat beside the input: same valid, same slot). Standalone, and in the router when a KEY entry is none, the key is the input itself.

**The schedule: two passes per frame** (§3):
- **Pass 1**, one slot per cycle as the inputs arrive: the key filter (always run, used if on), log2, the link group's running maximum. The raw input samples, each slot's level and its link group go into 32-entry buffers. The samples are stored **raw**, the `eq1` lesson.
- **Pass 2**, after the last input: per slot, its group's level (or its own), the curve, the ballistics, exp2 and the gain, one slot per cycle, fully pipelined.
- The last output comes about 32 + 25 cycles after the last input. The pool budget is 221.

**The coefficients:**
- **two bank entries per slot** on two read ports of one bank (`PORTS` = 2, one handshake, as the EQ's lanes):
  - entry A, read in pass 1: the control word and the key filter's three;
  - entry B, read in pass 2: everything else;
- each entry is then read exactly once per frame, which is the smoother's rule;
- `coef_smooth` gets a generic **per-lane no-smooth mask** (default none), with a case in its TB;
- `coef_swap` on the last input beat, as in the template.

**The state:** per slot, the ballistic state, the hold counter and the gate's flag (about 56 bits), and the key filter's two states and three remainders (about 112 bits). All of it fits in LUT-RAM.

**The window** (`lib_dynamics_regs_axil`):
- ID `0x4459_5001` ("DY"); CONFIG: slots and words;
- the two entries per slot;
- 32 read-only meter words: each slot's reduction, 16 fraction bits;
- 4 KB.

**Tests (on the PC):**
- `model/dyn_vectors.py` writes the vectors from `FixedPool`:
  - every mode, link groups of 1, 2 and 3, external keys, the key filter on and off;
  - settings changed mid-run through the smoother;
  - overloads (+24 dB makeup on a full-scale input);
  - a removal to identity.
- `tb_lib_dynamics` checks every output and meter bit-exact, the stream contract, the pool budget, identity, and each entry read once per frame.
- **The netlist run** (`scripts/netsim.ps1`), then mutants (a wrong segment index, no snap, the hold off by one, the hysteresis inverted, the link maximum taken over a stale frame, a missed remainder, …), then an OOC synthesis.

**Not in step B:** the router's KEY table and key stream, the third pool in FPGAmixer, and the server. Those are step C.

**Q8, for step C (the server), asked now because step A found it:** the release a removal or mode change switches to while it fades to identity.
- **20 ms** (recommended): bit-exact after about a quarter second; the remaining reduction comes back as a gentle 20 ms release.
- **5 ms**: done in under 0.1 s, but a deep reduction comes back fast enough to hear as a quick swell.
- The user's own release: no change in sound, but up to several seconds before the slot is free (about 2 s at 150 ms, much longer at 5 s).

**Decided (the user, 2026-11-22): build step B as planned, and Q8 = 20 ms** (the go-ahead was asked as "step B as planned, with a 20 ms removal release").

### 10.1 Step B built and verified in simulation (2026-11-22; the library, local, not committed)

**For a while, the simulator was blocked.** Windows Device Guard refused `xsim.exe` ("blocked by your organization's Device Guard policy"), three times. The user unblocked it the same day, and every simulation below ran after that. Two later launches failed with "child exe not found" (the known Defender launch failure, a tool failure, never a verdict); both passed on a re-run.

**What was built:**
- `rtl/coef_smooth.sv`: the `NOSMOOTH` lane mask (default none, so every existing user is unchanged). `sim/tb_coef_smooth.sv` gets a fifth unit (6 words × 4 lanes, K 4, lanes 1 and 3 passed through), checking that a masked lane jumps.
- `blocks/dynamics/`:
  - `lib_dynamics_pkg.sv`: the formats, the lanes, the latencies, `fits`, and the two 129-entry tables, generated from the model. `dyn_vectors.py` checks the tables against the model on every run, so they can't drift;
  - `lib_dynamics.sv`: the block;
  - `lib_dynamics_regs_axil.sv`: the window, ID `0x4459_5001`, 4 KB, reset = all zero = identity;
  - `tb_lib_dynamics.sv`: the testbench.
- `model/dyn_vectors.py`: 2,400 frames × 32 slots in four phases: identity, random settings, the extremes, a removal. It covers every mode, link groups of 1, 2 and 3 members and group 31, external keys, the key filter at 20, 100 and 500 Hz, the slow and fast alphas, holds, 10,441 saturated outputs and 420 gate moves. The removal phase must end bit-exact at identity, and the generator checks that it does. Three slots are set deliberately, to close the mutants the random settings left alive (below):
  - slot 8, an instant attack, rises only through the snap;
  - slot 16, an expander keyed by a silent slot, shows the zero key's floor;
  - slot 22, a compressor and then a gate at −15 dB, moves into the gate's hysteresis band as it becomes a gate.
- `scripts/xsim_regress.ps1` and `scripts/netsim.ps1`: a `dynamics` target each. `scripts/mutants/dynamics.json`: 22 mutants, each checked to match its source line exactly once.

**Three departures from the §10 plan, all simplifications:**
1. **One read port, not two.**
   - A word has 11 lanes. Entry A uses lanes 7–10, entry B lanes 0–9, and lanes 7–10 hold the words that must not glide in both entries, so one `NOSMOOTH` mask fits both.
   - Pass 1 reads entry A, and pass 2 reads entry B later on the same port. That halves the bank's RAM copies.
2. **Pass 1 runs after the last input beat**, not as the beats arrive, as the EQ does. Every coefficient read then comes after `coef_swap`, so a frame sees one bank in both passes. Read as the beats arrived, entry A would have come from the old bank and entry B from the new one.
3. **The meters are a stream** (`mtr_valid`, `mtr_ch`, `mtr_data`, one per slot per frame), not words in the dynamics window. The library block stays free of control-plane reads. In step C the platform takes the stream into a status window, the way it already reads its level meters.

**Timing:** pass 1 issues slot s at t = 1 + s; pass 2 at t = 49 + s. The first output comes 67 cycles after the last input beat and the last 98, against a pool budget of 221. So the router's latency is unchanged.

**Checked:**

| Check | Result |
|---|---|
| the library's regression (`xsim_regress.ps1`, 5 targets) | **all pass**; `dynamics`: **76,800 samples and 76,800 meters bit-exact** over 2,400 frames, the latencies exact (67 / 98), each word read once per frame, all after the swap |
| `smooth` with the `NOSMOOTH` unit | pass: 15,204 reads, 376 jumps, 0 errors |
| **the netlist run** (`netsim.ps1 dynamics`: the same TB on Vivado's synthesized netlist) | **bit-exact**, so the trimmed registers below are sound |
| mutants (`mutants/dynamics.json`: 21 in the block, 1 in `coef_smooth`) | **22 / 22 killed**. The first run killed 19; the three survivors (no snap, a zero key not floored, the gate's flag moving in every mode) were paths the vectors never reached. Slots 8, 16 and 22 now reach them, each first confirmed on the model: 2,240, 2,828 and 1,664 differences, exactly the error counts the re-run reported |
| FPGAmixer's regression (20 targets: `smooth_blocks`, `router`, `topwin` included), for the `coef_smooth` change | **all pass** |
| the models' tests (`test_dynamics`, `test_svf_eq`) | pass |

- **Out-of-context synthesis** (`scripts/synth_ooc.tcl`, xczu3eg, 100 MHz; mclk is 12.288 MHz):

| | `lib_dynamics` | for scale: `lib_svf_eq` |
|---|---|---|
| WNS | +2.926 ns | +3.74 ns |
| DSP48E2 | **11** (the study's count) | 28 |
| LUTs (of which LUT-RAM) | 2,687 (490) | |
| registers | 1,570 | |
| BRAM tiles | 1 (the two tables) | 4 |

  The first synthesis gave 13 DSPs: `s` and `inv2w` were declared at the lane's 23 bits, so their products split over two DSPs each. Their values fit 18 bits, and taking 18 gives one DSP each.
- **The RAMs are inferred at their full widths:** the input and key buffers 24 bits, raw; the states 112 and 58 bits. That's the `eq1` failure class, and it doesn't appear here. Six registers are trimmed:
  - sign copies of the raw key;
  - stages absorbed into a DSP's input register or the ROM's address register;
  - bits beyond a rounding.

  The netlist run passes bit-exact, so the trims are sound.

**Not done in step B:** nothing is committed (the library first, then FPGAmixer's submodule pointer, as for the EQ). Step C is next: the router's KEY table and key stream, the third pool in FPGAmixer, the meter window, the server, a Vivado check, and the image. Its plan comes first, for your review.
