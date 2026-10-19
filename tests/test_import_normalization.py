"""Imported AEM data declare their normalization, so ymerflow-processing reads it from the file."""

from libaarhusxyz.export import msgpack as xyz_msgpack

from aem_processes.import_msgpack_process import MsgpackImporter
from aem_processes.import_process import LibaarhusXYZImporter
from test_yf_processing_process import dataset_dir, storage, write_delivery


def import_delivery(tmp_path):
    xyz_path, gex_path = write_delivery(tmp_path, (("LM", 9.0),))
    result = LibaarhusXYZImporter.run(
        storage_context=storage(tmp_path, "import"),
        xyzfile=f"file://{xyz_path}", gexfile=f"file://{gex_path}",
        scalefactor=1.0, projection=32613)
    return result["outputs"]["imported_data"]


def test_an_imported_delivery_loads_as_rx_tx(tmp_path):
    """Read back from the stored text forms, as the processing process types do."""
    from ymerflow_processing.dataset import load

    directory = dataset_dir(import_delivery(tmp_path))
    assert load(f"{directory}/root.xyz", f"{directory}/root.gex").normalization == "rx_tx"


def reimport(tmp_path, url, normalization):
    """Pass an imported dataset back through the msgpack importer, with the
    given declaration (None removes it)."""
    xyz, gex = xyz_msgpack.load(url.split("file://", 1)[1], return_gex=True)
    xyz.model_info.pop("normalization", None)
    if normalization:
        xyz.model_info["normalization"] = normalization
    container = tmp_path / "container.msgpack"
    with open(container, "wb") as f:
        xyz.to_msgpack(f, gex=gex)
    result = MsgpackImporter.run(storage_context=storage(tmp_path, "reimport"),
                                 msgpack_file=f"file://{container}")
    output, _ = xyz_msgpack.load(result["outputs"]["imported_data"].split("file://", 1)[1],
                                 return_gex=True)
    return output.model_info.get("normalization")


def test_an_undeclared_container_is_declared_rx_tx(tmp_path):
    assert reimport(tmp_path, import_delivery(tmp_path), None) == "rx_tx"


def test_a_container_that_declares_its_convention_keeps_it(tmp_path):
    assert reimport(tmp_path, import_delivery(tmp_path), "rx") == "rx"
