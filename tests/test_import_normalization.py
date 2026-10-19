"""Both importers land data in the SimPEG static_instrument convention.

The convention is: dbdt per unit transmitter moment (tx divided out of the
arrays), receiver geometry NOT in the arrays but carried by the GEX GateFactor,
and a model_info scalefactor — both applied at read, never baked in. There is no
ymerflow `normalization` field any more; the data simply IS this format. See
simpeg/.../static_instrument/data_format.md.
"""

import numpy as np
import pytest
from libaarhusxyz.export import msgpack as xyz_msgpack

from aem_processes.import_msgpack_process import MsgpackImporter
from aem_processes.import_process import LibaarhusXYZImporter
from test_yf_processing_process import dataset_dir, storage, write_delivery

# turns (1.0) x TxLoopArea (342.0) x TxApproximateCurrent (9.0) for the LM.
LM_MOMENT = 1.0 * 342.0 * 9.0


def run_import(tmp_path, label="import", scalefactor=1.0, tx_normalized=True,
               rx_normalized=False, moments=(("LM", 9.0),)):
    xyz_path, gex_path = write_delivery(tmp_path, moments)
    result = LibaarhusXYZImporter.run(
        storage_context=storage(tmp_path, label),
        xyzfile=f"file://{xyz_path}", gexfile=f"file://{gex_path}",
        scalefactor=scalefactor, tx_normalized=tx_normalized,
        rx_normalized=rx_normalized, projection=32613)
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


def test_a_receiver_normalized_delivery_sets_gatefactor_to_one(tmp_path):
    """Receiver geometry already in the arrays would be applied twice at read,
    so the importer sets GateFactor = 1 to make the read-time multiply a no-op.
    The arrays themselves are untouched on the receiver axis."""
    base = channel_values(loaded(run_import(tmp_path, label="rx_false", rx_normalized=False)))
    dataset = loaded(run_import(tmp_path, label="rx_true", rx_normalized=True))

    assert dataset.survey.gex.gex_dict["Channel1"]["GateFactor"] == 1.0
    assert np.allclose(channel_values(dataset), base)


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
