# WearTest ATE

WearTest ATE is a desktop manufacturing-test application for wearable health and fitness devices such as fitness trackers, smart bands, and continuous physiological-monitoring products. It is designed to demonstrate how a test engineer can move from raw acquired sensor data to device acceptance, production analytics, tester-health checks, measurement-system analysis, and cycle-time optimization.

The project can run entirely from simulation, but it also accepts external acquisition data. CSV and NI TDMS/LabVIEW data can be normalized into the same internal measurement format before they reach the test engine. New source formats can be added through a small adapter interface without rewriting the core test logic.

All engineering limits, station behavior, reference values, GR&R settings, and cycle-time assumptions in this repository are configurable project assumptions. They are not production specifications or factory data from any specific manufacturer.

## What the project demonstrates

WearTest ATE covers the main pieces of a small automated test platform:

- End-of-line PASS/FAIL testing for power, IMU, optical PPG, skin temperature, and optional ECG paths.
- A single standardized JSONL measurement format used by simulation and imported acquisition data.
- CSV and NI TDMS/LabVIEW ingestion with channel mapping and unit conversion.
- CRC32 integrity checking, record sequencing, de-duplication identifiers, and local recovery spooling.
- User-editable engineering limits and product profiles through INI files.
- SQLite traceability for device tests, tester-health checks, GR&R studies, and cycle-time studies.
- Production metrics including true first-pass yield, failure Pareto, rolling FPY, station performance, and recent test history.
- Golden-unit testing to distinguish a product problem from a drifting test station.
- Crossed Gage R&R analysis with repeatability, reproducibility, part-to-part variation, %GR&R, ndc, and station bias.
- Cycle-time optimization that rejects faster sequences when simulated defect coverage degrades.
- A desktop GUI built for engineering use rather than a notebook or browser workflow.

## System flow

```text
Acquisition source
    |
    |-- Simulated wearable
    |-- CSV export
    |-- NI TDMS / LabVIEW
    |-- Custom adapter
    |
    v
Source adapter and unit conversion
    |
    v
Standardized JSONL measurement records
    |
    |-- CRC32 integrity check
    |-- Record ordering check
    |-- Source metadata
    |-- Device / station / session traceability
    |
    v
Reliable local transport
    |
    |-- Primary destination
    |-- Recovery spool if primary write fails
    |
    v
Manufacturing test engine
    |
    |-- Device PASS / FAIL
    |-- Failure codes
    |-- Golden-unit tester health
    |-- GR&R
    |-- Cycle-time validation
    |
    v
SQLite traceability and production dashboard
```

## Repository structure

```text
WearTest-ATE/
|
|-- run_app.py
|-- requirements.txt
|-- README.md
|-- .gitignore
|
|-- config/
|   |-- test_specs.ini
|   |-- products.ini
|   |-- golden_units.ini
|   |-- grr_study.ini
|   `-- cycle_time.ini
|
|-- examples/
|   |-- external_device_example.csv
|   `-- external_device_mapping.json
|
|-- weartest/
|   |-- app.py
|   |-- data.py
|   |-- adapters.py
|   |-- simulator.py
|   |-- test_engine.py
|   |-- storage.py
|   |-- grr.py
|   `-- cycle_time.py
|
|-- user_adapters/
|   `-- _example_adapter.py
|
`-- tests/
    `-- test_project.py
```

## Quick start with Anaconda and Spyder

WearTest uses Python 3.10 or newer syntax. An existing Anaconda environment is fine.

From Anaconda Prompt:

```bash
conda activate YOUR_ENVIRONMENT
cd PATH_TO/WearTest-ATE
python -m pip install -r requirements.txt
```

To confirm that Spyder is using the same environment, run this in the Spyder console:

```python
import sys
print(sys.executable)
```

Open `run_app.py` in Spyder and run it with F5. The launcher adds the project root to the Python path automatically, so the application does not depend on Spyder using a particular working directory.

The individual `weartest/*.py` modules also use absolute imports and a project-root bootstrap, which avoids the common `attempted relative import with no known parent package` error when files are run directly from Spyder.

## Main GUI workspaces

