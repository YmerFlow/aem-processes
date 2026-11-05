# Plan: compare two inverted TEM resistivity models, above DOI

## Goal

A process type, `compare_tem_models`, that takes two inverted resistivity models (A and B) and
produces a single **difference model** — a normal resistivity-model dataset carrying per-sounding,
per-layer Δlog₁₀ρ, so it plots as a curtain/grid like any inversion output. Its agreement metrics
(log₁₀ρ RMSE, bias, scatter, and correlation) are carried in `stats.json`-format files, reported three
ways as **sibling files in that same format**: **overall** (`stats.json`), **above the shallow DOI**
(`stats.above_doi_upper.json`), and **above the deep DOI** (`stats.above_doi_lower.json`), each also
written per flight line under `parts/`.

The "above DOI" comparison is the point of the feature: below the depth of investigation the two
models are not constrained by the data, so differences there are not informative. This capability
exists today only as the standalone script `deps/simpeg-optimization-eval/compare_truth.py`
(`compare_truth.py:63-83`, `mask_doi = depths < doi[:,None]`); it is not a registered process type
and cannot be run from the app. This plan promotes the same logic to a first-class process type.

## Background: current state

- **DOI is genuinely computed by the inversion** on the `ymerflow-master` branch of SimPEG
  (`feat/124-doi-christiansen-auken`, merged). Every inverted model gets per-sounding flightline
  columns `doi_upper` and `doi_lower` (metres below surface, positive down, NaN where a sounding was
  not inverted), from the Christiansen & Auken (2012) cumulative-sensitivity method
  (`simpeg/electromagnetics/utils/static_instrument/base.py:1202-1272`,
  `simpeg/electromagnetics/utils/doi_utils.py:212`). `doi_upper` is the shallow, conservative bound
  (Workbench `DOIAbsHigh`, threshold 1.5); `doi_lower` is the deeper bound (`DOIAbsLow`, 0.75).
  They are written only when `doi__enable` is true (the default); on failure the model still returns,
  without the columns.
- **No model-comparison process type exists.** The only registered `compare` code
  (`deps/ymerflow-processing/ymerflow_processing/compare/`) diffs *processing datasets*
  (data/uncertainty/mask arrays), not resistivity models, and runs under a different entry-point group
  (`ymerflow.processing.compare`), not `ymerflow.process_types`.
- **Models are written by `aem_processes.dataset_utils.write_dataset(xyz, gex, ...)`**
  (`dataset_utils.py:11`): `root.msgpack`, `root.xyz`, `root.gex`, `root.geojson`, `stats.json`,
  VTK/GLB (when the dataset has `layer_data`), one part per flight line, and an `info.json` listing
  the files by MIME type. It already supports an `extra_files={mime: (filename, text)}` argument
  (`dataset_utils.py:88-93`) for attaching a report beside the dataset.
- **Stats are computed by `aem_processes.stats`** (`stats.py`): `compute_column_stats(arr)` returns
  `count/min/max/mean/rms/geometric_mean/std/p5..p95/skewness/kurtosis` over the finite values,
  **ignoring NaN** (`stats.py:42-69`); `compute_xyz_stats(xyz)` builds the model `stats.json` —
  per-flightline-column stats plus, per `layer_data` channel, an `all` block and a per-layer
  breakdown (`layers.<stat>` lists) (`stats.py:80-145`).
- **The model to copy for structure is `gridding_process.Gridding`** (`gridding_process.py:420`):
  `schema()` with `x-format: dataset` URI inputs, `run(cls, storage_context=None, **kwargs)`,
  loading via `libaarhusxyz.export.msgpack.load`, `localize_urls` for inputs, write + `info.json`.

## Design decisions (settled with the operator)

1. **DOI mask = shallower of the two models, per sounding.** For each bound and each sounding the
   cutoff is `min(DOI_A, DOI_B)` — a layer is included only where *both* models are reliable. This is
   the defensible like-for-like choice; it never credits agreement at a depth one model cannot see.
2. **Report all three variants, not one.** `overall` (no mask), `above_doi_upper` (shallow,
   conservative), `above_doi_lower` (deep). The user sees the sensitivity of the metrics to the DOI
   choice rather than being handed one number.
