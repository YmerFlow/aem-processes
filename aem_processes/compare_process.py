"""Compare two inverted TEM resistivity models above the depth of investigation.

Produces a single *difference model* — an ordinary resistivity-model dataset whose
one data channel, ``delta_log_resistivity``, carries per-sounding, per-layer
Δlog₁₀ρ = log10(ρ_A) − log10(ρ_B). It plots as a curtain/grid like any inversion
output and grids with ``grid_tem``.

The agreement metrics (log₁₀ρ RMSE, bias, scatter, percentiles, correlation) are
simply the difference model's ``stats.json``. They are reported three ways as
sibling files in that same format: ``stats.json`` (overall), and the above-DOI
variants ``stats.above_doi_upper.json`` (shallow, conservative) and
``stats.above_doi_lower.json`` (deep), each also written per flight line.

Below the depth of investigation the two models are not constrained by the data,
so the above-DOI views are the point of the feature. The DOI mask uses the
shallower of the two models per sounding — ``min(DOI_A, DOI_B)`` — a layer counts
only where *both* models are reliable. The step-sampling and position-alignment
helpers are lifted from the standalone ``compare_truth.py`` eval script.
"""

import copy

import numpy as np
import libaarhusxyz
import libaarhusxyz.export.msgpack

from .utils import localize_urls
from .dataset_utils import DatasetWriter
from .stats import compute_channel_stats, compute_xyz_stats

#: The difference model's single data channel.
DELTA_CHANNEL = "delta_log_resistivity"

#: The above-DOI stats variants: a name and the flightline DOI column it masks by.
_DOI_VARIANTS = [
    {"name": "above_doi_upper", "doi_column": "doi_upper"},
    {"name": "above_doi_lower", "doi_column": "doi_lower"},
]

#: The ALC naming standard renames "resistivity" to "rho"; accept either so the
#: user-facing default survives normalize(naming_standard="alc").
_QUANTITY_ALIASES = {"resistivity": ("resistivity", "rho"), "rho": ("rho", "resistivity")}


def _resolve_quantity(xyz, quantity):
    """Return the actual layer_data channel for the requested quantity, or None.

    Tries the name as given, then its ALC alias (resistivity ↔ rho)."""
    ld = getattr(xyz, "layer_data", {}) or {}
    for candidate in _QUANTITY_ALIASES.get(quantity, (quantity,)):
        if candidate in ld:
            return candidate
    return None


def _find_col(columns, name):
    """Case-insensitive column lookup; returns the actual name or None."""
    for col in columns:
        if col.lower() == name.lower():
            return col
    return None


def _has_doi(xyz):
    """True if the (un-normalized) model already carries real DOI columns.

    Checked BEFORE normalize(), because libaarhusxyz add_defaults fabricates
    doi_upper=300 / doi_lower=500 for any resistivity model that lacks them —
    a fabricated DOI we must never treat as real.
    """
    cols = xyz.flightlines.columns
    return (_find_col(cols, "doi_upper") is not None
            and _find_col(cols, "doi_lower") is not None)


def _log10_positive(rho):
    """log10 of an array, NaN where values are non-finite or ≤ 0."""
    rho = np.asarray(rho, dtype=np.float64)
    out = np.full(rho.shape, np.nan)
    good = np.isfinite(rho) & (rho > 0)
    out[good] = np.log10(rho[good])
    return out


def _step_sample(log_values, dep_top, query_depths):
    """Piecewise-constant log-resistivity of one sounding sampled at query_depths.

    For each query depth, take the value of the layer whose top is the deepest
    one not below it (``compare_truth.step_sample``).
    """
    idx = np.clip(
        np.searchsorted(dep_top, query_depths, side="right") - 1,
        0, len(dep_top) - 1)
    return log_values[idx]


def _align(a_x, a_y, b_x, b_y, tol):
    """Map each A sounding to its nearest B sounding by UTMX/UTMY.

    Returns (idx, matched): idx[i] is the B index nearest A sounding i, matched[i]
    is True when that B sounding lies within `tol` metres. Identical-position
    surveys of equal length take the identity fast path (the common case).
    """
    n_a = len(a_x)
    if (len(a_x) == len(b_x)
            and np.nanmax(np.abs(a_x - b_x)) < tol
            and np.nanmax(np.abs(a_y - b_y)) < tol):
        return np.arange(n_a), np.ones(n_a, dtype=bool)

    idx = np.empty(n_a, dtype=int)
    dist = np.empty(n_a, dtype=np.float64)
    for i in range(n_a):
        d2 = (b_x - a_x[i]) ** 2 + (b_y - a_y[i]) ** 2
        j = int(np.argmin(d2))
        idx[i] = j
        dist[i] = np.sqrt(d2[j])
    return idx, dist <= tol


