"""The compare_tem_models process type, end to end against a local store.

Two synthetic resistivity models are written as msgpack datasets (as the
inversion would write them) and compared through CompareModels.run(). The
difference model and its stats.json family are then read back and checked.
"""

import json

import numpy as np
import pandas as pd
import pytest
from libaarhusxyz import GEX, XYZ
from libaarhusxyz.export import msgpack as xyz_msgpack

from aem_processes.compare_process import CompareModels
from aem_processes.stats import STATS_MIME, compute_xyz_stats

PER_LINE = 6
N = 2 * PER_LINE
DEP_TOP = np.array([0.0, 10.0, 20.0, 30.0, 40.0])
DEP_BOT = np.array([10.0, 20.0, 30.0, 40.0, np.inf])
NLAY = len(DEP_TOP)


# ── Fixtures: resistivity models, as files ───────────────────────────────────

def make_model(rho, doi_upper=None, doi_lower=None, with_doi=True,
               dep_top=DEP_TOP, dep_bot=DEP_BOT):
    """A resistivity model XYZ: two flight lines, layered earth.

    rho: (N, n_layer) linear resistivity.  with_doi controls whether the real
    doi_upper/doi_lower columns exist before normalization.
    """
    n_layer = len(dep_top)
    fl = pd.DataFrame({
        "Line": np.repeat([100, 200], PER_LINE),
        "UTMX": np.tile(np.arange(PER_LINE) * 25.0, 2) + 500000.0,
        "UTMY": np.repeat([4500000.0, 4501000.0], PER_LINE),
        "Topography": 100.0,
    })
    if with_doi:
        fl["doi_upper"] = np.full(N, 35.0) if doi_upper is None else doi_upper
        fl["doi_lower"] = np.full(N, 45.0) if doi_lower is None else doi_lower
    layer_data = {
        "resistivity": pd.DataFrame(np.asarray(rho, dtype=float)),
        "dep_top": pd.DataFrame(np.tile(dep_top, (N, 1))),
        "dep_bot": pd.DataFrame(np.tile(dep_bot, (N, 1))),
    }
    return XYZ({"flightlines": fl, "layer_data": layer_data,
                "model_info": {"projection": 32613}, "file_meta": {}})


def write_model(xyz, path):
    gex = GEX({"General": {"GateTime": np.zeros((1, 3)), "TxLoopArea": 1.0},
               "Channel1": {"NoGates": 1.0}})
    with open(path, "wb") as f:
        xyz.to_msgpack(f, gex=gex)


def storage(tmp_path, process_id):
    return {"process_id": process_id, "project_id": "test", "version": "0",
            "storage_base": f"file://{tmp_path}/store",
            "storage_kwargs": {"auto_mkdir": True}}


def run_compare(tmp_path, model_a, model_b, label="compare", **params):
    """Write both models, run the comparison, return the difference URL."""
    write_model(model_a, f"{tmp_path}/a.msgpack")
    write_model(model_b, f"{tmp_path}/b.msgpack")
    result = CompareModels.run(
        storage_context=storage(tmp_path, label),
        input_model_a=f"file://{tmp_path}/a.msgpack",
        input_model_b=f"file://{tmp_path}/b.msgpack",
        **params)
    return result["outputs"]["difference"]


def dataset_dir(url):
    return url.split("file://", 1)[1].rsplit("/", 1)[0]


def read_stats(url, name="stats.json"):
    with open(f"{dataset_dir(url)}/{name}") as f:
        return json.load(f)


def read_info(url):
    with open(f"{dataset_dir(url)}/info.json") as f:
        return json.load(f)


def load_diff(url):
    """Read the written difference model back as an XYZ."""
    return xyz_msgpack.load(f"{dataset_dir(url)}/root.msgpack")


def delta_block(url, name="stats.json"):
    return read_stats(url, name)["layer_data"]["delta_log_resistivity"]


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def rho():
    rng = np.random.default_rng(0)
    return 10 ** rng.uniform(1.0, 3.0, (N, NLAY))