3. **Output = metrics *and* a difference model.** The Δlog₁₀ρ model (for visual comparison) and the
   scalar report (for a number).

## Design

### The difference model carries the comparison as a model channel

Δ is defined per sounding, per layer, as

```
delta_log_resistivity = log10(rho_A) - log10(rho_B_on_A_mesh) = log10(rho_A / rho_B)
```

It rides on **model A's native layer geometry** (`dep_top`/`dep_bot`) and A's `flightlines`, so it is
a valid resistivity model: it writes through the existing `write_dataset` unchanged, plots as a
curtain/grid, and grids with `grid_tem` like any model. Model B is step-sampled onto A's layers
before differencing (see Algorithm). The stored model carries **only `delta_log_resistivity`** as its
data channel — not copies of A and B. The two source arrays (`log₁₀ρ_A`, `log₁₀ρ_B`-on-A) are needed
only transiently, to compute the correlation (below); they are never written to any file, so the
model and its `.xyz` text form stay small and readable.

The difference model's flightlines also carry the **effective (min-of-two) DOIs** as `doi_upper`
and `doi_lower` (`min(upper_A, upper_B)`, `min(lower_A, lower_B)` — the "shallower of the two" rule),
so that the DOI-masked stats variants key off a single pair of columns that `split_by_line` carries
to every part automatically.

### Reuse: the comparison IS more `stats.json` files, same format

Because the difference is a model, `write_dataset` writes its `stats.json` via `compute_xyz_stats`
with **no change**. Everything the operator asked for is in that format:

- **overall log₁₀ρ RMSE** = `layer_data.delta_log_resistivity.all.rms`
- **per-depth (per-layer) RMSE** = `layer_data.delta_log_resistivity.layers.rms`
- **bias** = `.all.mean`, **scatter** = `.all.std`, **outliers** = the percentiles.

(`rms` is already a field of `compute_column_stats`, `stats.py:59`.)

The three DOI views are **the same `compute_xyz_stats` format, as sibling files** — not a bespoke
schema. For each DOI bound, the layer_data is NaN-ed below that sounding's DOI and `compute_xyz_stats`
is called again (it already skips NaN, `stats.py:46-47`), so above-DOI layers get real stats and deep
layers collapse to `{"count": 0}`:

```
stats.json                    # overall    (unmasked compute_xyz_stats — written today, unchanged)
stats.above_doi_upper.json    # above the shallow, conservative DOI (Workbench DOIAbsHigh)
stats.above_doi_lower.json    # above the deep DOI (DOIAbsLow)
```

and, per flight line, `parts/{line}.stats.json`, `parts/{line}.above_doi_upper.stats.json`,
`parts/{line}.above_doi_lower.stats.json`. The frontend's existing `stats.json` viewer renders all of
them with no new frontend code. In `info.json`, the overall stays `application/vnd.ymerflow.stats+json`
(backward-compatible) and the variants are listed with a parameter,
`application/vnd.ymerflow.stats+json;mask=above_doi_upper` / `;mask=above_doi_lower`.

### Correlation: an additive field inside the existing stat blocks

Correlation between A and B — `corrcoef(log₁₀ρ_A, log₁₀ρ_B)` — is the one metric that is not a
per-column statistic (it needs two arrays). The operator confirmed additive fields are allowed as long
as they do not collide with existing keys, so it is **embedded in the delta channel's blocks**:

```jsonc
"layer_data": {
  "delta_log_resistivity": {
    "all":    { "count": ..., "rms": 0.18, "mean": 0.04, "std": 0.17, "p5": ..., ...,
                "correlation": 0.973 },                          // ← added scalar
    "layers": { "count": [...], "rms": [...], "mean": [...], ...,
                "correlation": [0.99, 0.98, 0.95, ...] }         // ← added per-layer list
  }
}
```

It is computed in `run()` from the in-memory `log₁₀ρ_A` / `log₁₀ρ_B` arrays and injected — **not** read
back from stored channels (that was the only reason to store A and B, and it is not worth the bloat).
`compute_xyz_stats` gains an optional `correlate=(array_a, array_b)` argument: when given, it computes
`corrcoef` over the finite pairs (respecting the same NaN mask as the variant) and merges the result
into the `all` and `layers` blocks of `delta_log_resistivity`. Absent for every existing caller, so
their output is byte-identical. Because the arrays are passed through `write_dataset`'s split loop and
sliced per line, each per-part file gets its own correlation too.