def _rmse(values, mask=None):
    """RMS over finite values (within mask); NaN if none remain."""
    x = values.ravel() if mask is None else values[mask]
    x = x[np.isfinite(x)]
    return float(np.sqrt(np.mean(x ** 2))) if len(x) else float("nan")


def _correlation(a, b, mask=None):
    """Pearson correlation of a vs b over finite pairs (within mask).

    None when fewer than two usable pairs remain or either side is constant.
    Rounded so a perfect match reads as exactly 1.0 rather than 1.0000000002.
    """
    a = np.asarray(a, dtype=np.float64).ravel()
    b = np.asarray(b, dtype=np.float64).ravel()
    if mask is not None:
        m = np.asarray(mask, dtype=bool).ravel()
        a, b = a[m], b[m]
    good = np.isfinite(a) & np.isfinite(b)
    a, b = a[good], b[good]
    if len(a) < 2 or np.std(a) == 0 or np.std(b) == 0:
        return None
    return round(float(np.corrcoef(a, b)[0, 1]), 6)


def _doi_mask(xyz, doi_column):
    """Above-DOI boolean mask (n_sounding, n_layer): True where the layer is at
    or above the sounding's DOI, i.e. ``dep_bot <= DOI`` (mirroring libaarhusxyz
    calculate_doi_layer, which marks ``dep_bot > doi`` as below). A NaN DOI
    (sounding not inverted) yields all-False, excluding it entirely.
    """
    doi = np.asarray(xyz.flightlines[doi_column].values, dtype=np.float64)
    dep_bot = np.asarray(xyz.layer_data["dep_bot"].values, dtype=np.float64)
    return dep_bot <= doi[:, None]


class ComparisonWriter(DatasetWriter):
    """DatasetWriter that reports the Δ channel's real stats — with correlation
    merged in, and above-DOI masked variants — by overriding compute_stats only.

    log_a / log_b are the transient (n_sounding, n_layer) log-resistivity arrays
    the correlation needs; they are never written to msgpack/xyz/parts. For a
    per-line part (which carries apply_idx from split_by_line) they are sliced to
    that line's soundings so each part's correlation is over that line alone.
    """

    def __init__(self, *args, log_a, log_b, variants, **kwargs):
        super().__init__(*args, **kwargs)
        self.log_a = np.asarray(log_a, dtype=np.float64)
        self.log_b = np.asarray(log_b, dtype=np.float64)
        self.variants = variants

    def _logs_for(self, xyz):
        if "apply_idx" in xyz.flightlines.columns:
            idx = xyz.flightlines["apply_idx"].values
            return self.log_a[idx], self.log_b[idx]
        return self.log_a, self.log_b

    def _delta_stats(self, base, delta_df, log_a, log_b, mask):
        """A copy of `base` whose Δ channel carries full (never constant-collapsed)
        stats plus an additive `correlation` scalar and per-layer list."""
        stats = copy.deepcopy(base)
        block = compute_channel_stats(delta_df)
        block["all"]["correlation"] = _correlation(log_a, log_b, mask)
        block["layers"]["correlation"] = [
            _correlation(log_a[:, j], log_b[:, j],
                         None if mask is None else mask[:, j])
            for j in range(log_a.shape[1])
        ]
        stats["layer_data"][DELTA_CHANNEL] = block
        return stats

    def compute_stats(self, xyz):
        base = compute_xyz_stats(xyz)
        log_a, log_b = self._logs_for(xyz)
        delta_df = xyz.layer_data[DELTA_CHANNEL]
        out = {None: self._delta_stats(base, delta_df, log_a, log_b, None)}
        for variant in self.variants:
            mask = _doi_mask(xyz, variant["doi_column"])
            out[variant["name"]] = self._delta_stats(
                base, delta_df.where(mask), log_a, log_b, mask)
        return out