def test_identity(tmp_path, rho, capsys):
    """A model compared with itself: Δ ≡ 0, rms 0, correlation 1, 0 unmatched."""
    url = run_compare(tmp_path, make_model(rho), make_model(rho), "identity")
    block = delta_block(url)
    assert block["all"]["rms"] == 0.0
    assert block["all"]["mean"] == 0.0
    assert block["all"]["correlation"] == 1.0
    # Every sounding/layer is finite — nothing dropped as unmatched.
    assert block["all"]["count"] == N * NLAY
    assert "unmatched soundings: 0" in capsys.readouterr().out


def test_known_offset(tmp_path, rho):
    """B = A / k ⇒ Δ = log10(ρ_A/ρ_B) = log10(k) everywhere; rms = |log10 k|."""
    k = 10.0
    url = run_compare(tmp_path, make_model(rho), make_model(rho / k), "offset")
    block = delta_block(url)
    assert block["all"]["mean"] == pytest.approx(np.log10(k))
    assert block["all"]["rms"] == pytest.approx(abs(np.log10(k)))
    assert block["all"]["correlation"] == 1.0


def test_doi_masking_sibling_files(tmp_path, rho):
    """Models agree above a DOI and disagree below: above-DOI rms ≪ overall."""
    rho_b = rho.copy()
    # Disagree only in the two deepest layers (indices 3, 4).
    rho_b[:, 3:] *= 100.0
    # A sees shallower than B on doi_upper; the effective bound is the min.
    model_a = make_model(rho, doi_upper=np.full(N, 25.0), doi_lower=np.full(N, 35.0))
    model_b = make_model(rho_b, doi_upper=np.full(N, 35.0), doi_lower=np.full(N, 45.0))
    url = run_compare(tmp_path, model_a, model_b, "doi")

    overall = delta_block(url, "stats.json")["all"]["rms"]
    upper = delta_block(url, "stats.above_doi_upper.json")["all"]["rms"]
    lower = delta_block(url, "stats.above_doi_lower.json")["all"]["rms"]
    assert upper < overall
    assert lower < overall
    # doi_upper min is 25 ⇒ only layers with dep_bot<=25 (idx 0,1) survive, all
    # agree ⇒ rms 0. doi_lower min is 35 ⇒ layers dep_bot<=35 (idx 0,1,2) ⇒ 0.
    assert upper == 0.0
    assert lower == 0.0

    # Deep layers collapse to count 0 in the masked files.
    upper_layers = delta_block(url, "stats.above_doi_upper.json")["layers"]["count"]
    assert upper_layers[3] == 0 and upper_layers[4] == 0
    assert upper_layers[0] > 0

    # Effective DOIs on the diff model are the shallower (min) of the two.
    diff = load_diff(url)
    assert np.allclose(diff.flightlines["doi_upper"].values, 25.0)
    assert np.allclose(diff.flightlines["doi_lower"].values, 35.0)


def test_delta_only_model(tmp_path, rho):
    """The written model carries only delta + geometry — not A or B."""
    url = run_compare(tmp_path, make_model(rho), make_model(rho * 2), "deltaonly")
    diff = load_diff(url)
    assert "delta_log_resistivity" in diff.layer_data
    assert "resistivity" not in diff.layer_data
    assert "rho" not in diff.layer_data
    for forbidden in ("log_resistivity_a", "log_resistivity_b",
                      "resistivity_a", "resistivity_b"):
        assert forbidden not in diff.layer_data
    # The .xyz text form likewise mentions neither source channel.
    with open(f"{dataset_dir(url)}/root.xyz") as f:
        text = f.read()
    assert "delta_log_resistivity".upper() in text.upper()


def test_correlation_additive_and_per_part(tmp_path, rho):
    """correlation lives inside the existing blocks without displacing a key,
    and each per-line part carries its own correlation."""
    url = run_compare(tmp_path, make_model(rho), make_model(rho * 3), "corr")
    block = delta_block(url)
    # Additive: correlation present, every compute_column_stats key still there.
    for key in ("count", "min", "max", "mean", "rms", "std", "p50"):
        assert key in block["all"]
    assert "correlation" in block["all"]
    assert "correlation" in block["layers"]

    # Per-part file exists and carries a correlation over that line only.
    info = read_info(url)
    assert info["parts"], "expected per-line parts"
    a_part = next(iter(info["parts"]))
    part_stats = read_stats(url, f"parts/{a_part}.stats.json")
    assert "correlation" in part_stats["layer_data"]["delta_log_resistivity"]["all"]


