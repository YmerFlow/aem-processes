# yf_process_tem Writes Its Save Points

**Status:** Plan. Decisions taken 2026-11-16; waiting for the plan to be committed.
**Repos touched:** `aem-processes` only (`aem_processes/yf_processing_process.py`, `tests/test_yf_processing_process.py`).
**Context:** ymerflow-processing 0.4.2 ([YmerFlow/ymerflow-processing#44](https://github.com/YmerFlow/ymerflow-processing/pull/44)) adds the step `save_intermediate_and_apply_diff(dataset, name, diff=None, sink=None)`. It hands the data, as the steps above it left it, to a runner-supplied `sink(name, dataset)`, then replays its own manual edit. The library's plan (`docs/plans/edit-points.md` in ymerflow-processing) and the platform's editor plan (`docs/plans/pipeline-edit-points.md` in YmerFlow/Ymerflow) leave aem-processes two jobs: supply that sink, so each save point becomes a dataset the plot editor can open, and remove `yf_process_tem`'s end-of-chain `diff` replay. The use that drives it is one processing chain run as one process with two save points, one at full rate after the automatic culls and one on the averaged data before the minimum-gates cull, each editable in the plot editor.

## What is there today

- `ProcessingRunner(pipeline, steps, inventory=True, sink=None)` passes `sink=` to every step whose signature has a `sink` parameter, mid-run, as it reaches that step. `Pipeline.validate` checks save point names (given, unique, not `output` or `processed_data`) before any data is read, and `_PipelineProcess.run` already calls it before fetching anything.
- The step calls `sink(name, dataset)` first and then replays its `diff` on the same `dataset` object. The runner's docstring says the sink must not modify the dataset, which the rest of the run goes on to use.
- `yf_process_tem` builds the runner without a sink, so every save point is skipped (its step report says `"skipped"`) and only its edit replays.
- A save point's `diff` is never fetched. `fetch_step_inputs` downloads only the parameters `data_parameters` picks out, which are unions of `str` and a loaded type (`str | XYZ`). The save point's `diff` is typed `Annotated[pydantic.AnyUrl, ...]`, so `data_parameters` returns nothing for it, and `apply_manual_edits` then calls `open()` on a storage URL and fails. Checked against ymerflow-processing main, where it is the only URL-typed step parameter.
- Edits address soundings by row position. `apply_manual_edits` reads `flightlines.apply_idx` from the diff and requires `0 <= apply_idx < len(dataset)`. The editor builds `apply_idx` from the parts of the dataset it plots, and `write_dataset`'s parts carry it: `XYZ.split_by_line()` sets `apply_idx` to the flightlines index before splitting.
- `write_dataset` changes the XYZ it is given: `split_by_line()` adds an `apply_idx` column to its flightlines. `yf_process_tem` also restores the input's column spelling with `normalize_naming` in place before writing. Both are harmless at the end of a run and wrong in the middle of one.
- The end-of-chain replay (`takes_edits`, the `diff` array in the schema, `_replay_edits`, `EDIT_CHECKPOINT` and the `diffs` loop in `run()`) replays each saved edit after the last step. The library's plan removes it (its Decision 6), because an edit applied after the minimum-gates cull can leave a sounding with too few gates and nothing after it to cull it.

## Decisions

Settled by the step's and the editor's contracts:

1. Each save point is a dataset of the process named exactly its `name`, and is returned in `outputs` under that name. The editor finds a save point's step by `name` equal to the dataset name, so the name cannot carry a prefix. The library's validation already keeps it from colliding with `processed_data`.
2. The sink writes before it returns. The step edits the same dataset object as soon as the sink returns, so a sink that held the dataset and wrote it at the end would save the edited state.
3. Root and per-line parts go through `write_dataset`, so each part carries `apply_idx` and an edit drawn on a part replays on the right rows.
4. The sink never modifies the dataset. It writes a shallow copy: new flightlines and layer frames over the same arrays, numbered by row position, and a copy of `model_info`. The renaming and the `apply_idx` column land on the copy only, and nothing is duplicated in memory. A full copy through `XYZ(xyz)` would double the memory of a full-rate snapshot for the length of the write. The final output is prepared the same way, so one helper serves both.

Decided 2026-11-16, each the recommended option:

5. **Which files a save point gets.** A: everything `processed_data` gets (msgpack, XYZ, GEX, GeoJSON, stats, per-line parts), through `write_dataset` unchanged. B: only what the plot editor reads (msgpack, GeoJSON, stats, parts), skipping the XYZ text, the slow part on a full-rate snapshot. Decided: A. One writer, and a snapshot downloads like any other dataset. Revisit if full-rate save points prove slow to write.
6. **A run whose parameters still carry a process-level `diff`**, as versions saved before this change do. A: fail at once, before anything is fetched, with a message saying the edit now belongs on a save point: put its URL on a `save_intermediate_and_apply_diff` step at the end of the pipeline, which sees the same state the old replay applied it to (one save point per round, in order, for a version with several). B: keep replaying it after the last step for old versions, with a printed deprecation note. C: ignore it. Decided: A. There is one way to replay an edit, as the library's plan decided, and nothing is lost without a word. The cost is that an old version with edits needs them moved before it re-runs. C would drop a person's edits silently and is listed only to be ruled out.
7. **An environment whose ymerflow-processing predates the sink.** A: give the runner a sink only when its signature takes one (the `inspect.signature` test the library itself uses for steps). An older library then runs as today and, having no save point step, rejects a pipeline that uses one when it validates. B: always pass the sink and require ymerflow-processing 0.4.2 or later, so an older environment fails every `yf_process_tem` run with a `TypeError`. Decided: A. It is one line, and `QCRunner`, which takes no sink, needs the same test anyway.
8. **Fetching a save point's `diff`.** A: `data_parameters` also counts a parameter typed as a URL (`pydantic.AnyUrl`, alone, in a union or inside `Annotated`) as data to read, so it is fetched with the other step inputs. B: special-case the save point step's `diff` by name. Decided: A. The library types its external inputs so the wrapper can find them, and a URL type is the plainest case. The library never types an output location as a URL, so nothing is read by mistake.

## Changes (`aem_processes/yf_processing_process.py`)

1. `_as_written(xyz)`: the shallow copy from Decision 4, with the input's column spelling restored (`naming_standard` from `model_info`, default `alc`) as the final output is today.
2. `_save_point_sink(storage_context, outputs)`: returns `sink(name, dataset)`, which writes `_as_written(dataset.survey.xyz)` and `dataset.survey.gex` through `write_dataset` under `name`, prints one line, and adds the dataset's root URL to `outputs[name]`.
3. `run()`: builds the runner with the sink when the runner takes one (Decision 7), collects outputs in the order they are written (save points, then `processed_data`), and writes the final output through `_as_written` too. The run report stays on `processed_data` alone: it is complete only at the end, and it already records every save point's step.
4. `data_parameters`: URL-typed parameters count as data (Decision 8).
5. Removed: `takes_edits`, the schema's `diff` property, `_replay_edits`, `EDIT_CHECKPOINT` and the `diffs` loop. A `diff` in the parameters raises (Decision 6). The module docstring says what the process writes, save points included.

## Tests (`tests/test_yf_processing_process.py`)

- A pipeline with two save points writes three datasets, each with root and parts, returned in `outputs` as `first`, `second` and `processed_data`.
- Each save point holds the state before its own edit: with a `diff` on the first save point, its dataset has the pre-edit in-use mask, while the second save point and `processed_data` show the edit.
- The sink leaves the run's dataset as it was: the same columns (no `apply_idx`), spelling, values and `model_info`, compared before and after the sink is called.
- Round trip: a diff built from one part of a save point (through its `apply_idx`), put on that save point and re-run, turns off exactly those gates in `processed_data`.
- A save point's `diff` in storage (`memory://`) is fetched, and the report and processing history record the URL, not the local copy.
- The schema has no `diff` property, and a run given one fails before fetching anything, with the message from Decision 6.
- With a stand-in runner that takes no sink, as an older library's, a pipeline without save points runs as before.
- The process-level replay tests (`test_saved_edits_are_replayed_after_every_step`, `test_every_round_of_edits_is_replayed_in_order`) are removed. `apply_manual_edits` keeps its own tests.
- The suite needs ymerflow-processing 0.4.2 or later.

## Not in this change

- Save points in `process_tem` (emerald-processing). Its save point step writes into a temporary folder that is dropped, and emerald-processing is on its way out.
- Moving existing versions' process-level edits onto save points, by hand or by a migration. Decision 6 covers only what a run does when it meets one.
- The editor (`docs/plans/pipeline-edit-points.md` in YmerFlow/Ymerflow). Until it ships, `yf_process_tem` outputs cannot be edited from the plot editor at all: the schema no longer has the `diff` today's editor appends to, and the editor does not yet write onto save point steps. Landing both in one environment build and platform deploy closes the gap.
- Carrying a run's cluster and resource settings across a Save, which large one-process pipelines need. A platform change, listed in both other plans.
- Write time of full-rate snapshots (Decision 5, B).