class CompareModels:
    """Compare two inverted resistivity models, above the depth of investigation."""

    @classmethod
    def schema(cls):
        return {
            "type": "object",
            "title": "Compare TEM Models",
            "description": (
                "Compare two inverted resistivity models (A and B) and produce a "
                "difference model carrying per-sounding, per-layer Δlog₁₀ρ = "
                "log10(ρ_A / ρ_B), plus agreement metrics (RMSE, bias, scatter, "
                "correlation) as stats.json — overall and above each model's DOI."
            ),
            "properties": {
                "input_model_a": {
                    "type": "string",
                    "format": "uri",
                    "x-format": "dataset",
                    "title": "Model A (reference)",
                    "description": (
                        "Reference resistivity model. Its layer geometry defines the "
                        "difference mesh and the sounding order of the comparison."
                    ),
                },
                "input_model_b": {
                    "type": "string",
                    "format": "uri",
                    "x-format": "dataset",
                    "title": "Model B",
                    "description": "Resistivity model to compare against model A.",
                },
                "quantity": {
                    "type": "string",
                    "title": "Compared Channel",
                    "description": (
                        "The layer_data channel to compare (both models must carry "
                        "it). The comparison is always in log₁₀ space."
                    ),
                    "default": "resistivity",
                },
                "position_tolerance": {
                    "type": "number",
                    "title": "Position Tolerance (m)",
                    "description": (
                        "Soundings within this distance are treated as the same "
                        "location when aligning model B to model A."
                    ),
                    "default": 1.0,
                    "exclusiveMinimum": 0,
                },
            },
            "required": ["input_model_a", "input_model_b"],
        }

    @classmethod
    def run(cls, storage_context=None, **kwargs):
        print("Comparing two resistivity models…")
        print(f"Parameters: {kwargs}")

        if not storage_context:
            raise ValueError("storage_context is required")

        process_id = storage_context["process_id"]
        process_version = storage_context["version"]
        storage_base = storage_context["storage_base"]
        storage_kwargs = storage_context["storage_kwargs"]

        url_a = kwargs.get("input_model_a")
        url_b = kwargs.get("input_model_b")
        if not url_a or not url_b:
            raise ValueError("input_model_a and input_model_b are both required")

        quantity = kwargs.get("quantity") or "resistivity"
        position_tolerance = float(kwargs.get("position_tolerance", 1.0))

        with localize_urls({"a": url_a, "b": url_b}, storage_kwargs) as localized:
            xyz_a, gex_a = libaarhusxyz.export.msgpack.load(localized["a"], True)
            xyz_b, _gex_b = libaarhusxyz.export.msgpack.load(localized["b"], True)

            # Detect real DOI columns BEFORE normalize fabricates 300/500 defaults.
            doi_available = _has_doi(xyz_a) and _has_doi(xyz_b)

            xyz_a.normalize(naming_standard="alc")
            xyz_b.normalize(naming_standard="alc")

            quantity_a = _resolve_quantity(xyz_a, quantity)
            quantity_b = _resolve_quantity(xyz_b, quantity)
            for label, xyz, resolved in (("A", xyz_a, quantity_a), ("B", xyz_b, quantity_b)):
                if resolved is None:
                    available = list(getattr(xyz, "layer_data", {}) or {})
                    raise ValueError(
                        f"Model {label} is not a resistivity model with a "
                        f"'{quantity}' layer_data channel. Available channels: "
                        f"{available}")

            # ── log-resistivity and A's layer geometry ──────────────────────────
            rho_a = np.asarray(xyz_a.layer_data[quantity_a].values, dtype=np.float64)
            log_a = _log10_positive(rho_a)
            n_a, n_layer_a = log_a.shape

            dep_top_a = np.asarray(xyz_a.layer_data["dep_top"].values, dtype=np.float64)
            dep_bot_a = np.asarray(xyz_a.layer_data["dep_bot"].values, dtype=np.float64)
            # Representative sample depth per A-layer: its midpoint, falling back to
            # the layer top where the bottom is non-finite (the open last layer).
            dep_bot_a_safe = np.where(np.isfinite(dep_bot_a), dep_bot_a, dep_top_a)
            mid_a = (dep_top_a + dep_bot_a_safe) / 2.0

            rho_b = np.asarray(xyz_b.layer_data[quantity_b].values, dtype=np.float64)
            log_b = _log10_positive(rho_b)
            dep_top_b = np.asarray(xyz_b.layer_data["dep_top"].values, dtype=np.float64)

            # ── Align B to A by position ─────────────────────────────────────────
            ax = np.asarray(xyz_a.flightlines["UTMX"].values, dtype=np.float64)
            ay = np.asarray(xyz_a.flightlines["UTMY"].values, dtype=np.float64)
            bx = np.asarray(xyz_b.flightlines["UTMX"].values, dtype=np.float64)
            by = np.asarray(xyz_b.flightlines["UTMY"].values, dtype=np.float64)
            idx, matched = _align(ax, ay, bx, by, position_tolerance)
            n_unmatched = int((~matched).sum())
            if n_unmatched:
                print(f"WARNING: {n_unmatched} of {n_a} A soundings have no B sounding "
                      f"within {position_tolerance} m; their Δ is NaN.")

            # ── Step-sample B onto A's layers; build Δ ───────────────────────────
            log_b_on_a = np.full((n_a, n_layer_a), np.nan)
            for i in range(n_a):
                if matched[i]:
                    j = idx[i]
                    log_b_on_a[i] = _step_sample(log_b[j], dep_top_b[j], mid_a[i])

            delta = log_a - log_b_on_a  # NaN where either side is NaN

            # ── Difference model flightlines (A's), plus effective DOIs ──────────
            fl = xyz_a.flightlines.copy()
            eff_upper = eff_lower = None
            if doi_available:
                upper_a = np.asarray(xyz_a.flightlines["doi_upper"].values, dtype=np.float64)
                lower_a = np.asarray(xyz_a.flightlines["doi_lower"].values, dtype=np.float64)
                upper_b = np.asarray(xyz_b.flightlines["doi_upper"].values, dtype=np.float64)
                lower_b = np.asarray(xyz_b.flightlines["doi_lower"].values, dtype=np.float64)
                upper_b_on_a = np.full(n_a, np.nan)
                lower_b_on_a = np.full(n_a, np.nan)
                upper_b_on_a[matched] = upper_b[idx[matched]]
                lower_b_on_a[matched] = lower_b[idx[matched]]
                # Shallower of the two per sounding (NaN where unmatched).
                eff_upper = np.minimum(upper_a, upper_b_on_a)
                eff_lower = np.minimum(lower_a, lower_b_on_a)
                fl["doi_upper"] = eff_upper
                fl["doi_lower"] = eff_lower
            else:
                print("WARNING: one or both models lack real doi_upper/doi_lower "
                      "columns; above-DOI stats variants are omitted (no 300/500 "
                      "fallback). Set doi__enable on the inversion to get them.")
                # Do not persist any fabricated DOI.
                for name in ("doi_upper", "doi_lower"):
                    col = _find_col(fl.columns, name)
                    if col is not None:
                        fl[col] = np.nan

            # Per-sounding overall agreement, carried on the flightlines so it
            # rides into the xyz/msgpack, every per-line part, and stats: RMSE
            # and MAE of Δ across that sounding's layers (all layers, unmasked).
            # Computed via finite-value sums so an all-NaN (unmatched) sounding
            # yields NaN without a mean-of-empty-slice warning.
            finite = np.isfinite(delta)
            count = finite.sum(axis=1)
            sq_sum = np.where(finite, delta ** 2, 0.0).sum(axis=1)
            abs_sum = np.where(finite, np.abs(delta), 0.0).sum(axis=1)
            with np.errstate(invalid="ignore", divide="ignore"):
                fl["delta_rmse"] = np.sqrt(sq_sum / count)
                fl["delta_mae"] = abs_sum / count

            # Δ-only layer_data on A's geometry (no A/B copies persisted).
            delta_df = xyz_a.layer_data[quantity_a].copy()
            delta_df.loc[:, :] = delta
            diff_dict = {
                "flightlines": fl,
                "layer_data": {
                    DELTA_CHANNEL: delta_df,
                    "dep_top": xyz_a.layer_data["dep_top"].copy(),
                    "dep_bot": xyz_a.layer_data["dep_bot"].copy(),
                },
                "model_info": dict(xyz_a.model_info),
            }
            if "file_meta" in xyz_a.model_dict:
                diff_dict["file_meta"] = dict(xyz_a.model_dict["file_meta"])
            diff_xyz = libaarhusxyz.XYZ(diff_dict)

            # ── Log a one-line summary per variant ───────────────────────────────
            print(f"  overall:          log10ρ RMSE = {_rmse(delta):.4f} decades")
            variants = []
            if doi_available:
                mask_upper = dep_bot_a <= eff_upper[:, None]
                mask_lower = dep_bot_a <= eff_lower[:, None]
                print(f"  above_doi_upper:  log10ρ RMSE = {_rmse(delta, mask_upper):.4f} decades")
                print(f"  above_doi_lower:  log10ρ RMSE = {_rmse(delta, mask_lower):.4f} decades")
                variants = _DOI_VARIANTS
            print(f"  unmatched soundings: {n_unmatched}")

            # ── Write the difference model ───────────────────────────────────────
            # ComparisonWriter overrides only compute_stats, to add the Δ
            # channel's correlation and the above-DOI variants; everything else
            # (msgpack/xyz/gex/geojson/VTK/GLB, per-line parts, info.json) is the
            # shared DatasetWriter path. log_b_on_a is the step-sampled B used
            # for correlation; it is never persisted.
            print("Writing difference model…")
            dataset_id = ComparisonWriter(
                diff_xyz, gex_a, "model-comparison",
                process_id, process_version, storage_base, storage_kwargs,
                log_a=log_a, log_b=log_b_on_a, variants=variants).write()

            difference_url = (
                f"{storage_base}/processes/{process_id}/{process_version}"
                f"/datasets/{dataset_id}/root.msgpack")

        print("Model comparison complete.")
        return {"status": "success", "outputs": {"difference": difference_url}}
