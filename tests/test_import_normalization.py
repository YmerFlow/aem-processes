"""The importers land data in the SimPEG static_instrument convention.

The convention is: dbdt per unit transmitter moment (tx divided out of the
arrays), the receiver area already in the delivered data, a model_info
scalefactor, and the GEX calibration (GateFactor, the amplitude factor) — both
applied at read, never baked in. There is no ymerflow `normalization` field any
more; the data simply IS this format.
"""

import sys
import types

import numpy as np
import pytest
from libaarhusxyz import GEX
from libaarhusxyz.export import msgpack as xyz_msgpack

from aem_processes.import_gdb_process import GeosoftGDBImporter
from aem_processes.import_msgpack_process import MsgpackImporter
from aem_processes.import_process import LibaarhusXYZImporter
from test_yf_processing_process import N_GATES, dataset_dir, storage, write_delivery

# turns (1.0) x TxLoopArea (342.0) x TxApproximateCurrent (9.0) for the LM.
LM_MOMENT = 1.0 * 342.0 * 9.0

#: A calibration amplitude factor, as a calibrated GEX carries it.
CALIBRATION = 0.97


def calibrate(gex_path, factor=CALIBRATION):
    """Give the delivery's GEX a calibration amplitude factor."""
    gex = GEX(str(gex_path))
    gex.gex_dict["Channel1"]["GateFactor"] = factor
    with open(gex_path, "wb") as f:
        gex.dump(f)


def run_import(tmp_path, label="import", scalefactor=1.0, tx_normalized=True,
               moments=(("LM", 9.0),), gate_factor=None, **extra):
    xyz_path, gex_path = write_delivery(tmp_path, moments)
    if gate_factor is not None:
        calibrate(gex_path, gate_factor)
    result = LibaarhusXYZImporter.run(
        storage_context=storage(tmp_path, label),
        xyzfile=f"file://{xyz_path}", gexfile=f"file://{gex_path}",
        scalefactor=scalefactor, tx_normalized=tx_normalized,
        projection=32613, **extra)
    return result["outputs"]["imported_data"]


def loaded(url):
    """The stored dataset read back through ymerflow's load(), the real read
    path — so arrays are reached by the canonical channel keys SimPEG uses,
    whatever on-disk naming the text forms round-tripped to."""
    from ymerflow_processing.dataset import load
    directory = dataset_dir(url)
    return load(f"{directory}/root.xyz", f"{directory}/root.gex")


def channel_values(dataset):
    channel = dataset.channels[0]
    return dataset.layer_data[channel.data_key].to_numpy(dtype=float)


def gate_factor(dataset):
    return dataset.survey.gex.gex_dict["Channel1"].get("GateFactor")


# ── The XYZ/GEX importer ─────────────────────────────────────────────────────

def test_import_produces_the_simpeg_convention(tmp_path):
    """No normalization field; scalefactor kept as metadata; and the result
    passes SimPEG's own base.py validation of the read-time scaling."""
    dataset = loaded(run_import(tmp_path))
    model_info = dataset.survey.xyz.model_info

    assert "normalization" not in model_info
    assert model_info["scalefactor"] == 1.0
    dbdt = -channel_values(dataset).flatten() * model_info["scalefactor"]
    assert np.nanmean(dbdt) < 1e-3     # mirrors SimPEG base.py do_validate()


def test_a_delivery_that_is_not_tx_normalized_is_divided_by_the_moment(tmp_path):
    """The SkyTEM default is tx already divided out; a raw instrument dump is
    not, so the importer divides each channel by its nominal dipole moment to
    produce the per-unit-moment convention."""
    base = channel_values(loaded(run_import(tmp_path, label="tx_true", tx_normalized=True)))
    divided = channel_values(loaded(run_import(tmp_path, label="tx_false", tx_normalized=False)))

    assert np.allclose(divided, base / LM_MOMENT)


def test_a_channel_without_a_moment_cannot_be_tx_normalized(tmp_path):
    """Dividing out the moment needs it; its absence is a hard error, because
    the convention cannot otherwise be produced."""
    xyz_path, gex_path = write_delivery(tmp_path, (("LM", 0.0),))  # current 0 -> moment 0
    with pytest.raises(ValueError, match="ApproxDipoleMoment"):
        LibaarhusXYZImporter.run(
            storage_context=storage(tmp_path, "no_moment"),
            xyzfile=f"file://{xyz_path}", gexfile=f"file://{gex_path}",
            scalefactor=1.0, tx_normalized=False, projection=32613)


