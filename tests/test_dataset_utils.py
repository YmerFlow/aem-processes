"""write_dataset writes the 3D forms (VTK, GLB) for resistivity models only.

AEM data has layer tables too (gates, their STDs and InUse flags) but no layer
depths, so there is nothing to place in 3D and no warning to print about it.
"""

import json

import numpy as np
import pandas as pd
from libaarhusxyz import GEX, XYZ

from aem_processes.dataset_utils import write_dataset

PER_LINE = 10
N = 2 * PER_LINE
N_GATES = 4
N_LAYERS = 5
THREE_D = {"model/vnd.vtk", "model/gltf-binary"}


def soundings(layer_data):
    """Two flight lines carrying layer_data, normalized as the processes do."""
    flightlines = pd.DataFrame({
        "Line": np.repeat([100101, 100201], PER_LINE),
        "UTMX": np.tile(np.arange(PER_LINE) * 25.0, 2) + 500000.0,
        "UTMY": np.repeat([4500000.0, 4501000.0], PER_LINE),
        "Topography": 1900.0,
    })
    xyz = XYZ({"flightlines": flightlines,
               "layer_data": {key: pd.DataFrame(table) for key, table in layer_data.items()},
               "model_info": {"projection": 32613, "scalefactor": 1.0}, "file_meta": {}})
    xyz.normalize(naming_standard="alc")
    return xyz


def data_shaped():
    return soundings({"Gate_Ch01": np.full((N, N_GATES), 1e-6),
                      "STD_Ch01": np.full((N, N_GATES), 0.03),
                      "InUse_Ch01": np.ones((N, N_GATES), dtype=np.int8)})


def model_shaped():
    thickness = np.full((N, N_LAYERS), 10.0)
    dep_bot = np.cumsum(thickness, axis=1)
    return soundings({"resistivity": np.full((N, N_LAYERS), 30.0),
                      "dep_top": dep_bot - thickness, "dep_bot": dep_bot})


def write(xyz, tmp_path):
    """Write xyz with write_dataset; the files of its root and of each part."""
    dataset_id = write_dataset(xyz, GEX({"General": {}}), "dataset", "process", "0",
                               f"file://{tmp_path}", {"auto_mkdir": True})
    with open(tmp_path / "processes/process/0/datasets" / dataset_id / "info.json") as f:
        info = json.load(f)
    assert len(info["parts"]) == 2
    return [set(info["files"])] + [set(part["files"]) for part in info["parts"].values()]


def test_data_is_written_without_3d_forms_or_a_warning(tmp_path, capsys):
    for files in write(data_shaped(), tmp_path):
        assert not files & THREE_D
    assert not list(tmp_path.rglob("*.vtk")) and not list(tmp_path.rglob("*.glb"))
    out = capsys.readouterr().out
    assert "VTK" not in out and "Warning" not in out


def test_a_resistivity_model_is_written_with_3d_forms(tmp_path, capsys):
    for files in write(model_shaped(), tmp_path):
        assert THREE_D <= files
    assert "Warning" not in capsys.readouterr().out
