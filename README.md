# aem-processes

AEM (Airborne Electromagnetic) process type implementations for YmerFlow: import, processing,
inversion, forward modelling, gridding, and the `compound_filter` (XYZ / libaarhusxyz InUse-diff)
process. Registered via the `ymerflow.process_types` setuptools entry-point group.

Installed via `pip install "aem-processes[all] @ git+https://github.com/YmerFlow/aem-processes.git"`.
The `[all]` extra pulls the heavy inversion deps (emerald-processing-em, SimPEG@simpleem3,
emerald-monitor). `pyinterp` compiles against Boost ≥ 1.79 — build with a C++ toolchain and
`CC=clang` to avoid a GCC 14 ICE.
