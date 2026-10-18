"""The ymerflow-processing process types, end to end against a local store.

A synthetic delivery is imported the way the platform imports one
(``import_skytem``), then processed and QC'd through the process types, so the
input is exactly what a real platform dataset looks like.
"""

import json
import sys

import numpy as np
import pandas as pd
import pytest
from libaarhusxyz import GEX, XYZ

from aem_processes.import_process import LibaarhusXYZImporter
from aem_processes.yf_processing_process import REPORT_MIME, YFProcessTEM, YFQCTEM

N_GATES = 12
PER_LINE = 30


# ── A delivery, as files ─────────────────────────────────────────────────────

def write_delivery(directory, moments):
    """A SkyTEM-shaped XYZ and GEX: two lines, the second flown too high."""
    opens = np.logspace(-5, -2.5, N_GATES)
    closes = opens * 1.25
    general = {"GateTime": np.column_stack([(opens + closes) / 2, opens, closes]),
               "TxLoopArea": 342.0}
    gex = {"General": general}
    for number, (moment, current) in enumerate(moments, start=1):
        general[f"NumberOfTurns{moment}"] = 1.0
        gex[f"Channel{number}"] = {
            "NoGates": float(N_GATES), "TransmitterMoment": moment,
            "TxApproximateCurrent": current, "UniformDataSTD": 0.03,
            "ReceiverPolarizationXYZ": "Z", "RemoveInitialGates": 0.0,
            "GateTimeShift": 0.0, "MeaTimeDelay": 0.0}
    gex_path = directory / "delivery.gex"
    with open(gex_path, "wb") as f:
        GEX(gex).dump(f)

    n = 2 * PER_LINE
    flightlines = pd.DataFrame({
        "Line": np.repeat([100101, 100201], PER_LINE),
        "Flight": np.repeat([1, 2], PER_LINE),
        "UTMX": np.tile(np.arange(PER_LINE) * 25.0, 2) + 500000.0,
        "UTMY": np.repeat([4500000.0, 4501000.0], PER_LINE),
        "Topography": 1900.0,
        "TxAltitude": np.r_[np.full(PER_LINE, 35.0), np.full(PER_LINE, 80.0)],
        "TxPitch": np.linspace(-2, 2, n),
        "TxRoll": np.linspace(1, -1, n),
        "GdSpeed": 85.0,
    })
    times = (opens + closes) / 2
    layer_data = {}
    for number, _ in enumerate(moments, start=1):
        decay = 1e-6 * number * (times / 1e-5) ** -1.5
        layer_data[f"Gate_Ch{number:02d}"] = pd.DataFrame(np.tile(decay, (n, 1)))
        layer_data[f"STD_Ch{number:02d}"] = pd.DataFrame(np.full((n, N_GATES), 0.03))
    xyz_path = directory / "delivery.xyz"
    XYZ({"flightlines": flightlines, "layer_data": layer_data,
         "model_info": {}, "file_meta": {}}).dump(str(xyz_path))
    return xyz_path, gex_path


def storage(tmp_path, process_id):
    return {"process_id": process_id, "project_id": "test", "version": "0",
            "storage_base": f"file://{tmp_path}/store",
            # A local store needs its directories made; object stores do not.
            "storage_kwargs": {"auto_mkdir": True}}


def dataset_dir(url):
    """The local directory of a dataset, from its root.msgpack URL."""
    return url.split("file://", 1)[1].rsplit("/", 1)[0]


def info(url):
    with open(f"{dataset_dir(url)}/info.json") as f:
        return json.load(f)


def report(url):
    with open(info(url)["files"][REPORT_MIME].split("file://", 1)[1]) as f:
        return json.load(f)


@pytest.fixture(params=[(("LM", 9.0),), (("LM", 9.0), ("HM", 110.0))],
                ids=["single-moment", "dual-moment"])
def imported(request, tmp_path):
    """A delivery imported into the store; its root.msgpack URL."""
    xyz_path, gex_path = write_delivery(tmp_path, request.param)
    result = LibaarhusXYZImporter.run(
        storage_context=storage(tmp_path, "import"),
        xyzfile=f"file://{xyz_path}", gexfile=f"file://{gex_path}",
        scalefactor=1.0, projection=32613)
    return result["outputs"]["imported_data"]


def bundled_steps(name):
    from ymerflow_processing.pipeline import Pipeline
    return [{step: params} for step, params in Pipeline.load(name).steps]


# ── The form ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("process, bundled", [(YFProcessTEM, "standard-processing"),
                                              (YFQCTEM, "acquisition-qc")])
