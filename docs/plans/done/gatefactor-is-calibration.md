# GateFactor Is the Calibration Factor: Importers Keep It as Delivered

**Status:** Implemented.
**Repos touched:** `aem-processes` (the XYZ/GEX and GDB importers, the README, and `fixed-rx-tx-normalization.md`). SimPEG's `static_instrument/data_format.md` describes `GateFactor` the way `fixed-rx-tx-normalization.md` did; it is not changed here.
**Context:** `fixed-rx-tx-normalization.md` read the GEX `GateFactor` as receiver-geometry normalization and gave the XYZ/GEX importer an `rx_normalized` option that set it to 1. The GDB importer was left out of that change and still wrote the retired `normalization` field.

## Finding

Ben, 2026-10-21: "GateFactor is the calibration factor; he doesn't know."

In the Aarhus GEX convention, `GateFactor` (per channel) is the system's calibration amplitude factor and `GateTimeShift` its calibration time shift. A calibration of the system sets both. The receiver area is already in the delivered dB/dt.

The code that reads the data already treats them that way:
- SimPEG multiplies `GateFactor` in at read (`MeasuredTEMXYZSystem._moment_data`, `dual.py`).
- libaarhusxyz adds `GateTimeShift` (and `MeaTimeDelay`) to the gate times (`GEX.gate_times`).

So the only thing that discarded a calibration was the importer: `rx_normalized` set `GateFactor` to 1.

## Changes

1. **`conform_to_simpeg_convention` never writes `GateFactor`.** It reads it as the GEX gives it, for the plausibility assertion `mean(-dbdt × scalefactor × GateFactor) < 1e-3`, which is unchanged. Its `rx_normalized` argument is gone.
2. **The XYZ/GEX importer drops `rx_normalized` from its form.** A run that still passes `rx_normalized: true` fails with an error saying why. A version saved with it on would otherwise import differently from what it asked for, without saying so. `false` or absent is accepted.
3. **The GDB importer conforms as the XYZ/GEX importer does.** The `model_info["normalization"]` it wrote is removed, and it goes through `conform_to_simpeg_convention` with a `tx_normalized` option (default true). It now gets the moment division and the plausibility assertion too.
4. **Docs.** The README's "Amplitude convention on import" and `fixed-rx-tx-normalization.md` say that `GateFactor` is the calibration amplitude factor and `GateTimeShift` the calibration time shift, both applied at read, and that the receiver area is in the delivered data. The receiver-geometry wording and the `rx_normalized` description are removed.

## Tests

`tests/test_import_normalization.py`:
- An XYZ/GEX import keeps a GEX `GateFactor` of 0.97 and leaves the arrays as delivered.
- `rx_normalized: true` is refused.
- A GDB import writes no `normalization` field, keeps `GateFactor` 0.97 and leaves the arrays as delivered. pygdb is installed per environment, so its reader is replaced by an in-memory stand-in with the three members the importer uses.
- A GDB import with `tx_normalized: false` divides by the moment.

## Not in this change

- SimPEG's `static_instrument/data_format.md` calls `GateFactor` receiver-geometry normalization (statement 2, the dual-moment paragraph, and the summary table). The same correction applies there.
- ymerflow-processing's `filters/noise.py` labels `GateFactor` "receiver geometry" in one docstring. The arithmetic it does, `dbdt × scalefactor × GateFactor`, is right either way.