### Form (`schema()`)

- `input_model_a` — `x-format: dataset`, "Model A (reference)". Its geometry defines the diff mesh and
  the report's sounding order.
- `input_model_b` — `x-format: dataset`, "Model B".
- `quantity` — the `layer_data` channel to compare, default `resistivity`. (Both models must carry it;
  comparison is always done in log₁₀ space, appropriate for resistivity.)
- `position_tolerance` — metres; soundings within this distance are treated as the same location for
  A↔B alignment. Default `1.0` (matches `compare_truth.py:56`).
- `required: [input_model_a, input_model_b]`.

### Run (`run()`)

1. **Load both models** with `libaarhusxyz.export.msgpack.load(path, True)` (via `localize_urls` for
   both inputs, as `gridding_process` does), `normalize(naming_standard="alc")`. Assert each has
   `layer_data` with the chosen `quantity`; raise a clear error otherwise (not a model).
2. **Align B to A by position.** Nearest-neighbour on `UTMX`/`UTMY`; if the sets are the same length
   and coincide within `position_tolerance`, the mapping is the identity (the common case — two
   inversions of one survey). Count and report soundings in A with no B within tolerance; their Δ is
   NaN (skipped by the stats, surfaced as `n_unmatched`).
3. **Step-sample B onto A's layers.** Piecewise-constant in depth (the `step_sample` logic of
   `compare_truth.py:38-42`): for each A-layer take B's value at that layer's depth. Yields
   `log10(ρ_B)` on A's `(n_sounding, n_layer_A)` mesh.
4. **Build Δ** `= log10(ρ_A) − log10(ρ_B_on_A)`, NaN where either side is non-finite or ρ ≤ 0.
5. **Build the two DOI masks**, per sounding, per layer, using `min(DOI_A, DOI_B)` for each bound and
   the libaarhusxyz convention that a layer is *above* the DOI when `dep_bot <= DOI` (mirrors
   `calculate_doi_layer`, `normalizer.py:276-287`, which marks `dep_bot > doi_*` as below). If either
   model lacks `doi_upper`/`doi_lower` (inversion ran with `doi__enable=False`, or an older model),
   set `doi_available=false`, report `above_doi_*` as `null`, and log a warning — do **not** silently
   fall back to the 300/500 m libaarhusxyz defaults, which would be a fabricated DOI.
6. **Assemble the difference XYZ** on A's geometry: `flightlines` = A's, plus the effective DOIs
   `doi_upper = min(upper_A, upper_B)` and `doi_lower = min(lower_A, lower_B)`; `layer_data` =
   `{"delta_log_resistivity": Δ, dep_top, dep_bot}` — **delta only**, no A/B copies. Carry A's
   `model_info` (CRS) so it grids and exports.
