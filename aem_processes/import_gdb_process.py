"""Import process for Geosoft GDB (database) files.

Reads an airborne TEM survey stored in a Geosoft ``.gdb`` database using the
clean-room ``pygdb`` reader (https://github.com/jcapriot/pygdb) and pairs it
with a GEX system description to produce a normalized XYZ dataset ready for
processing and inversion.

The GDB provides the per-sounding data (locations, altitudes, dB/dt gates); the
GEX provides the system geometry, waveform and gate times. Gate times are read
from the GEX and stored in ``model_info['gate times for channel N']`` where the
inversion expects them. Each GEX channel is mapped to its GDB data column by
receiver polarization (e.g. ``dBdt_Z`` -> the Z channel).
"""

import numpy as np
import pandas as pd
import libaarhusxyz
from .utils import localize_urls
from .dataset_utils import write_dataset
from .import_process import conform_to_simpeg_convention

# GDB channels are read with fallbacks so slightly different column namings
# still import. The first channel that exists on the line is used.
LINE_CANDIDATES = ["Line", "Line_no", "LineNo"]
EASTING_CANDIDATES = ["Easting", "UTMX", "X"]
NORTHING_CANDIDATES = ["Northing", "UTMY", "Y"]
TOPO_CANDIDATES = ["Ground_Elevation", "DEM", "Topography", "GPS_Height"]
ALT_CANDIDATES = ["Tx_Loop_Clearance", "Laser_Altimeter_Loop", "Rx_Clearance"]
PITCH_CANDIDATES = ["Tx_Loop_Pitch", "TxPitch"]
ROLL_CANDIDATES = ["Tx_Loop_Roll", "TxRoll"]

# Values at or below this are Geosoft dummy/null markers.
GDB_NULL_THRESHOLD = -1e30


