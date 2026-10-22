# GateFactor Is the Calibration Factor: Importers Keep It as Delivered

**Status:** Implemented.
**Repos touched:** `aem-processes` (the XYZ/GEX and GDB importers, the README, and `fixed-rx-tx-normalization.md`). SimPEG's `static_instrument/data_format.md` describes `GateFactor` the way `fixed-rx-tx-normalization.md` did; it is not changed here.
**Context:** `fixed-rx-tx-normalization.md` read the GEX `GateFactor` as receiver-geometry normalization and gave the XYZ/GEX importer an `rx_normalized` option that set it to 1. The GDB importer was left out of that change and still wrote the retired `normalization` field.

## Finding

Ben, 2026-10-21: "GateFactor is the calibration factor; he doesn't know."

Ben, 2026-10-22: receiver normalization is a unit division, independent of calibration; `GateFactor` is the calibration factor and stays as delivered.

In the Aarhus GEX convention, `GateFactor` (per channel) is the system's calibration amplitude factor and `GateTimeShift` its calibration time shift. A calibration of the system sets both. The receiver area × turns is already divided out of the delivered dB/dt.

The code that reads the data already treats them that way:
- SimPEG multiplies `GateFactor` in at read (`MeasuredTEMXYZSystem._moment_data`, `dual.py`).
- libaarhusxyz adds `GateTimeShift` (and `MeaTimeDelay`) to the gate times (`GEX.gate_times`).

So the only thing that discarded a calibration was the importer: `rx_normalized` set `GateFactor` to 1.

## Changes

1. **`conform_to_simpeg_convention` never writes `GateFactor`.** It reads it as the GEX gives it, for the plausibility assertion `mean(-dbdt × scalefactor × GateFactor) < 1e-3`, which is unchanged.
2. **`rx_normalized` is recorded metadata, default true, in both importer forms.** It declares whether the delivered dB/dt already has the receiver area × turns divided out. `conform_to_simpeg_convention` stores it as `model_info["rx_normalized"] = "true"` and never reads, writes or resets `GateFactor`. `false` is refused with an error: nothing in the chain divides by receiver area yet, so the import cannot normalize such data.
   - It is stored as a string because the XYZ text form reads a boolean back as the string `"True"`, and a processed dataset is written from that text form. `"true"` reads back the same from the stored msgpack, the stored text form and a processed dataset.
3. **The GDB importer conforms as the XYZ/GEX importer does.** The `model_info["normalization"]` it wrote is removed, and it goes through `conform_to_simpeg_convention` with `tx_normalized` and `rx_normalized` options (both default true). It now gets the moment division, the recorded receiver normalization and the plausibility assertion too.
4. **Docs.** The README's "Amplitude convention on import" and `fixed-rx-tx-normalization.md` say that `GateFactor` is the calibration amplitude factor and `GateTimeShift` the calibration time shift, both applied at read, and that `rx_normalized` is recorded metadata. The receiver-geometry wording is removed.
5. **Test step names follow ymerflow-processing#30**, which renamed the synthetic-data pipeline's steps to `set_noise_floor`, `assign_uncertainty`, `add_noise`.

## Tests

`tests/test_import_normalization.py`:
- An `rx_normalized` XYZ/GEX import keeps a GEX `GateFactor` of 0.97 and leaves the arrays as delivered.
- `rx_normalized: false` is refused.
- The recorded `rx_normalized` reads back as `"true"` from the stored msgpack, the stored text form, and a dataset processed by `yf_process_tem`.
- A GDB import writes no `normalization` field, records `rx_normalized`, keeps `GateFactor` 0.97 and leaves the arrays as delivered. pygdb is installed per environment, so its reader is replaced by an in-memory stand-in with the three members the importer uses.
- A GDB import with `tx_normalized: false` divides by the moment.

## Not in this change

- SimPEG's `static_instrument/data_format.md` calls `GateFactor` receiver-geometry normalization (statement 2, the dual-moment paragraph, and the summary table). The same correction applies there.
