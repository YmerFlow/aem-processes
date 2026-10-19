# aem-processes

AEM (Airborne Electromagnetic) process type implementations for YmerFlow: import, processing,
inversion, forward modelling, gridding, and Apply Manual Edits (`apply_manual_edits`: the in-use edits
saved from a plot, applied to a dataset). Registered via the `ymerflow.process_types` setuptools entry-point group.

Installed via `pip install "aem-processes[all] @ git+https://github.com/YmerFlow/aem-processes.git"`.
The `[all]` extra pulls the heavy inversion deps (emerald-processing-em, SimPEG@simpleem3,
emerald-monitor). `pyinterp` compiles against Boost ≥ 1.79 — build with a C++ toolchain and
`CC=clang` to avoid a GCC 14 ICE.

`yf_process_tem` and `yf_qc_tem` run [ymerflow-processing](https://github.com/YmerFlow/ymerflow-processing) pipelines: processing (dataset in, processed dataset out) and acquisition QC (the dataset with a verdict column per check). Both write the library's run report as `report.json` beside the output dataset, listed in its `info.json` as `application/vnd.ymerflow.run-report+json`. The library is in the `[all]` extra.

## Amplitude convention on import

Both importers produce exactly the **SimPEG `static_instrument` convention**:
`dbdt_chNgt` per unit transmitter moment (tx divided out of the arrays), receiver
geometry carried by the GEX `GateFactor` and the unit scaling by `scalefactor`
(`model_info`), both applied **at read and never baked into the arrays**. There
is no `normalization` metadata field — the data simply *is* this format. The
canonical spec is `simpeg/.../static_instrument/data_format.md`.

- **XYZ/GEX importer** takes two booleans describing how the upload was
  normalized, defaulting to the SkyTEM / Aarhus Workbench delivery state:
  - `tx_normalized` (default `true`) — the dB/dt is already divided by the
    transmitter moment. If `false`, the importer divides each channel by its
    nominal `ApproxDipoleMoment` (a channel without one is a hard error).
  - `rx_normalized` (default `false`) — receiver geometry is already in the
    arrays. If `true`, the importer sets that channel's `GateFactor = 1` so the
    read-time multiply is a no-op; the arrays are otherwise untouched.
  After reconciling, the importer asserts
  `mean(-dbdt × scalefactor × GateFactor) < 1e-3` per channel (mirroring SimPEG's
  own validation) as a hard error, since there is no convention field left to
  cross-check against.
- **msgpack importer** assumes the container is already in this convention and
  converts nothing; it only strips any stale `normalization` field an older
  container carries.

Tests: `python -m pytest tests` (needs ymerflow-processing installed).