def test_the_calibration_gatefactor_is_kept_as_delivered(tmp_path):
    """GateFactor is the calibration amplitude factor SimPEG multiplies in at
    read, so the import keeps it and leaves the arrays as delivered."""
    uncalibrated = channel_values(loaded(run_import(tmp_path, label="uncalibrated")))
    dataset = loaded(run_import(tmp_path, label="calibrated", gate_factor=CALIBRATION))

    assert gate_factor(dataset) == CALIBRATION
    assert np.allclose(channel_values(dataset), uncalibrated)


def test_the_removed_receiver_reset_is_refused(tmp_path):
    """rx_normalized reset GateFactor to 1, discarding the calibration. A saved
    version still asking for it fails rather than import differently."""
    with pytest.raises(ValueError, match="rx_normalized"):
        run_import(tmp_path, gate_factor=CALIBRATION, rx_normalized=True)


# ── The GDB importer ─────────────────────────────────────────────────────────

GDB_LINE = 100101
GDB_SOUNDINGS = 30


def fake_pygdb(channels):
    """A stand-in for the pygdb reader, serving one line of in-memory channels.

    It has the three members the importer reads; pygdb is installed per
    environment and is not needed to test the import itself.
    """
    class GDB:
        line_names = [f"L{GDB_LINE}"]

        def __init__(self, path):
            pass

        def channels_on_line(self, line):
            return list(channels)

        def read(self, line, name):
            return channels[name]

    return types.SimpleNamespace(GDB=GDB)


def run_gdb_import(tmp_path, monkeypatch, label="gdb", tx_normalized=True,
                   gate_factor=None):
    _, gex_path = write_delivery(tmp_path, (("LM", 9.0),))
    if gate_factor is not None:
        calibrate(gex_path, gate_factor)
    times = GEX(str(gex_path)).gex_dict["General"]["GateTime"][:N_GATES, 0]
    n = GDB_SOUNDINGS
    channels = {
        "Line": np.full(n, GDB_LINE), "Easting": 500000.0 + 25.0 * np.arange(n),
        "Northing": np.full(n, 4500000.0), "Ground_Elevation": np.full(n, 1900.0),
        "Tx_Loop_Clearance": np.full(n, 35.0),
        "dBdt_Z": np.tile(1e-6 * (times / 1e-5) ** -1.5, (n, 1)),
    }
    monkeypatch.setitem(sys.modules, "pygdb", fake_pygdb(channels))
    gdb_path = tmp_path / "survey.gdb"
    gdb_path.write_bytes(b"")
    result = GeosoftGDBImporter.run(
        storage_context=storage(tmp_path, label),
        gdbfile=f"file://{gdb_path}", gexfile=f"file://{gex_path}",
        scalefactor=1.0, tx_normalized=tx_normalized, projection=32613)
    return result["outputs"]["imported_data"], channels["dBdt_Z"]


def test_gdb_import_produces_the_simpeg_convention(tmp_path, monkeypatch):
    """The GDB path conforms as the XYZ path does: no normalization field, the
    calibration GateFactor kept, the arrays as delivered."""
    url, delivered = run_gdb_import(tmp_path, monkeypatch, gate_factor=CALIBRATION)
    dataset = loaded(url)

    assert "normalization" not in dataset.survey.xyz.model_info
    assert gate_factor(dataset) == CALIBRATION
    assert np.allclose(channel_values(dataset), delivered)


def test_a_gdb_that_is_not_tx_normalized_is_divided_by_the_moment(tmp_path, monkeypatch):
    url, delivered = run_gdb_import(tmp_path, monkeypatch, tx_normalized=False)

    assert np.allclose(channel_values(loaded(url)), delivered / LM_MOMENT)


# ── The msgpack importer ─────────────────────────────────────────────────────

def test_msgpack_assumes_the_convention_and_converts_nothing(tmp_path):
    """A ymerflow msgpack container is assumed already in the convention, so the
    arrays are passed through as-is; only a stale normalization field an older
    container carries is stripped."""
    source_url = run_import(tmp_path, label="source")
    xyz, gex = xyz_msgpack.load(source_url.split("file://", 1)[1], return_gex=True)
    xyz.model_info["normalization"] = "rx_tx"      # as an older container would carry
    data_name = xyz.layer_data_data_name(1)
    before = xyz.layer_data[data_name].values.copy()

    container = tmp_path / "container.msgpack"
    with open(container, "wb") as f:
        xyz.to_msgpack(f, gex=gex)
    result = MsgpackImporter.run(storage_context=storage(tmp_path, "reimport"),
                                 msgpack_file=f"file://{container}")
    out, _ = xyz_msgpack.load(
        result["outputs"]["imported_data"].split("file://", 1)[1], return_gex=True)

    assert "normalization" not in out.model_info
    assert np.allclose(out.layer_data[out.layer_data_data_name(1)].values, before)
