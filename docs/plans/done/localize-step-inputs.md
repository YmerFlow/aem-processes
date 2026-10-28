# Step Inputs in Storage: Fetch Them Before the Run

**Status:** Implemented.
**Repos touched:** `aem-processes` (`aem_processes/yf_processing_process.py`, its tests, the README).
**Context:** `yf_process_tem` and `yf_qc_tem` passed every step parameter to the ymerflow-processing runner as given. Only the input dataset and the plot editor's `diff` list were downloaded first. The library opens only local paths, so a step parameter holding a storage URL failed: `compute_misfit` could never read an inversion's synthetic, and `apply_manual_edits` written as a step with a `diff` URL failed the same way.

## Which parameters are inputs

No schema field marks a step parameter as an input; swaggerspect's forms carry no `format: uri` or `x-format: dataset` for them. The library's own annotations do, though, because it types its external inputs: a parameter that reads data accepts a loaded object as well as a string, and the string says where to read it from.

| step | parameter | annotation |
|---|---|---|
| `compute_misfit`, `cull_by_misfit` | `result` | `str \| XYZ \| Dataset` |
| `apply_manual_edits` | `diff` | `str \| XYZ` |
| `apply_terrain_model` | `terrain` | `str \| rasterio.DatasetReader` |
| `cull_near_infrastructure` | `infrastructure` | `str \| GeoDataFrame` |
| `cull_by_misfit` | `within` | `str \| GeoDataFrame` |

Every other string parameter is a value: `export_vector`'s `path` (where to write) is plain `str`, and `apply_nominal_current`'s `columns` is `str | dict`, whose other member is built in.

**Rule:** a parameter is a data input when its annotation is a union of `str` with a class that is neither built in nor a path type. A string containing `://` given to such a parameter is downloaded. A blanket "any string containing `://`" over the whole step list was rejected because it would read an output location as an input.

## Changes

1. `data_parameters(step)` applies the rule to a registered step function.
2. `fetch_step_inputs(pipeline, registered, storage_kwargs, held)` downloads every URL given to a data parameter, once, before the runner is built, into an `ExitStack` that lives for the whole run and deletes the copies when it closes. It returns the registry with those steps wrapped to receive the local copies.
3. The pipeline itself keeps the URLs. The runner records the pipeline's parameters, so the report and the dataset's processing history name the storage URL, not a temporary file, as `_replay_edits` already does for the editor's diffs.
4. The pipeline is validated against the registry before anything is fetched, so an unknown step still fails at once.

## Tests

- `compute_misfit` with its `result` in `memory://` storage, run in a fresh interpreter: fails on master with `FileNotFoundError: ... 'memory://inversion/root.xyz'`, works with the change.
- `apply_manual_edits` as a step with its `diff` in `memory://` storage: the edit is applied, and the report and processing history keep the URL.
- `data_parameters` classifies the six inputs above and leaves `export_vector.path` and `apply_nominal_current.columns` alone.
- An `export_vector` path in storage triggers no download.

## Limits

- One file per URL. A vector input given as a shapefile needs its sidecar files too; a GeoPackage or GeoJSON works.
- `compute_misfit` reads its `result` with the XYZ text reader, so the URL must be the synthetic's `root.xyz`, not its `root.msgpack`.
