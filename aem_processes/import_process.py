"""Import process for geophysics data."""

import re
import numpy as np
import pandas as pd
import libaarhusxyz
from .utils import localize_urls, normalize_column_case
from .dataset_utils import write_dataset


def conform_to_simpeg_convention(xyz, gex, channels, tx_normalized,
                                 rx_normalized, scalefactor):
    """Reconcile an uploaded delivery to the SimPEG static_instrument format.

    The SimPEG format the whole inversion stack reads is: ``dbdt_chNgt`` per unit
    transmitter moment (tx divided out of the arrays), with receiver geometry NOT
    in the arrays — the GEX ``GateFactor`` and the model_info ``scalefactor`` are
    applied at read, never baked in. The upload may already be tx-normalized
    (SkyTEM / Aarhus deliveries are) and/or rx-normalized; each axis is declared
    by its own boolean and reconciled independently here.

    Transmitter axis
        If ``tx_normalized`` is false the arrays still carry the transmitter
        moment, so each channel's ``dbdt`` is divided by its nominal
        ``ApproxDipoleMoment`` (``nturns x TxLoopArea x TxApproximateCurrent``).
        A channel whose moment the GEX does not give is a hard error: the
        convention cannot be produced. The relative ``dbdt_std`` fraction is a
        ratio and so is left alone.

    Receiver axis
        If ``rx_normalized`` is true the receiver geometry is already in the
        arrays, so the channel's GEX ``GateFactor`` is set to ``1.0`` to stop
        SimPEG applying it a second time at read. Otherwise the delivered
        ``GateFactor`` is kept.

    After reconciling, assert ``mean(-dbdt x scalefactor x GateFactor) < 1e-3``
    per channel, mirroring SimPEG's ``base.py`` validation. With no convention
    field left to cross-check against, this plausibility check is the only
    automatic guard, and SimPEG would reject the same data at inversion anyway —
    so it is a hard error that blocks the import, raised here where the GEX is
    present and the inputs are still fixable.
    """
    for channel in channels:
        block = gex.gex_dict.setdefault("Channel%d" % channel, {})
        data_name = xyz.layer_data_data_name(channel)

        if not tx_normalized:
            moment = block.get("ApproxDipoleMoment")
            if not moment:
                raise ValueError(
                    "Channel %d: the GEX gives no ApproxDipoleMoment, so the "
                    "transmitter moment cannot be divided out to produce the "
                    "SimPEG per-unit-moment convention." % channel)
            xyz.layer_data[data_name] = xyz.layer_data[data_name] / float(moment)

        if rx_normalized:
            block["GateFactor"] = 1.0

        gate_factor = block.get("GateFactor", 1.0) or 1.0
        dbdt = -xyz.layer_data[data_name].values.flatten() * scalefactor * gate_factor
        mean = np.nanmean(dbdt)
        assert mean < 1e-3, (
            "Channel %d: mean(-dbdt x scalefactor x GateFactor) = %g is not "
            "< 1e-3, so the data is not in V/m^2 after read-time scaling. The "
            "scalefactor, or the tx_normalized / rx_normalized declaration, is "
            "probably wrong — SimPEG would reject this data at inversion."
            % (channel, mean))


