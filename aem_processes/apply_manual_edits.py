"""Apply Manual Edits process type.

Loads an input XYZ dataset, applies the in-use edits saved from a plot (sparse
libaarhusxyz msgpack diffs, one per save), and writes the result as a new
dataset.
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


class ApplyManualEdits:
    """Apply the in-use edits saved from a plot to an input XYZ dataset.

    Parameters
    ----------
    input   : storage URL of the input XYZ msgpack dataset (required)
    diff    : storage URLs of the msgpack diffs saved by the plot editor, one per save,
              oldest first (optional; a single URL is accepted too)
    output_name : name for the output dataset (default: "filtered")
    """

    @classmethod
    def schema(cls):
        return {
            "type": "object",
            "title": "Apply Manual Edits",
            "description": "Apply the in-use edits saved from a plot (gates and soundings turned "
                           "off or back on) to a dataset, without processing it. With no edits it "
                           "copies the input.",
            "properties": {
                "input": {
                    "type": "string",
                    "format": "uri",
                    "x-format": "dataset",
                    "title": "Input Dataset",
                    "description": "Input XYZ dataset to filter",
                },
                "diff": {
                    "type": "array",
                    "items": {"type": "string", "format": "uri"},
                    "title": "InUse Diffs (optional)",
                    "description": "libaarhusxyz msgpack diffs saved by the InUse editor, one per save, "
                                   "applied oldest first; leave empty for pass-through",
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
        # One entry per save, oldest first; a single URL from an older version is one save.
        diff = kwargs.get("diff") or []
        diff_urls = [diff] if isinstance(diff, str) else list(diff)
        output_name = kwargs.get("output_name", "filtered")

        print(f"apply_manual_edits: loading input from {input_url}")

        with _localize(input_url, storage_kwargs) as local_input:
            xyz, gex = libaarhusxyz.export.msgpack.load(local_input, True)

        # The dataset's own naming (ALC for every importer), not libaarhusxyz's
        # default: the plot editor writes its edits against InUse_Ch01, and the
        # output has to stay in the names the editor can open again.
        xyz.normalize(naming_standard=xyz.model_info.get("naming_standard") or "alc")
        print(f"Loaded {len(xyz.flightlines)} soundings")

        for diff_url in diff_urls:
            print(f"apply_manual_edits: applying diff from {diff_url}")
            with _localize(diff_url, storage_kwargs) as local_diff:
                diff_xyz, _ = libaarhusxyz.export.msgpack.load(local_diff, True)
            # A dropped edit is the worst outcome, so a mismatch fails the run.
            missing = sorted(set(diff_xyz.layer_data) - set(xyz.layer_data))
            if missing:
                raise KeyError(f"The edits in {diff_url} name {', '.join(missing)}, which this "
                               f"dataset does not have (it has {', '.join(sorted(xyz.layer_data))}).")
            n_soundings = len(diff_xyz.flightlines)
            n_channels = len(diff_xyz.layer_data)
            print(f"Loaded diff: {n_soundings} sounding overrides across {n_channels} channel(s)")
            xyz = xyz.apply_diff(diff_xyz)
            print(f"Applied diff")

        dataset_id = write_dataset(
            xyz, gex, output_name,
            process_id, process_version,
            storage_base, storage_kwargs,
        )
        print(f"apply_manual_edits: output dataset ID = {dataset_id}")
        return {"status": "success", "output_name": output_name, "dataset_id": dataset_id}