7. **Write the difference model** with
   `write_dataset(diff_xyz, gex_a, "model-comparison", ..., stats_variants=[{"name": "above_doi_upper",
   "doi_column": "doi_upper"}, {"name": "above_doi_lower", "doi_column": "doi_lower"}],
   correlate=("delta_log_resistivity", logA, logB))`. This reuses the whole write path:
   msgpack/xyz/gex/geojson, the standard `stats.json`, VTK/GLB, per-line parts, and `info.json`; and
   additionally emits the two DOI-masked stats files per root and per part, each carrying the injected
   correlation. (`gex_a` is model A's GEX, loaded alongside it.) `logA`/`logB` are the transient
   `(n_sounding, n_layer)` arrays; `write_dataset` slices them by line in the split loop and never
   stores them.
8. **Return** `{"status": "success", "outputs": {"difference": <dataset msgpack url>}}`, and print a
   one-line summary per variant to the log (RMSE + correlation), as `compare_truth.py` does.

### Where the code lives

`aem-processes`, a new module `aem_processes/compare_process.py` with class `CompareModels`,
registered in `setup.py` under `ymerflow.process_types` as
`compare_tem_models=aem_processes.compare_process:CompareModels`. This reuses `dataset_utils`,
`utils.localize_urls` and `stats` directly; it needs no new environment. The step-sampling /
alignment helpers (`step_sample`, `align`, `resample`) are small and are lifted from
`compare_truth.py` into the module (or a shared `_resample.py`) rather than depending on the eval
repo, which is not installed in environments.

### Additions to `stats.py` and `dataset_utils.py` (the only new infra)

- **`stats.py`: `compute_xyz_stats(xyz, mask=None, correlate=None)`** — two optional, backward-compatible
  arguments. `mask` is a `(n_sounding, n_layer)` boolean (or a `doi_column`+geometry the caller resolves
  to one); masked-out `layer_data` entries are NaN-ed before the existing per-column stats run, giving
  an above-DOI variant in the identical format. `correlate=(channel, array_a, array_b)` computes
  `corrcoef` over the finite, in-mask pairs — overall and per layer — and merges `correlation` into that
  channel's `all` and `layers` blocks. Both default off; every existing caller's output is unchanged.
- **`dataset_utils.py`: `write_dataset(..., stats_variants=None, correlate=None)`** — `stats_variants`
  is a list of `{"name", "doi_column"}`; for the root and inside the per-line split loop, it derives the
  above-DOI mask from that part's `flightlines[doi_column]` vs `dep_bot` (above when `dep_bot <= DOI`),
  calls `compute_xyz_stats(..., mask=...)`, and writes `stats.{name}.json` /
  `parts/{line}.{name}.stats.json`, listing them in `info.json` under
  `application/vnd.ymerflow.stats+json;mask={name}`. `correlate=(channel, array_a, array_b)` is threaded
  through to `compute_xyz_stats`, the arrays sliced to each part's soundings so per-part correlation is
  over that line only. The arrays are transient compute inputs — never written to msgpack/xyz/parts.

## Tests

`tests/test_compare_models.py`:

- **Identity.** A model compared with itself: Δ ≡ 0, `stats.json`'s `delta_log_resistivity.all.rms`
  is 0, `all.correlation` is 1, `n_unmatched` 0.
- **Known offset.** B = A scaled by a constant factor `k`: Δ ≡ `log10(k)` everywhere, `all.rms` =
  `|log10 k|`, `all.correlation` 1. Confirms sign and definition (`log10(ρ_A/ρ_B)`).
- **DOI masking as sibling files.** Construct A and B that agree above a per-sounding DOI and disagree
  below: `above_doi_lower`/`above_doi_upper` `.all.rms` ≪ overall `.all.rms`; the deep layers in the
  masked files collapse to `{"count": 0}`; the shallower-of-the-two rule is exercised by giving A and B
  different DOIs and checking the effective `doi_upper`/`doi_lower` on the diff model are the mins.
- **Delta-only model.** The written `root.msgpack`/`root.xyz` contain `delta_log_resistivity` and the
  geometry but **no** `log_resistivity_a`/`_b` channel — A and B are not persisted.
- **Correlation is additive and per-part.** `all.correlation` and `layers.correlation` appear inside
  the existing blocks without displacing any `compute_column_stats` key; a per-line
  `parts/{line}.stats.json` carries a correlation computed over that line's soundings only.
- **Missing DOI.** A model without `doi_upper`/`doi_lower`: the `above_doi_*` files are omitted (or
  written empty) with a logged warning, `stats.json` (overall) still written — and no 300/500 fallback
  appears.
- **Mismatched meshes.** A (39 layers) vs B (40 layers): B step-sampled onto A's mesh, diff model has
  A's layer count, stats finite.
- **Backward-compatible stats.** `compute_xyz_stats(xyz)` with neither new argument returns byte-identical
  output to today's; an existing process (e.g. `inversion_process`) is unaffected.

## Not in this change

- **No frontend work.** The difference model plots with the existing resistivity-curtain / grid
  widgets, and all stats files render in the existing `stats.json` viewer. A viewer that specifically
  surfaces the `;mask=` variants side by side (or a metrics panel) is a separate frontend plan if wanted.
- **No N-way comparison.** Exactly two models (A vs B). A reference-vs-many sweep is a later extension.
- **No new DOI computation.** This consumes the `doi_upper`/`doi_lower` the inversion already writes;
  it does not compute or re-derive DOI.