def test_the_form_opens_with_the_bundled_pipeline(process, bundled):
    schema = process.schema()
    json.dumps(schema)  # the platform stores it as JSON
    steps = schema["properties"]["steps"]
    assert steps["default"] == bundled_steps(bundled)
    offered = [next(iter(api["properties"])) for api in steps["items"]["anyOf"]]
    in_pipeline = [next(iter(step)) for step in steps["default"]]
    assert offered[:len(in_pipeline)] == in_pipeline


def step_forms(process):
    """(step name, its argument schema) for every step a process type offers."""
    for api in process.schema()["properties"]["steps"]["items"]["anyOf"]:
        yield next(iter(api["properties"].items()))


#: One value of each JSON type, to check a field accepts what it declares.
SAMPLE = {"number": 1.5, "integer": 2, "string": "x", "boolean": True, "array": [], "object": {}}


@pytest.mark.parametrize("process", [
    pytest.param(YFProcessTEM, marks=pytest.mark.xfail(strict=True, reason=(
        "swaggerspect adds the docstring's union type string as a top-level "
        "\"type\": \"object\" beside the anyOf, so a scalar or per-channel field "
        "rejects its own scalar. Fixed in swaggerspect, not worked around here."))),
    YFQCTEM,
])
def test_every_field_accepts_a_value_of_each_type_it_declares(process):
    """A field the platform's form cannot fill, or that rejects what it says it
    takes, cannot be entered at all."""
    import jsonschema

    refused = []
    for step, arguments in step_forms(process):
        for name, field in arguments.get("properties", {}).items():
            declared = [m.get("type") for m in field["anyOf"]] if "anyOf" in field else [field.get("type")]
            for json_type in declared:
                if json_type is None or not jsonschema.Draft7Validator(field).is_valid(SAMPLE[json_type]):
                    refused.append(f"{step}.{name} ({json_type})")
    assert not refused, refused


def test_parameters_are_offered_in_signature_order():
    import inspect
    from ymerflow_processing.pipeline import load_steps

    for process in (YFProcessTEM, YFQCTEM):
        registered = load_steps(process.group)
        for step, arguments in step_forms(process):
            offered = list(arguments.get("properties", {}))
            signature = list(inspect.signature(registered[step]).parameters)
            assert offered == [p for p in signature if p in offered], step


def test_no_step_form_asks_for_the_dataset():
    """The process supplies it; a form field for it would be an object box on every step."""
    for process in (YFProcessTEM, YFQCTEM):
        for step, arguments in step_forms(process):
            assert "dataset" not in arguments.get("properties", {}), step
            assert "dataset" not in arguments.get("required", []), step


def test_without_the_library_the_form_is_a_bare_list(monkeypatch):
    monkeypatch.setitem(sys.modules, "ymerflow_processing.pipeline", None)
    steps = YFProcessTEM.schema()["properties"]["steps"]
    assert steps["type"] == "array" and "default" not in steps


# ── Processing ───────────────────────────────────────────────────────────────

def test_processing_writes_the_dataset_and_its_report(imported, tmp_path):
    result = YFProcessTEM.run(storage_context=storage(tmp_path, "process"),
                              input_data=imported, steps=bundled_steps("synthetic-data"))
    output = result["outputs"]["processed_data"]
    files = info(output)["files"]
    assert {"text/x-aarhusxyz", "text/x-aarhusxyz-gex", REPORT_MIME} <= set(files)
    names = [step["step"] for step in report(output)["steps"]]
    assert names[-3:] == ["estimate_noise_model", "assign_uncertainty", "perturb_from_uncertainty"]


def test_the_process_type_adds_nothing_to_the_library(imported, tmp_path):
    """The same pipeline run directly on the same two files gives the same data."""
    from ymerflow_processing.dataset import load
    from ymerflow_processing.pipeline import Pipeline, ProcessingRunner

    steps = bundled_steps("synthetic-data")
    result = YFProcessTEM.run(storage_context=storage(tmp_path, "process"),
                              input_data=imported, steps=steps)
    through_platform = XYZ(f"{dataset_dir(result['outputs']['processed_data'])}/root.xyz")

    source = dataset_dir(imported)
    direct, _ = ProcessingRunner(Pipeline({"kind": "filters", "steps": steps})).run(
        load(f"{source}/root.xyz", f"{source}/root.gex"))
    direct.survey.xyz.dump(str(tmp_path / "direct.xyz"))
    direct = XYZ(str(tmp_path / "direct.xyz"))

    assert set(through_platform.layer_data) == set(direct.layer_data)
    for key in direct.layer_data:
        np.testing.assert_array_equal(through_platform.layer_data[key].values,
                                      direct.layer_data[key].values, err_msg=key)


