# Fixed Normalization Convention: Conform to SimPEG static_instrument, Drop the Ceremony

**Status:** Implemented. Corrected 2026-10-21 by `gatefactor-is-calibration.md`: `GateFactor` is the calibration amplitude factor, not receiver geometry, the receiver area is already in the delivered data, and the `rx_normalized` option is removed. The text below is corrected to match.
**Repos touched:** `deps/aem-processes` (importers), `deps/ymerflow-processing` (dataset + filters). `deps/simpeg/simpeg/electromagnetics/utils/static_instrument` is the **definitive, read-only authority** — its assumptions are not changed; everything else conforms to it. `deps/emerald-processing-em` is a dependency but **not on the live inversion path** (see below).
**Context:** The `import-declares-normalization` change (aem-processes) plus ymerflow-processing's `declare_normalization`/`convert_normalization` steps and the three-valued `Dataset.normalization` turned the amplitude convention into mutable metadata declared and converted by pipeline steps. In practice it is ceremony: the convert step is commented out of the standard pipeline, the declare step only re-asserts what import wrote, and — critically — the one engine that actually inverts the data (`simpeg.static_instrument`) ignores the field entirely and has its own fixed, documented convention. This plan deletes the ceremony and makes the on-disk convention exactly what SimPEG static_instrument reads.

---

## The definitive convention (from `simpeg/.../static_instrument`, `data_format.md` + code)

SimPEG static_instrument is the authority and **is not being changed**. Its on-disk format:

1. **`dbdt_chNgt` — measured dBz/dt, already normalized *per unit transmitter moment*** (the transmitter moment is divided out), in **raw instrument units** (e.g. nV/m², not SI). This is what a SkyTEM/Aarhus delivery already contains. The forward proves the assumption: the SimPEG source carries **unit dipole moment** (`current = 1/area`) *because the data are dB/dt normalised per unit transmitter moment* (`dual.py:344-352`).
2. **`scalefactor` (model_info)** — dimensionless multiplier, raw → **V/m² (= T/s)**, the SI unit SimPEG's receiver expects. Applied **at read**: `dbdt × scalefactor` (`base.py:308`, `dual.py:251`). Validation: `mean(-dbdt × scalefactor) < 1e-3` (`base.py:309`). **Not baked into the data.**
3. **`GateFactor` (GEX, per channel)** — the calibration amplitude factor, applied **at read**: `-dbdt × scalefactor × GateFactor × tiltcorrection` (`dual.py:251`). **Not baked into the data.** Its companion `GateTimeShift` is the calibration time shift, added to the gate times by libaarhusxyz (`GEX.gate_times`).
4. **Transmitter moment is *not* applied to the data at read.** The real `ApproxDipoleMoment` (= `nturns × TxLoopArea × TxApproximateCurrent`, nominal) is used **only** for the noise floor (below). The data stays per-unit-moment.
5. **`dbdt_std_chNgt` — dimensionless relative fraction** of the data, not an absolute standard deviation (`dual.py:253`, `data_format.md`).

**Noise floor + uncertainty** (`base.py:407-435`):

```
N_i(t) = (noise_level_1ms / dipole_moment_i) · (t · 1000)^noise_exponent      [noise_exponent default −0.5]
σ      = max(std_fraction, std_data) · |data| + N(t)                          [std_data default 0.03]
```

- `noise_level_1ms` default `1e-9`, docstring unit **V/Am²** — i.e. the floor is **stated before moment normalization** and divided by the per-channel `dipole_moment_i` to land in the same per-unit-moment scale as the data. `dipole_moment_i = ApproxDipoleMoment` (`dual.py:143-145`; base default `[1]`).
- `data` here is `dbdt × scalefactor × GateFactor` (per-unit-moment V/m²). `σ` is absolute, in that same unit.

### What this means for "data on disk is normalized by transmitter AND receiver moment"

Right, with two factors that stay out of the arrays:

- **Transmitter moment — yes, in the data.** Divided out on disk, by convention, as SkyTEM delivers it. This is the real fixed invariant.
- **Receiver area — yes, in the data.** The delivered dB/dt is already normalized by it.
- **Calibration — no, NOT in the data.** It is `GateFactor` (amplitude) and `GateTimeShift` (time), applied at read from the GEX.
- **Unit scaling — no, NOT in the data.** It is `scalefactor`, applied at read from model_info.

So **`scalefactor` and `GateFactor` are load-bearing metadata and must survive on disk** — "ditch all metadata" cannot mean them. What we ditch is the *ymerflow-processing normalization field and its declare/convert steps*, which are a separate, redundant layer the definitive engine never reads.

