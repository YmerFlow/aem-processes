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
    from ymerflow_processing.core.pipeline import Pipeline
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


def test_every_parameter_of_every_step_can_be_entered():
    """A field with neither a type nor a choice of types renders as
    'unsupported' in the platform's form."""
    for process in (YFProcessTEM, YFQCTEM):
        for api in process.schema()["properties"]["steps"]["items"]["anyOf"]:
            step, arguments = next(iter(api["properties"].items()))
            for name, field in arguments.get("properties", {}).items():
                assert "type" in field or "anyOf" in field, f"{step}.{name}"


def test_parameters_are_offered_in_signature_order():
    steps = YFProcessTEM.schema()["properties"]["steps"]["items"]["anyOf"]
    cull = next(api for api in steps if "cull_negatives" in api["properties"])
    assert list(cull["properties"]["cull_negatives"]["properties"]) == [
        "start_gate", "spread_earlier", "disable_tail"]


def test_without_the_library_the_form_is_a_bare_list(monkeypatch):
    monkeypatch.setitem(sys.modules, "ymerflow_processing.core.pipeline", None)
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
    from ymerflow_processing.core.dataset import load
    from ymerflow_processing.core.pipeline import Pipeline, ProcessingRunner

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