class LibaarhusXYZImporter:
    """Import SkyTEM data from XYZ/GEX files (Aarhus Workbench format)."""

    @classmethod
    def schema(cls):
        """Return JSON Schema for import parameters."""
        return {
            "type": "object",
            "title": "Import SkyTEM (XYZ/GEX)",
            "description": "Import airborne TEM data from Aarhus Workbench XYZ files with a GEX "
                           "system description (and optional ALC column mapping). Produces an XYZ "
                           "dataset ready for processing and inversion.",
            "properties": {
                "xyzfile": {
                    "type": "string",
                    "format": "uri",
                    "x-format": "upload",
                    "x-url-media-type": "text/x-aarhusxyz",
                    "title": "XYZ Data File",
                    "description": "The data file (.xyz)",
                    "pattern": "\\.xyz(\\.gz|\\.bz2|\\.zip)?$"
                },
                "gexfile": {
                    "type": "string",
                    "format": "uri",
                    "x-format": "upload",
                    "x-url-media-type": "text/x-aarhusxyz-gex",
                    "title": "GEX System File",
                    "description": "System description / calibration file (.gex)",
                    "pattern": "\\.gex(\\.gz|\\.bz2|\\.zip)?$"
                },
                "alcfile": {
                    "type": "string",
                    "format": "uri",
                    "x-format": "upload",
                    "x-url-media-type": "text/x-aarhusxyz-alc",
                    "title": "ALC Allocation File (Optional)",
                    "description": "Column name mapping file (.alc)",
                    "pattern": "\\.alc(\\.gz|\\.bz2|\\.zip)?$"
                },
                "scalefactor": {
                    "type": "number",
                    "title": "Scale Factor",
                    "description": "Data unit: 1 = volt, 1e-12 = picovolt",
                    "default": 1e-12
                },
                "tx_normalized": {
                    "type": "boolean",
                    "title": "Transmitter-moment normalized",
                    "description": "The uploaded dB/dt is already divided by the transmitter moment "
                                   "(per unit dipole moment). True for SkyTEM / Aarhus Workbench deliveries. "
                                   "If false, the importer divides each channel by its nominal dipole moment.",
                    "default": True
                },
                "rx_normalized": {
                    "type": "boolean",
                    "title": "Receiver-geometry normalized",
                    "description": "The uploaded dB/dt already has receiver coil area x turns applied (V/m^2). "
                                   "Usually false for SkyTEM deliveries, which carry receiver geometry as the "
                                   "GEX GateFactor applied at read. If true, the importer sets GateFactor = 1 so "
                                   "it is not applied twice.",
                    "default": False
                },
                "projection": {
                    "type": "integer",
                    "title": "EPSG Projection Code",
                    "description": "EPSG code for the projection and chart datum of sounding locations",
                    "format": "x-epsg",
                    "minimum": 1
                }
            },
            "required": ["xyzfile", "gexfile", "scalefactor", "projection"]
        }

    @classmethod
    def run(cls, storage_context=None, **kwargs):
        """Import data and write output datasets.

        Args:
            storage_context: Dict with process_id, storage_base, storage_kwargs
            **kwargs: Process parameters from schema (xyzfile, gexfile, alcfile, scalefactor, projection)

        Returns:
            Dict with status and outputs
        """
        print("Running import...")
        print(f"Parameters: {kwargs}")

        if not storage_context:
            raise ValueError("storage_context is required")

        process_id = storage_context['process_id']
        process_version = storage_context['version']
        storage_base = storage_context['storage_base']
        storage_kwargs = storage_context['storage_kwargs']

        # Extract parameters
        xyzfile = kwargs.get("xyzfile")
        gexfile = kwargs.get("gexfile")
        alcfile = kwargs.get("alcfile")
        scalefactor = kwargs.get("scalefactor", 1e-12)
        tx_normalized = kwargs.get("tx_normalized", True)
        rx_normalized = kwargs.get("rx_normalized", False)
        projection = kwargs.get("projection")

        # Validate required parameters
        assert xyzfile is not None, "Missing xyz file"
        assert gexfile is not None, "Missing gex file"
        assert isinstance(projection, int) and projection > 0, \
            "Invalid projection, please provide a valid EPSG projection code"
        assert isinstance(scalefactor, (int, float)) and scalefactor != 0, \
            "Invalid scalefactor, please provide a valid scalefactor"

        # Localize URLs to local files
        file_params = {
            "xyzfile": xyzfile,
            "gexfile": gexfile,
            "alcfile": alcfile
        }

        with localize_urls(file_params, storage_kwargs) as localized_files:
            print("Creating survey from files...")

            # TODO: if the uploaded file is a CSV (comma-separated, no
            # /- prefixed headers), auto-convert it to Aarhus XYZ format
            # before passing to libaarhusxyz. See pending_tools/aem_csv_to_xyz.py
            # (data_csv_to_xyz function) for the conversion logic. Detection:
            # check whether the first line of the file starts with '/' or
            # contains commas with no whitespace.

            # Load XYZ data
            xyz = libaarhusxyz.XYZ(
                localized_files["xyzfile"],
                alcfile=localized_files.get("alcfile")
            )

            # Set metadata
            xyz.model_info['scalefactor'] = float(scalefactor)
            xyz.model_info['projection'] = int(projection)
            normalize_column_case(xyz)
            xyz.normalize(naming_standard="alc")

            # Add InUse flags (all 1s) for any channel that has gate data but no InUse data
            channels_with_data = set()
            for key in xyz.layer_data.keys():
                m = re.match(r'^Gate_Ch(\d+)$', key) or re.match(r'^dbdt_ch(\d+)$', key, re.IGNORECASE)
                if m:
                    channels_with_data.add(int(m.group(1)))
            for channel in sorted(channels_with_data):
                if xyz.layer_data_inuse_name(channel) is None:
                    data_name = xyz.layer_data_data_name(channel)
                    gate_data = xyz.layer_data[data_name]
                    str_channel = f"0{channel}"[-2:]
                    inuse_key = f"InUse_Ch{str_channel}"
                    xyz.layer_data[inuse_key] = pd.DataFrame(
                        np.ones(gate_data.shape, dtype=np.int8),
                        index=gate_data.index,
                        columns=gate_data.columns
                    )
                    print(f"Added InUse flags (all 1s) for channel {channel} ({inuse_key})")

            assert "projection" in xyz.model_info
            assert "scalefactor" in xyz.model_info

            # Load GEX (system description)
            gex = libaarhusxyz.GEX(localized_files["gexfile"])

            # Land the data in exactly the SimPEG static_instrument convention:
            # dbdt per unit transmitter moment (tx divided out of the arrays),
            # receiver geometry NOT in the arrays but carried by the GEX
            # GateFactor and applied at read, scalefactor converting raw -> V/m^2
            # also at read. The upload varies on two independent axes, declared by
            # tx_normalized / rx_normalized; here we reconcile each to the target.
            # There is no ymerflow `normalization` field any more — the data
            # simply IS the SimPEG convention, and scalefactor + GateFactor stay
            # metadata, never baked into the arrays. See
            # simpeg/.../static_instrument/data_format.md.
            conform_to_simpeg_convention(
                xyz, gex, sorted(channels_with_data),
                tx_normalized=tx_normalized, rx_normalized=rx_normalized,
                scalefactor=float(scalefactor))

            # Write dataset
            print("Writing imported dataset...")
            dataset_id = write_dataset(
                xyz,
                gex,
                "imported_data",
                process_id,
                process_version,
                storage_base,
                storage_kwargs
            )

            outputs = {
                'imported_data': f"{storage_base}/processes/{process_id}/{process_version}/datasets/{dataset_id}/root.msgpack"
            }

            print("Import complete")
            return {"status": "success", "outputs": outputs}