### emerald is not on the live path (and assumes the opposite)

The platform's inversion and forward load systems from the `simpeg.static_instrument` entry-point group (`inversion_process.py:28`, `forward_process.py:27`, `setup.py:50-55`). `emeraldprocessing` is an installed dependency but its `normalize_data_for_simpeg` is **not** invoked by these paths. Note for the record: emerald assumes the *opposite* on-disk convention — receiver-normalized (V/m²) input with the **transmitter moment NOT yet divided out**, which it then divides itself. A SimPEG-format file (tx already divided out) fed to emerald with its default would be double-divided by the moment (3–4 orders of magnitude, silent). Since emerald is off-path this is not a live risk, but any future use of emerald on these files must set its `input_normalization` to match — do **not** point emerald at these files on its defaults.

---

## Change 1 — XYZ/GEX importer converts any normalization to the SimPEG format (`aem-processes/aem_processes/import_process.py`)

The importer's job is to land data in exactly the SimPEG format regardless of how the upload was normalized. **The SimPEG format is: `dbdt_chNgt` per unit transmitter moment (tx divided out), with the receiver area already in the delivered data — `scalefactor` (model_info) converts raw → V/m² and `GateFactor` (GEX) applies the calibration amplitude factor, both at read; `dbdt_std_chNgt` a dimensionless fraction.**

The uploaded data varies on one axis the importer reconciles — is the transmitter moment already divided out — declared by one boolean (checkbox). The default reflects a SkyTEM / Aarhus delivery, which is the current setup: tx divided out.

```python
"tx_normalized": {
    "type": "boolean",
    "title": "Transmitter-moment normalized",
    "description": "The uploaded dB/dt is already divided by the transmitter moment "
                   "(per unit dipole moment). True for SkyTEM / Aarhus Workbench deliveries. "
                   "If false, the importer divides each channel by its nominal dipole moment.",
    "default": true
}
```

- **Transmitter action** — when tx is not yet divided out, divide each channel's `dbdt_chNgt` by its nominal `ApproxDipoleMoment` (`nturns × TxLoopArea × TxApproximateCurrent`). Fail loudly if a channel's moment is absent from the GEX — the convention cannot be produced.
- **Calibration** — the delivered `GateFactor` and `GateTimeShift` are kept as the GEX gives them; SimPEG applies the factor and libaarhusxyz the shift at read. The importer never writes either.
- **`scalefactor`** stays a model_info value and a separate parameter (raw → V/m²); it is never multiplied into the arrays. After a correct import, `mean(-dbdt × scalefactor × GateFactor)` must satisfy SimPEG's `< 1e-3` validation — **add an import-time assertion mirroring `base.py:309` as a hard error that blocks the import** (decided, open decision #3), since there is no longer a convention field to cross-check against. A hard error, not a warning: it is the only remaining automatic guard, and SimPEG would reject the same data at inversion anyway — failing at import is where the GEX is present and the inputs are still fixable.
- **Remove `xyz.model_info.setdefault("normalization", "rx_tx")`** (`import_process.py:160`). The ymerflow convention field is gone; `scalefactor` and `GateFactor` remain.
- **Target engine:** measured data is read by `MeasuredTEMXYZSystem`, which applies `× scalefactor × GateFactor` for **any number of moments, 1…N** (`dual.py:251`) and reads a GEX. There is no separate single-moment path that skips `GateFactor` — the `base.py:308` line is only the base class's validation `assert`, not a data read. So the calibration applies to every measured import. `SyntheticTEMXYZSystem` has no GEX and behaves as `GateFactor = 1` throughout; it is not an import target but see Change 3 for how the noise step must tolerate a missing GEX.

## Change 2 — msgpack importer assumes SimPEG format (`aem-processes/aem_processes/import_msgpack_process.py`)

**None of Change 1 applies to the msgpack importer.** A ymerflow msgpack container is assumed to be **already in the SimPEG convention** (per unit transmitter moment, the calibration carried by `GateFactor`, with its own `scalefactor`). There is **no `tx_normalized` parameter and no amplitude conversion** — the container is passed through as-is. Strip any stale `model_info["normalization"]` an older container carries so no dead field survives.

## Change 3 — Remove the ceremony from ymerflow-processing (`deps/ymerflow-processing`)

ymerflow-processing is the pre-inversion processing library; it must read amplitudes the **same way SimPEG does** so its culling/uncertainty decisions bind at inversion.

