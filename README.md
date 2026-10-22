# aem-processes

AEM (Airborne Electromagnetic) process type implementations for YmerFlow: import, processing,
inversion, forward modeling, gridding, and Apply Manual Edits (`apply_manual_edits`: the in-use edits
saved from a plot, applied to a dataset). Registered via the `ymerflow.process_types` setuptools entry-point group.

Installed via `pip install "aem-processes[all] @ git+https://github.com/YmerFlow/aem-processes.git"`.
The `[all]` extra pulls the heavy inversion deps (emerald-processing-em, SimPEG@simpleem3,
emerald-monitor). `pyinterp` compiles against Boost ≥ 1.79 — build with a C++ toolchain and
`CC=clang` to avoid a GCC 14 ICE.

`yf_process_tem` and `yf_qc_tem` run [ymerflow-processing](https://github.com/YmerFlow/ymerflow-processing) pipelines: processing (dataset in, processed dataset out) and acquisition QC (the dataset with a verdict column per check). Both write the library's run report as `report.json` beside the output dataset, listed in its `info.json` as `application/vnd.ymerflow.run-report+json`. The library is in the `[all]` extra.

## Amplitude convention on import

Every importer produces exactly the **SimPEG `static_instrument` convention**: `dbdt_chNgt` per unit transmitter moment (tx divided out of the arrays), with the receiver area already in the delivered data. The unit scaling `scalefactor` (`model_info`) and the GEX calibration are applied **at read and never baked into the arrays**: `GateFactor` is the calibration amplitude factor, which SimPEG multiplies in, and `GateTimeShift` the calibration time shift, which libaarhusxyz adds to the gate times. An importer never changes either. There is no `normalization` metadata field — the data simply *is* this format. The canonical spec is `simpeg/.../static_instrument/data_format.md`.

- **XYZ/GEX and GDB importers** take one boolean, `tx_normalized` (default `true`, the SkyTEM / Aarhus Workbench delivery state): the dB/dt is already divided by the transmitter moment. If `false`, the importer divides each channel by its nominal `ApproxDipoleMoment` (a channel without one is a hard error). After reconciling, the importer asserts `mean(-dbdt × scalefactor × GateFactor) < 1e-3` per channel (mirroring SimPEG's own validation) as a hard error, since there is no convention field left to cross-check against. The XYZ/GEX importer's former `rx_normalized` option, which set `GateFactor = 1`, is refused with an error, because it discarded the calibration.
- **msgpack importer** assumes the container is already in this convention and converts nothing; it only strips any stale `normalization` field an older container carries.

Tests: `python -m pytest tests` (needs ymerflow-processing installed).
