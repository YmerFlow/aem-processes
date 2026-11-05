"""invert_tem fetches every input before the run.

simpeg opens only local paths. ``input_data`` was always downloaded first; the
earlier models a warm start reads (``startmodel__dataset``, in simpeg's plan
"Warm start for the spatially constrained inversion") are inputs too, given in
the system's form as a list of dataset URLs. Every URL in the parameters is
localized, as the processing process does, so any other dataset a system
argument names is fetched the same way.

No inversion is run: a stand-in system, served under an entry point name the way
simpeg's systems are, records what it is given. The form schema, its validation
and the system loading are the real ones.
"""

import os
import typing
from types import SimpleNamespace

import fsspec
import numpy as np
import pandas as pd
import pytest
import swaggerspect
import libaarhusxyz
from libaarhusxyz.export import msgpack as xyz_msgpack

from aem_processes import inversion_process, utils
from aem_processes.inversion_process import Inversion

SEEN = {}


class StandIn:
    """A system that records its start models in place of inverting."""

    startmodel__res: float = 100.
    "Halfspace resistivity [ohm.m]."
    startmodel__dataset: typing.Annotated[list, {"json_schema": {"items": {
        "type": "string", "format": "uri", "x-format": "dataset"}}}] = None
    "Earlier inversion models to start from."
    reference: typing.Annotated[str, {"json_schema": {
        "format": "uri", "x-format": "dataset"}}] = None
    "A model to compare against, standing in for any other dataset argument."

    def __init__(self, xyz):
        self.xyz = xyz

    @classmethod
    def load_gex(cls, gex):
        return cls

    def make_directives(self):
        return []

    def invert(self):
        SEEN["res"] = self.startmodel__res
        SEEN["dataset"] = self.startmodel__dataset
        SEEN["exists"] = [os.path.exists(path) for path in self.startmodel__dataset or []]
        SEEN["final_beta"] = [xyz_msgpack.load(path).model_info["final_beta"]
                              for path in self.startmodel__dataset or []]
        SEEN["reference"] = self.reference
        SEEN["reference_exists"] = self.reference is not None and os.path.exists(self.reference)


@pytest.fixture(autouse=True)
def stand_in(monkeypatch):
    """Serve StandIn as the only system, as the entry points serve simpeg's."""
    def apis(group):
        if group != "simpeg.static_instrument":
            return {}
        docs = swaggerspect.get_apis_dict({"Stand-in": StandIn}, local_names=True)
        return dict(docs, title=group, description=group)

    monkeypatch.setattr(swaggerspect, "get_apis_entrypoints", apis)
    monkeypatch.setattr(utils, "get_entry_points",
                        lambda group: {"Stand-in": SimpleNamespace(load=lambda: StandIn)})
    monkeypatch.setattr(inversion_process, "emerald_monitor", None)
    SEEN.clear()


def put_model(url, final_beta=None):
    """Write a small layered model to storage as the platform writes a dataset's root.msgpack."""
    n = 4
    xyz = libaarhusxyz.XYZ()
    xyz.flightlines = pd.DataFrame({"x": 500000 + 25.0 * np.arange(n), "y": np.full(n, 3800000.0),
                                    "line_no": np.full(n, 10), "alt": np.full(n, 30.0),
                                    "topo": np.full(n, 1800.0)})
    xyz.layer_data["resistivity"] = pd.DataFrame(np.full((n, 3), 50.0))
    xyz.layer_data["dep_top"] = pd.DataFrame(np.tile([0.0, 10.0, 40.0], (n, 1)))
    xyz.layer_data["dep_bot"] = pd.DataFrame(np.tile([10.0, 40.0, np.inf], (n, 1)))
    xyz.model_info["projection"] = 32613
    if final_beta is not None:
        xyz.model_info["final_beta"] = final_beta
    with fsspec.open(url, "wb") as f:
        xyz_msgpack.dump(xyz, f)
    return url


def run(tmp_path, startmodel, **args):
    context = {"process_id": "process", "project_id": "test", "version": "0",
               "storage_base": f"file://{tmp_path}/store", "storage_kwargs": {}}
    data = put_model(f"memory://{tmp_path.name}/data/root.msgpack")
    return Inversion.run(storage_context=context, input_data=data,
                         system={"Stand-in": {"startmodel": startmodel, **args}})


def test_start_models_in_storage_reach_the_system_as_local_files(tmp_path):
    models = [put_model(f"memory://{tmp_path.name}/lci_{i}/root.msgpack", final_beta=beta)
              for i, beta in enumerate([10.0, 30.0])]
    result = run(tmp_path, {"res": 40, "dataset": models})
    assert result["status"] == "success"
    assert SEEN["res"] == 40
    assert all("://" not in path and path.endswith(".msgpack") for path in SEEN["dataset"])
    assert SEEN["exists"] == [True, True]                   # there while the system runs
    assert SEEN["final_beta"] == [10.0, 30.0]               # in the order given
    assert not any(os.path.exists(path) for path in SEEN["dataset"])   # removed after


def test_a_job_without_start_models_is_unchanged(tmp_path):
    run(tmp_path, {"res": 40})
    assert SEEN["dataset"] is None and SEEN["res"] == 40


def test_any_dataset_a_system_argument_names_is_fetched(tmp_path):
    """No argument is picked out by name: every URL in the system form is localized."""
    reference = put_model(f"memory://{tmp_path.name}/reference/root.msgpack")
    run(tmp_path, {"res": 40}, reference=reference)
    assert "://" not in SEEN["reference"] and SEEN["reference_exists"]
    assert not os.path.exists(SEEN["reference"])


def test_the_form_offers_start_models_as_datasets():
    """The field comes from the system class; invert_tem's own schema needs nothing for it."""
    system = Inversion.system_schema()["anyOf"][0]["properties"]["Stand-in"]
    dataset = system["properties"]["startmodel"]["properties"]["dataset"]
    assert dataset["type"] == "array" and dataset["items"]["x-format"] == "dataset"