### Run Device Test

Runs the complete acceptance sequence for one product profile. The current profiles are defined in `config/products.ini` and can be extended without editing the GUI code.

The user can run one selected product or run all configured product profiles in one batch. Each profile receives its own traceable session.

### External Data

Imports data collected by another acquisition system and runs it through the same test engine as simulated data.

The included CSV example intentionally uses non-canonical units:

- battery voltage in mV
- acceleration in m/s^2
- angular rate in rad/s
- ECG impedance in ohm

The mapping file converts these into the engineering units expected by the test engine.

### Production

The production dashboard reads directly from the SQLite traceability database and shows:

- unique devices tested
- true first-pass yield based only on the first attempt for each DUT
- first-pass failures
- total test sessions, including retests
- failure Pareto
- rolling 20-device FPY
- station-level performance
- latest tester-health state
- recent test sessions

The dashboard is scrollable and can be refreshed manually. Refreshing re-queries SQLite and redraws the KPI cards, tables, Pareto chart, and FPY trend.

### Data Integrity

Shows whether the most recent acquisition passed the standardized-record integrity checks. WearTest uses CRC32 to detect accidental record corruption during transport or storage.

CRC32 is an integrity check, not a cryptographic security mechanism. It detects accidental changes but is not intended to authenticate a sender or defend against deliberate tampering.

### Test Limits

Displays the active engineering limits from `config/test_specs.ini`. The file can be edited with a normal text editor and reloaded from the GUI without changing Python code.

### Tester Health

Runs a known-good golden reference through one selected station or all configured stations. The result evaluates the tester rather than the product.

For each metric, WearTest compares measured and reference values, calculates tester bias, and reports one of three states:

- Healthy
- Warning
- Needs attention

The current simulation includes gradual station drift so the tester-health logic can be demonstrated without physical ATE hardware.

### Measurement System

Runs a crossed Gage R&R study across multiple reference units, stations, and repeated trials.

The analysis reports:

- repeatability
- station-to-station reproducibility
- part-by-station contribution
- total GR&R
- %GR&R
- part-to-part variation
- total study variation
- number of distinct categories, ndc
- mean bias for every station

The default configuration uses 5 reference units, 3 stations, and 3 trials per station, for 45 measurements.

### Cycle Time

Compares a baseline test sequence with faster candidate sequences. Candidate waveform windows are actually shortened before the same acceptance engine evaluates them.

A faster sequence is validated only if it meets all configured quality guardrails for defect-detection retention, escaped defects, and false-reject change.

The throughput value is an idealized single-station estimate:

```text
units per hour = 3600 / estimated cycle time
```

It does not include loading, unloading, operator time, maintenance, downtime, or line balancing.

### Simulation

Provides controlled fault injection and demo-batch generation for development without hardware. Batch generation is incremental and non-blocking, with visible progress, file counts, current stage, and a Cancel action.

## Standardized measurement records

After ingestion, every scalar or waveform becomes a `MeasurementRecord`. Important fields include:

```text
device_id
station_id
session_id
sequence
measurement
unit
values
timestamp_utc
sample_rate_hz
test_step
source_format
source_name
quality_flags
record_id
checksum_crc32
```

This boundary is what lets CSV, TDMS, simulation, and future data sources share the same downstream test engine.

## External acquisition example

The repository includes:

```text
examples/external_device_example.csv
examples/external_device_mapping.json
```

The mapping file describes what each source channel means and how its unit should be converted. For example:

```json
"accel_x_mps2": {
  "measurement": "accel_x",
  "source_unit": "m/s^2",
  "target_unit": "g"
}
```

The GUI uses the mapping to normalize the file and then evaluates it using the same acceptance logic used for simulated devices.

## Adding another acquisition format

`weartest/adapters.py` defines the `DataAdapter` interface. A custom converter only needs to turn the source data into a list of standardized `MeasurementRecord` objects.

A starting template is provided at:

```text
user_adapters/_example_adapter.py
```

Copy the file, remove the leading underscore, give the adapter a unique format name and file extension, and implement its `convert()` method. WearTest discovers user adapters at startup.

The manufacturing test engine does not need to be changed for every new file format.

