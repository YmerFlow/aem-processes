"""Compound filter process type.

Loads an input XYZ dataset, optionally applies a sparse InUse diff
(libaarhusxyz msgpack format), and writes the result as a new dataset.
"""

import os
import contextlib
import tempfile
import fsspec

from aem_processes.dataset_utils import write_dataset


@contextlib.contextmanager
def _localize(url, storage_kwargs):
    """Download a remote file to a local temp path and yield the path."""
    if not url or "://" not in url:
        yield url
        return

    if url.startswith("file://"):
        yield url[len("file://"):]
        return

    _, ext = os.path.splitext(url)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    try:
        with fsspec.open(url, "rb", **storage_kwargs) as src:
            tmp.write(src.read())
        tmp.close()
        yield tmp.name
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


class compound_filter:
    """Apply a sparse InUse diff to an input XYZ dataset.

    Parameters
    ----------
    input   : storage URL of the input XYZ msgpack dataset (required)
    diff    : storage URL of a JSON diff file produced by the frontend editor (optional)
    output_name : name for the output dataset (default: "filtered")
    """

    @classmethod
    def schema(cls):
        return {
            "type": "object",
            "title": "Compound Filter",
            "description": "Apply an InUse diff produced by the flag editor to an XYZ dataset, "
                           "masking or unmasking soundings and gates. With no diff it passes the "
                           "input through unchanged.",
            "properties": {
                "input": {
                    "type": "string",
                    "format": "uri",
                    "x-format": "dataset",
                    "title": "Input Dataset",
                    "description": "Input XYZ dataset to filter",
                },
                "diff": {
                    "type": "string",
                    "format": "uri",
                    "title": "InUse Diff (optional)",
                    "description": "libaarhusxyz msgpack diff produced by the InUse editor; leave blank for pass-through",
                },
                "output_name": {
                    "type": "string",
                    "title": "Output Dataset Name",
                    "default": "filtered",
                    "description": "Name for the output dataset",
                },
            },
            "required": ["input"],
        }

    @classmethod
    def run(cls, storage_context=None, **kwargs):
        import libaarhusxyz
        import numpy as np

        process_id = storage_context["process_id"]
        process_version = storage_context["version"]
        storage_base = storage_context["storage_base"]
        storage_kwargs = storage_context["storage_kwargs"]

        input_url = kwargs["input"]
        diff_url = kwargs.get("diff") or None
        output_name = kwargs.get("output_name", "filtered")

        print(f"compound_filter: loading input from {input_url}")

        with _localize(input_url, storage_kwargs) as local_input:
            xyz, gex = libaarhusxyz.export.msgpack.load(local_input, True)

        xyz.normalize()
        print(f"Loaded {len(xyz.flightlines)} soundings")

        if diff_url:
            print(f"compound_filter: applying diff from {diff_url}")
            try:
                with _localize(diff_url, storage_kwargs) as local_diff:
                    diff_xyz, _ = libaarhusxyz.export.msgpack.load(local_diff, True)
                n_soundings = len(diff_xyz.flightlines)
                n_channels = len(diff_xyz.layer_data)
                print(f"Loaded diff: {n_soundings} sounding overrides across {n_channels} channel(s)")
                xyz = xyz.apply_diff(diff_xyz)
                print(f"Applied diff")
            except Exception as e:
                print(f"Warning: could not apply diff ({e}); continuing without it")

        dataset_id = write_dataset(
            xyz, gex, output_name,
            process_id, process_version,
            storage_base, storage_kwargs,
        )
        print(f"compound_filter: output dataset ID = {dataset_id}")
        return {"status": "success", "output_name": output_name, "dataset_id": dataset_id}