class GeosoftGDBImporter:
    """Import AEM data from a Geosoft GDB database and a GEX system file."""

    @classmethod
    def schema(cls):
        """Return JSON Schema for import parameters."""
        return {
            "type": "object",
            "title": "Import Geosoft GDB",
            "description": "Import airborne TEM data from a Geosoft .gdb database with a GEX "
                           "system description. The GDB supplies sounding locations, altitudes "
                           "and dB/dt gates; the GEX supplies the waveform, gate times and "
                           "receiver geometry. Produces an XYZ dataset ready for processing and "
                           "inversion.",
            "properties": {
                "gdbfile": {
                    "type": "string",
                    "format": "uri",
                    "x-format": "upload",
                    "x-url-media-type": "application/x-geosoft-gdb",
                    "title": "GDB Database File",
                    "description": "Geosoft database file (.gdb)",
                    "pattern": "\\.gdb(\\.gz|\\.bz2|\\.zip)?$"
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
                "dbdt_channel_template": {
                    "type": "string",
                    "title": "dB/dt Channel Template",
                    "description": "GDB array-channel name per GEX receiver, with "
                                   "'{polarization}' replaced by the channel's coil orientation "
                                   "(X/Y/Z). Use e.g. 'dBdt_{polarization}_F' to import the "
                                   "filtered variant instead.",
                    "default": "dBdt_{polarization}"
                },
                "components": {
                    "type": "string",
                    "title": "Components (Optional)",
                    "description": "Comma-separated receiver polarizations to import (e.g. 'Z' or "
                                   "'Z,X'), matched against each GEX channel's "
                                   "ReceiverPolarizationXYZ. Leave empty to import every channel. "
                                   "For 1D inversion import only the vertical (Z) component; the "
                                   "horizontal (X) response of a layered earth is ~0 and is not "
                                   "1D-invertible.",
                    "default": ""
                },
                "lines": {
                    "type": "string",
                    "title": "Lines (Optional)",
                    "description": "Comma-separated list of line names to import (e.g. "
                                   "'L100,L200'). Leave empty to import all lines.",
                    "default": ""
                },
                "scalefactor": {
                    "type": "number",
                    "title": "Scale Factor",
                    "description": "Multiplier converting the stored dB/dt values to volts "
                                   "(V/(A*m^2)). Set according to the delivery units of the GDB.",
                    "default": 1e-12
                },
                "tx_normalized": {
                    "type": "boolean",
                    "title": "Transmitter-moment normalized",
                    "description": "The stored dB/dt is already divided by the transmitter moment "
                                   "(per unit dipole moment). If false, the importer divides each "
                                   "channel by its nominal dipole moment from the GEX.",
                    "default": True
                },
                "rx_normalized": {
                    "type": "boolean",
                    "title": "Receiver-area normalized",
                    "description": "The stored dB/dt already has the receiver area x turns divided out. "
                                   "Recorded in the dataset's metadata; it changes neither the data nor "
                                   "the GEX GateFactor, which is the calibration factor. False is refused: "
                                   "nothing in the processing chain divides by receiver area yet.",
                    "default": True
                },
                "projection": {
                    "type": "integer",
                    "title": "EPSG Projection Code",
                    "description": "EPSG code for the projection and chart datum of the Easting/"
                                   "Northing sounding locations",
                    "format": "x-epsg",
                    "minimum": 1
                }
            },
            "required": ["gdbfile", "gexfile", "scalefactor", "projection"]
        }

    @classmethod
    def run(cls, storage_context=None, **kwargs):
        """Import GDB data and write output dataset.

        Args:
            storage_context: Dict with process_id, version, storage_base, storage_kwargs
            **kwargs: Process parameters from schema

        Returns:
            Dict with status and outputs
        """
        # Imported here (not at module scope) so the base image build and schema
        # extraction do not require pygdb, which is installed per-environment.
        from pygdb import GDB

        print("Running Geosoft GDB import...")
        print(f"Parameters: {kwargs}")

        if not storage_context:
            raise ValueError("storage_context is required")

        process_id = storage_context['process_id']
        process_version = storage_context['version']
        storage_base = storage_context['storage_base']
        storage_kwargs = storage_context['storage_kwargs']

        gdbfile = kwargs.get("gdbfile")
        gexfile = kwargs.get("gexfile")
        scalefactor = kwargs.get("scalefactor", 1e-12)
        tx_normalized = kwargs.get("tx_normalized", True)
        rx_normalized = kwargs.get("rx_normalized", True)
        projection = kwargs.get("projection")
        channel_template = kwargs.get("dbdt_channel_template") or "dBdt_{polarization}"
        lines_filter = [s.strip() for s in (kwargs.get("lines") or "").split(",") if s.strip()]
        components = [c.strip().upper() for c in (kwargs.get("components") or "").split(",") if c.strip()]

        assert gdbfile is not None, "Missing GDB file"
        assert gexfile is not None, "Missing GEX file"
        assert isinstance(projection, int) and projection > 0, \
            "Invalid projection, please provide a valid EPSG projection code"
        assert isinstance(scalefactor, (int, float)) and scalefactor != 0, \
            "Invalid scalefactor, please provide a valid scalefactor"

        with localize_urls({"gdbfile": gdbfile, "gexfile": gexfile}, storage_kwargs) as localized_files:
            print("Loading GEX system description...")
            gex = libaarhusxyz.GEX(localized_files["gexfile"])

            # Map each GEX receiver channel to its polarization and gate times.
            gd = gex.gex_dict
            n_channels = sum(1 for key in gd if key.startswith("Channel"))
            assert n_channels > 0, "GEX contains no [ChannelN] sections"
            gate_times = np.asarray(gd["General"]["GateTime"])
            # GateTimeNN columns are (center, open, close); the center is the gate time.
            gate_time_centers = gate_times[:, 0] if gate_times.ndim == 2 else gate_times

            channel_polarization = {}  # channel number -> 'X'/'Y'/'Z'
            for channel in range(1, n_channels + 1):
                pol = str(gd[f"Channel{channel}"]["ReceiverPolarizationXYZ"]).strip().upper()
                channel_polarization[channel] = pol
            print(f"GEX channels: {channel_polarization}, {len(gate_time_centers)} gates")

            if components:
                channel_polarization = {ch: pol for ch, pol in channel_polarization.items()
                                        if pol in components}
                assert channel_polarization, \
                    f"No GEX channels match requested components {components}"
                print(f"Filtered to components {components}: channels {channel_polarization}")

            print("Opening GDB database...")
            gdb = GDB(localized_files["gdbfile"])

            line_names = list(gdb.line_names)
            if lines_filter:
                missing = [ln for ln in lines_filter if ln not in line_names]
                assert not missing, f"Requested lines not in GDB: {missing}. Available: {line_names}"
                line_names = [ln for ln in line_names if ln in lines_filter]
            assert line_names, "No lines to import"
            print(f"Importing {len(line_names)} line(s): {line_names}")

            per_line = [
                cls._read_line(gdb, ln, channel_polarization, channel_template)
                for ln in line_names
            ]
            flightlines = pd.concat([fl for fl, _ in per_line], ignore_index=True)
            layer_data = {}
            for _, ld in per_line:
                for key, df in ld.items():
                    layer_data.setdefault(key, []).append(df)
            layer_data = {
                key: pd.concat(dfs, ignore_index=True) for key, dfs in layer_data.items()
            }
            assert layer_data, "No dB/dt channels with data found in the GDB"

            model_info = {
                "scalefactor": float(scalefactor),
                "projection": int(projection),
                "source": "Geosoft GDB",
            }
            for channel in range(1, n_channels + 1):
                model_info[f"gate times for channel {channel}"] = list(gate_time_centers)

            print(f"Building XYZ: {len(flightlines)} soundings, "
                  f"layer_data keys {list(layer_data.keys())}")
            xyz = libaarhusxyz.XYZ({
                "flightlines": flightlines,
                "layer_data": layer_data,
                "model_info": model_info,
            })
            xyz.normalize(naming_standard="alc")

            assert "projection" in xyz.model_info
            assert "scalefactor" in xyz.model_info

            # Land the data in the SimPEG static_instrument convention, as the
            # XYZ/GEX importer does: per unit transmitter moment, the receiver
            # normalization recorded, the GEX calibration applied at read as
            # delivered.
            channels = sorted(ch for ch in channel_polarization
                              if f"Gate_Ch{ch:02d}" in xyz.layer_data)
            conform_to_simpeg_convention(
                xyz, gex, channels,
                tx_normalized=tx_normalized, rx_normalized=rx_normalized,
                scalefactor=float(scalefactor))

            print("Writing imported dataset...")
            dataset_id = write_dataset(
                xyz, gex, "imported_data",
                process_id, process_version, storage_base, storage_kwargs
            )

            outputs = {
                'imported_data': f"{storage_base}/processes/{process_id}/{process_version}/datasets/{dataset_id}/root.msgpack"
            }
            print("GDB import complete")
            return {"status": "success", "outputs": outputs}

    @classmethod
    def _read_line(cls, gdb, line, channel_polarization, channel_template):
        """Read one GDB line into (flightlines DataFrame, layer_data dict).

        Flightline columns use ALC naming so ``normalize(naming_standard='alc')``
        is a no-op rename; layer_data uses ``Gate_ChNN``/``InUse_ChNN`` per GEX
        channel number, mapping each channel to its polarization's GDB column.
        """
        available = set(gdb.channels_on_line(line))

        def read_scalar(candidates, required=True):
            for name in candidates:
                if name in available:
                    return np.asarray(gdb.read(line, name)).astype(float).ravel()
            if required:
                raise KeyError(
                    f"Line {line}: none of {candidates} present. Available: {sorted(available)}")
            return None

        line_id = read_scalar(LINE_CANDIDATES).astype(np.int64)
        flightlines = pd.DataFrame({
            "Line": line_id,
            "UTMX": read_scalar(EASTING_CANDIDATES),
            "UTMY": read_scalar(NORTHING_CANDIDATES),
            "Topography": read_scalar(TOPO_CANDIDATES),
            "TxAltitude": read_scalar(ALT_CANDIDATES),
        })
        pitch = read_scalar(PITCH_CANDIDATES, required=False)
        if pitch is not None:
            flightlines["TxPitch"] = pitch
        roll = read_scalar(ROLL_CANDIDATES, required=False)
        if roll is not None:
            flightlines["TxRoll"] = roll

        n_rows = len(flightlines)
        layer_data = {}
        for channel, pol in channel_polarization.items():
            col = channel_template.format(polarization=pol)
            if col not in available:
                print(f"  Line {line}: dB/dt column '{col}' (channel {channel}, {pol}) absent, skipping")
                continue
            arr = np.asarray(gdb.read(line, col)).astype(float)
            if arr.ndim != 2 or arr.shape[0] != n_rows:
                print(f"  Line {line}: '{col}' has unexpected shape {arr.shape}, skipping")
                continue
            null_mask = arr <= GDB_NULL_THRESHOLD
            if null_mask.all():
                print(f"  Line {line}: '{col}' (channel {channel}, {pol}) is all null, skipping")
                continue
            arr[null_mask] = np.nan
            suffix = f"0{channel}"[-2:]
            layer_data[f"Gate_Ch{suffix}"] = pd.DataFrame(arr)
            layer_data[f"InUse_Ch{suffix}"] = pd.DataFrame((~null_mask).astype(np.int8))
        return flightlines, layer_data