## Configuration files

### `config/test_specs.ini`

Product acceptance limits for battery, accelerometer, gyroscope, optical sensing, temperature, and ECG impedance.

### `config/products.ini`

Defines available product configurations and whether ECG is required.

### `config/golden_units.ini`

Contains golden-unit reference values, tester-bias limits, warning thresholds, and simulated station drift.

### `config/grr_study.ini`

Controls the default GR&R metric, number of reference units, trials, decision bands, and repeatability assumptions.

### `config/cycle_time.ini`

Defines baseline and candidate test sequences, timing assumptions, validation population, and quality guardrails.

## Example simulated results

These numbers are produced from the current default configuration and deterministic simulation seeds. They are demonstration results only.

### Golden-unit tester health

The intentionally stable station remains Healthy during repeated checks. Under the default simulated drift model, the intentionally drifting station progresses from Healthy to Warning and eventually Needs attention as its accelerometer bias exceeds the configured tester limit.

### GR&R

For the default Accelerometer X study with 5 reference units, 3 stations, and 3 trials per station:

```text
Measurements                  45
% GR&R                       19.3%
Repeatability sigma        0.00265 g
Reproducibility sigma      0.00569 g
Part-to-part sigma         0.03193 g
ndc                              7
Assessment                    Review
```

The result is intentionally not perfect. It gives the application measurable repeatability and station-to-station variation to diagnose.

### Cycle-time validation

With the current default timing and validation assumptions:

```text
Baseline sequential       23.3 s   about 155 units/hour
Parallel acquisition      17.3 s   validated
Balanced                  12.3 s   validated
Aggressive                 7.3 s   validated
Too short                  6.3 s   rejected
```

The Aggressive sequence retains 100% simulated defect detection in the configured validation population and reduces estimated cycle time by about 68.7% relative to the baseline.

The still-faster Too short sequence is rejected because simulated defect-detection retention falls to about 85.7%, with a 14.3% escaped-defect rate. The optimizer therefore does not simply select the shortest sequence.

## Running the tests

From the project root:

```bash
python -m pytest -q
```

The test suite covers:

- CRC corruption detection
- unit conversion
- normal and faulty DUT acceptance
- external CSV import
- reliable fallback storage
- TDMS import when `nptdms` is available
- true first-pass yield and retest behavior
- rolling FPY
- golden-unit drift detection
- configurable product profiles
- crossed GR&R calculations and persistence
- cycle-time optimization and coverage loss

## Runtime data

WearTest creates a local `runtime/` directory when the application runs. It contains the SQLite traceability database, normalized session files, and transport/recovery data.

Runtime outputs are ignored by Git so local test history does not accidentally become part of the repository.

## Design choices

A few choices are deliberate:

- **JSONL instead of a proprietary binary interchange format:** easy to inspect, stream, validate, and convert from common acquisition tools. A high-throughput production system could later replace the transport encoding without changing the measurement contract.
- **SQLite for the portfolio application:** zero server setup and enough structure to demonstrate traceability, retests, station history, GR&R, and optimization studies.
- **INI configuration:** engineers can change limits and study settings without editing Python code.
- **Separate adapters and test logic:** source-file details stay outside the manufacturing acceptance engine.
- **UTC storage with local-time display:** traceability timestamps remain unambiguous while the GUI stays readable for the operator.
- **Golden-unit checks separate from DUT acceptance:** a known-good reference failure points toward the tester rather than automatically blaming the product.

## Project boundaries

This repository is a manufacturing-test software demonstration, not a medical-device validation package or a production-ready factory release.

- Sensor measurements are simulated unless the user imports external acquisition data.
- Acceptance limits and station drift are configurable project assumptions.
- GR&R reference populations are simulated.
- Cycle-time values are modeled rather than measured on a real production line.
- CRC32 protects against accidental corruption, not malicious alteration.
- Hardware communication, MES integration, access control, calibration certificates, and regulated validation would require additional work for a deployed system.

## Technology

- Python
- Tkinter / ttk
- NumPy
- Pandas
- Matplotlib
- SQLite
- npTDMS
- pytest