def test_the_output_keeps_the_platform_spelling(imported, tmp_path):
    """Reading the text form lowercases every name; the output must not."""
    from libaarhusxyz.export import msgpack as xyz_msgpack

    result = YFQCTEM.run(storage_context=storage(tmp_path, "qc"), input_data=imported,
                         steps=[{"height_above_ground": {"good_max": 45, "marginal_max": 65}}])
    output, _ = xyz_msgpack.load(result["outputs"]["qc_data"].split("file://", 1)[1],
                                 return_gex=True)
    assert "Gate_Ch01" in output.layer_data and "InUse_Ch01" in output.layer_data
    assert {"Line", "TxAltitude", "qs_height"} <= set(output.flightlines.columns)


def test_a_dataset_without_text_forms_is_read_from_its_msgpack(imported, tmp_path):
    path = f"{dataset_dir(imported)}/info.json"
    described = info(imported)
    del described["files"]["text/x-aarhusxyz"], described["files"]["text/x-aarhusxyz-gex"]
    with open(path, "w") as f:
        json.dump(described, f)
    result = YFProcessTEM.run(storage_context=storage(tmp_path, "process"),
                              input_data=imported, steps=bundled_steps("synthetic-data"))
    assert REPORT_MIME in info(result["outputs"]["processed_data"])["files"]


def test_an_unknown_step_fails_before_any_data_is_read(tmp_path):
    with pytest.raises(Exception, match="no_such_step"):
        YFProcessTEM.run(storage_context=storage(tmp_path, "process"),
                         input_data=f"file://{tmp_path}/nowhere/root.msgpack",
                         steps=[{"no_such_step": {}}])


def test_a_qc_step_in_a_processing_pipeline_says_where_it_belongs(tmp_path):
    with pytest.raises(Exception, match="qc"):
        YFProcessTEM.run(storage_context=storage(tmp_path, "process"),
                         input_data=f"file://{tmp_path}/nowhere/root.msgpack",
                         steps=[{"height_above_ground": {"good_max": 45, "marginal_max": 65}}])


# ── QC ───────────────────────────────────────────────────────────────────────

def test_out_of_spec_completes_and_says_so(imported, tmp_path):
    """The second line is flown at 80 m against a 65 m limit: a finding, not a failure."""
    result = YFQCTEM.run(storage_context=storage(tmp_path, "qc"), input_data=imported,
                         steps=[{"height_above_ground": {"good_max": 45, "marginal_max": 65}}])
    output = result["outputs"]["qc_data"]
    assert result["status"] == "success"
    assert report(output)["summary"]["worst_verdict"] == "OOS"
    assert "qs_height" in XYZ(f"{dataset_dir(output)}/root.xyz").flightlines.columns


def test_the_bundled_qc_pipeline_runs(imported, tmp_path):
    result = YFQCTEM.run(storage_context=storage(tmp_path, "qc"), input_data=imported,
                         steps=bundled_steps("acquisition-qc"))
    summary = report(result["outputs"]["qc_data"])["summary"]
    assert summary["worst_verdict"] == "OOS"


def test_a_step_that_raises_fails_the_run(imported, tmp_path):
    """Naming altimeter columns that are not there is a mistake, not a finding."""
    with pytest.raises(Exception):
        YFQCTEM.run(storage_context=storage(tmp_path, "qc"), input_data=imported,
                    steps=[{"altimeter_agreement": {"columns": ["nope", "nada"],
                                                    "good_max": 2.0, "marginal_max": 5.0}}])


# ── Manual edits ─────────────────────────────────────────────────────────────

def write_editor_diff(path, edits, key="InUse_Ch01"):
    """What the plot editor saves for one round: msgpack, a row per sounding
    touched, a column per gate touched labelled by its index, NaN elsewhere.

    ``edits`` is ``{(sounding, gate): 0 or 1}``.
    """
    soundings = sorted({s for s, _ in edits})
    gates = sorted({g for _, g in edits})
    columns = {g: [edits.get((s, g), np.nan) for s in soundings] for g in gates}
    diff = XYZ({"flightlines": pd.DataFrame({"apply_idx": [float(s) for s in soundings]}),
                "layer_data": {key: pd.DataFrame(columns)},
                "model_info": {"diff_dummy": np.nan}, "file_meta": {}})
    with open(path, "wb") as f:
        diff.to_msgpack(f)
    return f"file://{path}"


def in_use_of(url, key="InUse_Ch01"):
    from libaarhusxyz.export import msgpack as xyz_msgpack
    output, _ = xyz_msgpack.load(url.split("file://", 1)[1], return_gex=True)
    return output, output.layer_data[key].values


