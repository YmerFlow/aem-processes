"""Utility functions for writing and managing datasets.

`DatasetWriter` serialises one normalized XYZ model to storage in every
supported format — msgpack, GeoJSON, Aarhus XYZ text, GEX, stats.json, VTK/GLB
— for the root and for each flight line, then writes an ``info.json`` index.
Each write step is its own method so a subclass can override or wrap exactly
one of them (e.g. the model-comparison process overrides ``write_stats`` to add
correlation and above-DOI variants) without reimplementing the rest.

``write_dataset`` is the thin function form every existing process type calls.
"""

import uuid
import json
import io
import fsspec
import slugify
from .stats import compute_xyz_stats, STATS_MIME


class DatasetWriter:
    """Write a normalized XYZ model to storage in all supported formats.

    The XYZ object must be normalized before writing — split_by_line() relies
    on xyz.line_id_column, which is only available after normalization.
    """

    def __init__(self, xyz, gex, dataset_name, process_id, process_version,
                 storage_base, storage_kwargs, extra_files=None):
        """
        Args:
            xyz: libaarhusxyz.XYZ instance (normalized before calling).
            gex: libaarhusxyz.GEX instance.
            dataset_name: Name for this dataset.
            process_id: Process ID.
            process_version: Process version number (string or int).
            storage_base: Storage base URL.
            storage_kwargs: fsspec storage arguments.
            extra_files: Optional {mime_type: (filename, text)} written beside
                the dataset and listed in its info.json, e.g. a run report.
        """
        self.xyz = xyz
        self.gex = gex
        self.dataset_name = dataset_name
        self.storage_kwargs = storage_kwargs
        self.extra_files = extra_files or {}
        self.dataset_id = str(uuid.uuid4())
        self.prefix = (
            f"{storage_base}/processes/{process_id}/{process_version}"
            f"/datasets/{self.dataset_id}")

    # ── orchestration ──────────────────────────────────────────────────────
    def write(self):
        """Write root, every flight-line part, and info.json. Returns the id."""
        files = self.write_root()
        parts = self.write_parts()
        self.write_info(files, parts)
        return self.dataset_id

    # ── single-file writers (each overridable) ──────────────────────────────
    def _open(self, url, mode="w"):
        return fsspec.open(url, mode, **self.storage_kwargs)

    def write_msgpack(self, xyz, url):
        print(f"Writing msgpack to: {url}")
        with self._open(url, "wb") as f:
            xyz.to_msgpack(f, gex=self.gex)

    def write_geojson(self, xyz, url):
        buffer = io.StringIO()
        xyz.to_geojson(buffer)
        geojson = json.loads(buffer.getvalue())
        for feature in geojson.get("features", []):
            feature.setdefault("properties", {})["dataset_id"] = self.dataset_id
        print(f"Writing geography to: {url}")
        with self._open(url) as f:
            json.dump(geojson, f)

    def write_xyz(self, xyz, url):
        print(f"Writing XYZ to: {url}")
        with self._open(url, "wb") as f:
            xyz.dump(f)

    def write_gex(self, url):
        print(f"Writing GEX to: {url}")
        with self._open(url, "wb") as f:
            self.gex.dump(f)

    def compute_stats(self, xyz):
        """The stats dict(s) for one scope, keyed by variant name.

        Returns {variant_name: stats_dict}; the None key is the standard
        ``stats.json``. Subclasses override only this to add variants (e.g. the
        model comparison's above-DOI masks) or extra fields — the file I/O and
        info.json registration in write_stats stay shared.
        """
        return {None: compute_xyz_stats(xyz)}

    def write_stats(self, xyz, stats_url_for):
        """Write the stats file(s) for one scope (root or a flight line) and
        return {mime_type: url} to merge into that scope's file map.

        `stats_url_for(variant_name)` returns the url for a stats file;
        variant_name None is the standard ``stats.json``. Variants are listed
        under STATS_MIME + ";mask={name}" so the overall stays backward-compatible.
        """
        files = {}
        for variant_name, stats in self.compute_stats(xyz).items():
            url = stats_url_for(variant_name)
            print(f"Writing stats to: {url}")
            with self._open(url) as f:
                json.dump(stats, f, indent=2)
            mime = (STATS_MIME if variant_name is None
                    else f"{STATS_MIME};mask={variant_name}")
            files[mime] = url
        return files

    def write_vtk_glb(self, xyz, vtk_url, glb_url, label=""):
        """Write VTK + GLB for a resistivity model (has layer_data). 3D export
        is best-effort: a failure logs a warning and leaves the rest intact."""
        files = {}
        if not (hasattr(xyz, "layer_data") and xyz.layer_data):
            return files
        try:
            print(f"Writing VTK to: {vtk_url}")
            with self._open(vtk_url) as f:
                xyz.to_vtk(f)
            files["model/vnd.vtk"] = vtk_url

            print(f"Writing GLB to: {glb_url}")
            glb_buffer = io.BytesIO()
            xyz.to_glb(glb_buffer)
            glb_buffer.seek(0)
            with self._open(glb_url, "wb") as f:
                f.write(glb_buffer.read())
            files["model/gltf-binary"] = glb_url
        except Exception as e:
            print(f"Warning: Could not write 3D formats (VTK/GLB){label}: {e}")
            print("Continuing without 3D model exports...")
        return files

    # ── root ─────────────────────────────────────────────────────────────────
    def write_root(self):
        p = self.prefix
        self.write_msgpack(self.xyz, f"{p}/root.msgpack")
        self.write_geojson(self.xyz, f"{p}/root.geojson")
        self.write_xyz(self.xyz, f"{p}/root.xyz")
        self.write_gex(f"{p}/root.gex")

        files = {
            "application/x-aarhusxyz-msgpack": f"{p}/root.msgpack",
            "application/geo+json": f"{p}/root.geojson",
            "text/x-aarhusxyz": f"{p}/root.xyz",
            "text/x-aarhusxyz-gex": f"{p}/root.gex",
        }
        files.update(self.write_stats(self.xyz, self._root_stats_url))

        for mime_type, (filename, text) in self.extra_files.items():
            url = f"{p}/{filename}"
            print(f"Writing {filename} to: {url}")
            with self._open(url) as f:
                f.write(text)
            files[mime_type] = url

        files.update(self.write_vtk_glb(self.xyz, f"{p}/root.vtk", f"{p}/root.glb"))
        return files

    def _root_stats_url(self, mask_name):
        return (f"{self.prefix}/stats.json" if mask_name is None
                else f"{self.prefix}/stats.{mask_name}.json")

    # ── flight-line parts ──────────────────────────────────────────────────
    def write_parts(self):
        parts = {}
        xyz = self.xyz
        # xyz.line_id_column handles all column-name variants; only split when
        # there is a line column and more than one sounding.
        if xyz.line_id_column and len(xyz.flightlines) > 1:
            print(f"Splitting {len(xyz.flightlines)} soundings into flight lines "
                  f"using column '{xyz.line_id_column}'...")
            for fline, line_xyz in xyz.split_by_line().items():
                fline_str = slugify.slugify(str(fline), separator="_")
                parts[fline_str] = {"files": self.write_part(fline_str, line_xyz)}
        return parts

    def write_part(self, fline_str, line_xyz):
        d = f"{self.prefix}/parts"
        self.write_msgpack(line_xyz, f"{d}/{fline_str}.msgpack")
        self.write_geojson(line_xyz, f"{d}/{fline_str}.geojson")
        self.write_xyz(line_xyz, f"{d}/{fline_str}.xyz")

        files = {
            "application/x-aarhusxyz-msgpack": f"{d}/{fline_str}.msgpack",
            "application/geo+json": f"{d}/{fline_str}.geojson",
            "text/x-aarhusxyz": f"{d}/{fline_str}.xyz",
        }

        def stats_url_for(mask_name):
            return (f"{d}/{fline_str}.stats.json" if mask_name is None
                    else f"{d}/{fline_str}.{mask_name}.stats.json")
        files.update(self.write_stats(line_xyz, stats_url_for))

        files.update(self.write_vtk_glb(
            line_xyz, f"{d}/{fline_str}.vtk", f"{d}/{fline_str}.glb",
            label=f" for part {fline_str}"))
        return files

    # ── index ────────────────────────────────────────────────────────────────
    def write_info(self, files, parts):
        info = {
            "id": self.dataset_id,
            "mime_type": "application/x-aarhusxyz-msgpack",
            "dataset_name": self.dataset_name,
            "files": files,
            "parts": parts,
        }
        url = f"{self.prefix}/info.json"
        print(f"Writing dataset info to: {url}")
        with self._open(url) as f:
            json.dump(info, f, indent=2)


def write_dataset(xyz, gex, dataset_name, process_id, process_version,
                  storage_base, storage_kwargs, extra_files=None):
    """Write a dataset to storage with all supported formats and flight line
    parts. Thin wrapper over DatasetWriter; returns the dataset id (UUID str).

    The xyz object must be normalized before calling this function.
    """
    return DatasetWriter(
        xyz, gex, dataset_name, process_id, process_version,
        storage_base, storage_kwargs, extra_files).write()