- **`filters/normalize.py`:** delete `declare_normalization`, `convert_normalization`, `recorded_normalization`. Keep `apply_nominal_current` (it is about the current column, not the amplitude convention).
- **`dataset.py`:** remove the three-valued `Dataset.normalization` property/argument, the `NORMALIZATIONS`/`RAW`/`RX_NORMALIZED`/`RX_TX_NORMALIZED` constants, and the `normalization` argument + `rx` default in `load()` (`dataset.py:582, 951-1020`). This removes the live `load()`-defaults-to-`rx` landmine. The data simply *is* the SimPEG convention.
- **`filters/noise.py` (`NoiseModel`):** make it numerically identical to SimPEG's floor —
  `N(t) = (noise_level_1ms / dipole_moment) · (t·1000)^exponent`, `σ = max(fraction, std_data)·|data| + N(t)`,
  same `noise_level_1ms` units (V/Am², pre-moment), same `1e3` ms reference, same `−0.5` default, divided by the same per-channel nominal `ApproxDipoleMoment`. Drop the general raw/rx/rx_tx conversion matrix; the only amplitude conversion that remains is the mandatory divide-by-dipole-moment, which is irreducible.
  - **Dipole moment is already available** — TEM channels carry it (`methods/tem.py:150,307`, from the GEX `ApproxDipoleMoment`) and the callers (`cull_noise_floor.py:121`, `uncertainty.py:114`, `figures.py:424`) already pass it. No new wiring there.
  - **`scalefactor` and `GateFactor` are NOT read anywhere in ymerflow-processing today** (verified: zero references to either). The floor is currently a ratio `floor / |value|` with both sides in raw array units. For the **estimated-floor** path this is self-consistent (floor from `_scatter` over the same raw arrays; `scalefactor`/`GateFactor` cancel in the ratio) and already matches SimPEG. For the **supplied-floor** path (`noise_level_1ms` given by the user, as in `manual-processing.yaml`) the number is physical (V/Am²) but is divided by raw `dbdt`, so it is wrong by exactly `scalefactor × GateFactor` — with the import default `scalefactor = 1e-12` that is a ~10¹² silent error, the precise failure mode this effort targets.
  - **Decided (open decision #5 = Option A): a supplied `noise_level_1ms` is in physical units (V/Am²), identical to SimPEG.** So the noise step must newly read `scalefactor` (`dataset.survey.xyz.model_info`) and `GateFactor` (`dataset.survey.gex.gex_dict[f"Channel{n}"]`) and apply them to the physical form it materializes transiently (`dbdt × scalefactor × GateFactor`). This is new input plumbing, not just a deletion. A user types the same floor number here as for SimPEG. The estimated-floor path is unaffected (floor and data both raw, factors cancel in the ratio). **`GateFactor` and `scalefactor` default to `1.0` when absent** — a `SyntheticTEMXYZSystem`-style dataset carries no GEX and is normalized as if `GateFactor = 1`, so the noise step must tolerate a missing GEX / missing field rather than fail.
- **Amplitude basis (decided):** ymerflow-processing holds data between steps in **exactly the on-disk / SimPEG format** — raw `dbdt_chNgt` per unit transmitter moment, with `scalefactor` (model_info) and `GateFactor` (GEX) kept as metadata, **never applied to the arrays**. Steps read and write the raw form, so what a step hands the next step is byte-for-byte the disk convention and nothing re-derives a basis. The physical form (`dbdt × scalefactor × GateFactor`, then floor ÷ dipole_moment) is materialized **only transiently inside the noise-floor step**, used for the comparison, and discarded — the step persists a culling mask / relative-fraction result in the raw-format arrays, not scaled values. This keeps processing's floor numerically identical to SimPEG's `uncert_array` without ever leaving the canonical format. Pin it with a cross-check test against SimPEG's `uncert_array` on a shared dataset.
- **Pipelines:** remove `declare_normalization` from `manual-processing.yaml` and the commented `convert_normalization` from `standard-processing.yaml`; rewrite the comments that describe the convention-travels-with-the-dataset model.
- **Tests:** delete/rewrite `test_import_normalization.py` (aem-processes) and ymerflow-processing's declare/convert tests. Add (a) an import test that output matches the SimPEG format and passes `base.py:309`'s validation, and (b) a ymerflow-processing test that its noise floor equals SimPEG's `uncert_array` for the same inputs.

## Change 4 — Document the convention in the relevant places

The convention must be written down where each audience meets it, with **one canonical source and references to it** rather than four drifting copies.

- **Canonical spec:** `deps/simpeg/simpeg/electromagnetics/utils/static_instrument/data_format.md` is already the authority and stays so. Extend it only if a gap is found (e.g. make the "`GateFactor` is the calibration amplitude factor, applied at read, not in the arrays" and "data are per unit transmitter moment" statements explicit and prominent, since the whole stack hinges on them). Every other doc links here instead of restating the physics.
- **`deps/aem-processes/README.md`** (and any import section): document that both importers produce the SimPEG convention; that the XYZ/GEX importer takes the boolean `tx_normalized` (default true, the SkyTEM delivery state) and how it maps to the tx division; and that the msgpack importer assumes the convention and converts nothing. State plainly that `scalefactor` and `GateFactor` are metadata applied at read and must never be baked into the arrays.
- **`deps/ymerflow-processing/docs/reference/filters.md`** and **`docs/reference/llms.txt`**: remove `declare_normalization` and `convert_normalization`; state that the library holds data in the SimPEG convention between steps and that the noise-floor step alone materializes physical units transiently. Update `filters/normalize.py`'s own module docstring the same way (it currently teaches the three-convention model at length).
- **`deps/ymerflow-processing/README.md`** and the `building_plans/01` filter inventory (filter_08 "where unit confusion concentrates"): note that the convention is now fixed by the SimPEG format upstream, so the declare/convert steps are retired; keep the historical rationale but mark it superseded rather than deleting the reasoning.
- **`Nagelfluh/docs/architecture/pipelines.md`** (and a short note in `docs/architecture/processes.md` or `storage.md`): describe the data-on-disk convention once, for platform readers, linking to the canonical `data_format.md`. This is where someone adding a new importer or process will look first.
- Cross-check that no doc still claims data on disk is "fully rx_tx normalized" or that a `normalization` metadata field exists — that model is being removed.

---

## Risks

| Risk | Mitigation |
|------|------------|
| Baking `scalefactor`/`GateFactor` into the data would make SimPEG double-apply them at read | Change 1 keeps both as metadata; only the (optional) tx-moment division is ever applied at import. |
| A non-per-unit-moment file reaches disk mislabeled, with no convention field to catch it | Import is the single choke point; the `tx_normalized` boolean is the assertion, made where the GEX is present, plus the `mean(-dbdt × scalefactor × GateFactor) < 1e-3` plausibility assertion. |
| ymerflow-processing and SimPEG disagree on amplitude basis → processing culls against a different floor than inversion weights | Resolved: internal format = disk format (raw, per-unit-moment); physical units materialized only transiently inside the noise step. Cross-check test against SimPEG `uncert_array`. |
| The noise step forgets to discard its transient scaling and persists physical values into the raw arrays | The step writes only a mask / relative fraction back; a round-trip test asserts the arrays are unchanged in magnitude outside the mask. |
| A user-supplied `noise_level_1ms` (physical V/Am²) is divided by raw `dbdt` without `scalefactor`/`GateFactor` → ~10¹² silent error (neither is read in the library today) | Decided Option A: Change 3 wires both into the transient physical form; cross-check test against SimPEG `uncert_array` catches a mismatch. |
| emerald ever pointed at these files on its default convention | Off the live path today; documented that emerald assumes the opposite tx-moment state. |
| Stale `model_info["normalization"]` in old containers | Changes 1 & 2 strip it. |

## Open decisions for discussion

1. ~~**`input_normalization` enum names / default**~~ — **decided:** a boolean parameter, `tx_normalized` (default true), the SkyTEM delivery state. See Change 1. (A second boolean, `rx_normalized`, which set `GateFactor = 1`, was removed on 2026-10-21: it discarded the calibration.)
2. ~~**Amplitude basis in ymerflow-processing**~~ — **decided:** internal format = on-disk format (raw, per-unit-moment); the noise-floor step transforms to physical units transiently and discards them. See Change 3.
3. ~~Whether to add the plausibility assertion to import~~ — **decided: yes, add `mean(-dbdt × scalefactor × GateFactor) < 1e-3` (mirroring `base.py:309`) as a hard error that blocks the import.** See Change 1.
4. ~~Single-moment (`base`) path and `GateFactor`~~ — **resolved:** the `Dual moment`/`Single moment` systems were removed long ago. Measured data (the target) is read by `MeasuredTEMXYZSystem`, which applies `GateFactor` for 1…N moments; `SyntheticTEMXYZSystem` has no GEX and behaves as `GateFactor = 1`. So the calibration applies to every measured import, and the noise step defaults `GateFactor`/`scalefactor` to `1.0` when no GEX is present. See Changes 1 and 3.
5. ~~**Units of a user-supplied `noise_level_1ms`**~~ — **decided: Option A, physical units (V/Am²), identical to SimPEG.** The noise step reads `scalefactor` + `GateFactor` and applies them to the transient physical form. See Change 3.