def test_info_lists_mask_variants(tmp_path, rho):
    """info.json keeps the overall stats MIME and adds parameterised variants."""
    url = run_compare(tmp_path, make_model(rho), make_model(rho * 2), "info")
    files = read_info(url)["files"]
    assert STATS_MIME in files
    assert f"{STATS_MIME};mask=above_doi_upper" in files
    assert f"{STATS_MIME};mask=above_doi_lower" in files


def test_missing_doi(tmp_path, rho, capsys):
    """Models without real DOI: above_doi_* omitted, overall still written,
    no fabricated 300/500 DOI persisted."""
    url = run_compare(tmp_path, make_model(rho, with_doi=False),
                      make_model(rho * 2, with_doi=False), "nodoi")
    import os
    assert os.path.exists(f"{dataset_dir(url)}/stats.json")
    assert not os.path.exists(f"{dataset_dir(url)}/stats.above_doi_upper.json")
    assert not os.path.exists(f"{dataset_dir(url)}/stats.above_doi_lower.json")
    files = read_info(url)["files"]
    assert f"{STATS_MIME};mask=above_doi_upper" not in files

    diff = load_diff(url)
    # No 300/500 fallback: the diff's DOI columns are either absent or all-NaN
    # (all-NaN flightline columns are dropped on msgpack write) — never the
    # fabricated 300/500 default.
    for col in ("doi_upper", "doi_lower"):
        if col in diff.flightlines.columns:
            assert np.all(np.isnan(diff.flightlines[col].values))
    assert "WARNING" in capsys.readouterr().out


def test_per_sounding_rmse_mae_on_flightlines(tmp_path, rho):
    """The diff model's flightlines carry per-sounding overall RMSE and MAE of
    Δ across layers — persisted (not just in stats) and sliced into each part."""
    k = 100.0  # B = A / k ⇒ Δ = log10(k) in every layer
    url = run_compare(tmp_path, make_model(rho), make_model(rho / k), "persnd")
    diff = load_diff(url)
    assert "delta_rmse" in diff.flightlines.columns
    assert "delta_mae" in diff.flightlines.columns
    # Δ is a constant log10(k) per layer ⇒ per-sounding RMSE = MAE = |log10 k|.
    assert np.allclose(diff.flightlines["delta_rmse"].values, abs(np.log10(k)))
    assert np.allclose(diff.flightlines["delta_mae"].values, abs(np.log10(k)))
    # The xyz text form carries them too.
    with open(f"{dataset_dir(url)}/root.xyz") as f:
        text = f.read().upper()
    assert "DELTA_RMSE" in text and "DELTA_MAE" in text


def test_mismatched_meshes(tmp_path, rho):
    """A (5 layers) vs B (6 layers): B step-sampled onto A's mesh; stats finite."""
    b_dep_top = np.array([0.0, 8.0, 16.0, 24.0, 32.0, 40.0])
    b_dep_bot = np.array([8.0, 16.0, 24.0, 32.0, 40.0, np.inf])
    rho_b = 10 ** np.random.default_rng(1).uniform(1.0, 3.0, (N, 6))
    model_a = make_model(rho)
    model_b = make_model(rho_b, dep_top=b_dep_top, dep_bot=b_dep_bot)
    url = run_compare(tmp_path, model_a, model_b, "mesh")
    diff = load_diff(url)
    assert diff.layer_data["delta_log_resistivity"].shape[1] == NLAY
    block = delta_block(url)
    assert block["all"]["count"] > 0
    assert np.isfinite(block["all"]["rms"])


def test_backward_compatible_stats(tmp_path, rho):
    """compute_xyz_stats is untouched: an ordinary model's stats carry no
    correlation key — correlation lives only in the comparator's output."""
    model = make_model(rho)
    model.normalize(naming_standard="alc")
    plain = compute_xyz_stats(model)
    for block in plain["layer_data"].values():
        assert "correlation" not in block.get("all", {})
        assert "correlation" not in block.get("layers", {})