def test_saved_edits_are_replayed_after_every_step(imported, tmp_path):
    """Saving edits re-runs the process with "diff" set; the edit must survive it.
    A single URL, as a version saved before edits accumulated carries."""
    diff = write_editor_diff(tmp_path / "inuse_diff_processed_data.msgpack", {(2, 3): 0})
    result = YFProcessTEM.run(storage_context=storage(tmp_path, "process"), input_data=imported,
                              steps=[{"cull_negatives": {}}], diff=diff)
    output_url = result["outputs"]["processed_data"]

    output, in_use = in_use_of(output_url)
    assert in_use[2, 3] == 0
    assert in_use.sum() == in_use.size - 1  # that gate and nothing else

    last = report(output_url)["steps"][-1]
    assert last["step"] == "apply_manual_edits"
    assert last["params"]["diff"] == diff  # the edits' URL, not a temporary file
    history = json.loads(output.model_info["processing_history"])
    assert [entry["step"] for entry in history][-2:] == ["cull_negatives", "apply_manual_edits"]


def test_every_round_of_edits_is_replayed_in_order(imported, tmp_path):
    """One entry per save, oldest first: the second round turns one gate the
    first disabled back on, and disables another."""
    first = write_editor_diff(tmp_path / "round1.msgpack", {(2, 3): 0, (2, 5): 0})
    second = write_editor_diff(tmp_path / "round2.msgpack", {(2, 3): 1, (4, 6): 0})
    result = YFProcessTEM.run(storage_context=storage(tmp_path, "process"), input_data=imported,
                              steps=[{"cull_negatives": {}}], diff=[first, second])
    output_url = result["outputs"]["processed_data"]

    output, in_use = in_use_of(output_url)
    assert in_use[2, 3] == 1 and in_use[2, 5] == 0 and in_use[4, 6] == 0
    assert in_use.sum() == in_use.size - 2
    replayed = [s["params"]["diff"] for s in report(output_url)["steps"] if s["step"] == "apply_manual_edits"]
    assert replayed == [first, second]
    history = json.loads(output.model_info["processing_history"])
    assert [entry["params"].get("diff") for entry in history if entry["step"] == "apply_manual_edits"] == [first, second]


def run_compound_filter(imported, tmp_path, key):
    from aem_processes.compound_filter import compound_filter

    first = write_editor_diff(tmp_path / "round1.msgpack", {(2, 3): 0, (2, 5): 0}, key=key)
    second = write_editor_diff(tmp_path / "round2.msgpack", {(2, 3): 1, (4, 6): 0}, key=key)
    context = storage(tmp_path, "filter")
    result = compound_filter.run(storage_context=context, input=imported, diff=[first, second])
    return (f"{context['storage_base']}/processes/filter/0/datasets/"
            f"{result['dataset_id']}/root.msgpack")


def test_compound_filter_applies_every_round_in_order(imported, tmp_path):
    """The other process the plot editor re-runs gets the same list. Its diffs
    are written here in its own (libaarhusxyz) naming: see the next test."""
    url = run_compound_filter(imported, tmp_path, key="dbdt_inuse_ch1gt")
    _, in_use = in_use_of(url, key="dbdt_inuse_ch1gt")
    assert in_use[2, 3] == 1 and in_use[2, 5] == 0 and in_use[4, 6] == 0


@pytest.mark.xfail(strict=True, raises=KeyError, reason=(
    "compound_filter normalizes its input to libaarhusxyz naming before applying "
    "the diff, so the editor's InUse_Ch01 matches nothing; the KeyError is caught "
    "and printed as a warning and the edits are dropped. Predates the list change."))
def test_compound_filter_applies_the_editors_own_diffs(imported, tmp_path):
    url = run_compound_filter(imported, tmp_path, key="InUse_Ch01")
    _, in_use = in_use_of(url)
    assert in_use[2, 3] == 1 and in_use[2, 5] == 0 and in_use[4, 6] == 0


def test_only_processing_offers_edits():
    assert "diff" in YFProcessTEM.schema()["properties"]
    assert "diff" not in YFQCTEM.schema()["properties"]


def test_qc_refuses_edits_rather_than_dropping_them(tmp_path):
    with pytest.raises(ValueError, match="no in-use edits"):
        YFQCTEM.run(storage_context=storage(tmp_path, "qc"),
                    input_data=f"file://{tmp_path}/nowhere/root.msgpack",
                    steps=[{"height_above_ground": {"good_max": 45, "marginal_max": 65}}],
                    diff=f"file://{tmp_path}/nowhere/diff.msgpack")
