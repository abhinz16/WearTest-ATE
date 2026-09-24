"""Generate deterministic, wearable-relevant measurements for software testing."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import sys
import uuid

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from weartest.data import MeasurementRecord
from weartest.grr import GrrMetricDefinition
from weartest.test_engine import GoldenUnitConfiguration


@dataclass(frozen=True)
class FaultProfile:
    """Select synthetic defects to inject into a simulated wearable device.

    Args:
        battery_low: Simulate an out-of-spec battery voltage.
        accel_bias: Add an excessive X-axis accelerometer offset.
        gyro_bias: Add an excessive Z-axis gyroscope bias.
        ppg_low_snr: Increase optical waveform noise.
        ppg_saturation: Force a block of optical samples into saturation.
        skin_temp_offset: Add an excessive temperature offset.
        ecg_high_impedance: Simulate a high-impedance ECG electrode path.
    """

    battery_low: bool = False
    accel_bias: bool = False
    gyro_bias: bool = False
    ppg_low_snr: bool = False
    ppg_saturation: bool = False
    skin_temp_offset: bool = False
    ecg_high_impedance: bool = False


class WearableSimulator:
    """Create bench-style data for a generic wearable sensor architecture.

    The model is intentionally generic. It represents power, motion, optical PPG,
    temperature, and optional ECG paths without claiming WHOOP's private firmware,
    manufacturing procedure, or proprietary acceptance limits.

    Args:
        rng_seed: Optional seed that makes generated data repeatable in tests.
    """

    def __init__(self, rng_seed: int | None = None) -> None:
        """Create a repeatable random-number generator for simulated acquisition.

        Args:
            rng_seed: Optional NumPy random seed.
        """

        self.rng = np.random.default_rng(rng_seed)

    def _record(
        self,
        *,
        seq: int,
        name: str,
        unit: str,
        values,
        fs: float | None,
        device_id: str,
        station_id: str,
        session_id: str,
        step: str,
    ) -> MeasurementRecord:
        """Build one canonical record from a simulated fixture measurement.

        Args:
            seq: Sequence number within the test session.
            name: Canonical measurement name.
            unit: Canonical engineering unit.
            values: Scalar or waveform data.
            fs: Sample rate in hertz for waveform data.
            device_id: Device-under-test identifier.
            station_id: Simulated test station identifier.
            session_id: Unique simulated test session identifier.
            step: Human-readable acquisition step.

        Returns:
            Canonical MeasurementRecord.
        """

        return MeasurementRecord.create(
            device_id=device_id,
            station_id=station_id,
            session_id=session_id,
            sequence=seq,
            measurement=name,
            unit=unit,
            values=values,
            sample_rate_hz=fs,
            test_step=step,
            source_format="simulator",
            source_name="WearableSimulator",
        )

    def acquire(
        self,
        device_id: str,
        station_id: str,
        faults: FaultProfile | None = None,
        include_ecg: bool = True,
    ) -> list[MeasurementRecord]:
        """Simulate one complete end-of-line wearable test acquisition.

        Args:
            device_id: Device-under-test identifier.
            station_id: Test station identifier.
            faults: Optional synthetic defect profile.
            include_ecg: Whether to include the optional ECG electrode-path check.

        Returns:
            Canonical records for one simulated device test session.
        """

        active_faults = faults or FaultProfile()
        session_id = str(uuid.uuid4())
        records: list[MeasurementRecord] = []
        sequence = 0

        battery = self.rng.normal(4.05, 0.025)
        if active_faults.battery_low:
            battery = self.rng.normal(3.72, 0.02)
        records.append(
            self._record(
                seq=sequence,
                name="battery_voltage",
                unit="V",
                values=float(battery),
                fs=None,
                device_id=device_id,
                station_id=station_id,
                session_id=session_id,
                step="Power and communication",
            )
        )
        sequence += 1

        # A stationary fixture expects X/Y near zero and Z near +1 g.
        imu_rate_hz = 100.0
        imu_samples = 500
        accel_bias = np.array([0.0, 0.0, 0.0])
        if active_faults.accel_bias:
            accel_bias[0] = 0.080
        accel_means = np.array([0.0, 0.0, 1.0]) + accel_bias

        for axis, mean in zip("xyz", accel_means):
            values = self.rng.normal(mean, 0.006, imu_samples)
            records.append(
                self._record(
                    seq=sequence,
                    name=f"accel_{axis}",
                    unit="g",
                    values=values,
                    fs=imu_rate_hz,
                    device_id=device_id,
                    station_id=station_id,
                    session_id=session_id,
                    step="Motion sensor static test",
                )
            )
            sequence += 1

        gyro_means = np.array([0.08, -0.06, 0.10])
        if active_faults.gyro_bias:
            gyro_means[2] = 1.20

        for axis, mean in zip("xyz", gyro_means):
            values = self.rng.normal(mean, 0.08, imu_samples)
            records.append(
                self._record(
                    seq=sequence,
                    name=f"gyro_{axis}",
                    unit="deg/s",
                    values=values,
                    fs=imu_rate_hz,
                    device_id=device_id,
                    station_id=station_id,
                    session_id=session_id,
                    step="Motion sensor static test",
                )
            )
            sequence += 1

        # A known optical fixture modulation lets the tester separate expected
        # signal energy from residual noise without modeling human physiology.
        ppg_rate_hz = 100.0
        ppg_samples = 800
        time = np.arange(ppg_samples) / ppg_rate_hz
        clean_ppg = 1.0 + 0.22 * np.sin(2 * math.pi * 1.2 * time)
        ppg_noise = 0.10 if active_faults.ppg_low_snr else 0.012
        ppg = clean_ppg + self.rng.normal(0.0, ppg_noise, ppg_samples)
        if active_faults.ppg_saturation:
            ppg[100:180] = 1.50

        records.append(
            self._record(
                seq=sequence,
                name="ppg_optical",
                unit="normalized",
                values=ppg,
                fs=ppg_rate_hz,
                device_id=device_id,
                station_id=station_id,
                session_id=session_id,
                step="Optical sensor response",
            )
        )
        sequence += 1

        temperature = self.rng.normal(33.0, 0.12)
        if active_faults.skin_temp_offset:
            temperature += 1.2
        records.append(
            self._record(
                seq=sequence,
                name="skin_temperature",
                unit="degC",
                values=float(temperature),
                fs=None,
                device_id=device_id,
                station_id=station_id,
                session_id=session_id,
                step="Temperature reference test",
            )
        )
        sequence += 1

        if include_ecg:
            impedance = self.rng.normal(90.0, 8.0)
            if active_faults.ecg_high_impedance:
                impedance = self.rng.normal(210.0, 10.0)
            records.append(
                self._record(
                    seq=sequence,
                    name="ecg_electrode_impedance",
                    unit="kohm",
                    values=float(impedance),
                    fs=None,
                    device_id=device_id,
                    station_id=station_id,
                    session_id=session_id,
                    step="ECG electrode path test",
                )
            )

        return records

    def acquire_golden(
        self,
        config: GoldenUnitConfiguration,
        station_id: str,
        check_number: int,
    ) -> list[MeasurementRecord]:
        """Simulate a known-good reference device measured by one test station.

        The golden unit itself remains fixed. Only the simulated station offsets
        and configured per-check drift change the measured values. This lets the
        software demonstrate tester-health monitoring without physical ATE.

        Args:
            config: Golden-unit references and per-station drift assumptions.
            station_id: Test station being verified.
            check_number: One-based sequential golden check number for this station.

        Returns:
            Canonical measurements representing the station's view of the golden unit.

        Raises:
            ValueError: If the station is not configured or check_number is invalid.
        """

        if station_id not in config.station_drift:
            raise ValueError(f"No golden-unit drift profile is configured for {station_id}.")
        if check_number < 1:
            raise ValueError("Golden-unit check_number must be at least 1.")

        drift = config.station_drift[station_id]
        references = config.references
        multiplier = float(check_number - 1)
        session_id = str(uuid.uuid4())
        records: list[MeasurementRecord] = []
        sequence = 0

        def station_value(reference_key: str, offset_key: str, drift_key: str) -> float:
            """Apply one configured station offset and gradual drift term.

            Args:
                reference_key: Key selecting the known golden-unit value.
                offset_key: Key selecting the station's initial measurement offset.
                drift_key: Key selecting additional drift per golden-unit check.

            Returns:
                Simulated station measurement before random repeatability noise.
            """

            return (
                references[reference_key]
                + drift[offset_key]
                + multiplier * drift[drift_key]
            )

        battery = station_value(
            "battery_voltage", "battery_offset_v", "battery_drift_per_check_v"
        ) + self.rng.normal(0.0, 0.002)
        records.append(
            self._record(
                seq=sequence, name="battery_voltage", unit="V", values=battery, fs=None,
                device_id=config.device_id, station_id=station_id, session_id=session_id,
                step="Golden-unit power reference",
            )
        )
        sequence += 1

        imu_rate_hz = 100.0
        imu_samples = 500
        for axis in "xyz":
            mean = station_value(
                f"accel_{axis}", f"accel_{axis}_offset_g",
                f"accel_{axis}_drift_per_check_g",
            )
            values = self.rng.normal(mean, 0.003, imu_samples)
            records.append(
                self._record(
                    seq=sequence, name=f"accel_{axis}", unit="g", values=values,
                    fs=imu_rate_hz, device_id=config.device_id, station_id=station_id,
                    session_id=session_id, step="Golden-unit motion reference",
                )
            )
            sequence += 1

        for axis in "xyz":
            mean = station_value(
                f"gyro_{axis}", f"gyro_{axis}_offset_dps",
                f"gyro_{axis}_drift_per_check_dps",
            )
            values = self.rng.normal(mean, 0.025, imu_samples)
            records.append(
                self._record(
                    seq=sequence, name=f"gyro_{axis}", unit="deg/s", values=values,
                    fs=imu_rate_hz, device_id=config.device_id, station_id=station_id,
                    session_id=session_id, step="Golden-unit motion reference",
                )
            )
            sequence += 1

        ppg_rate_hz = 100.0
        ppg_samples = 800
        time = np.arange(ppg_samples) / ppg_rate_hz
        ppg_amplitude = station_value(
            "ppg_amplitude", "ppg_amplitude_offset",
            "ppg_amplitude_drift_per_check",
        )
        ppg = (
            1.0
            + ppg_amplitude
            * np.sin(2 * math.pi * config.ppg_fixture_frequency_hz * time)
            + self.rng.normal(0.0, 0.004, ppg_samples)
        )
        records.append(
            self._record(
                seq=sequence, name="ppg_optical", unit="normalized", values=ppg,
                fs=ppg_rate_hz, device_id=config.device_id, station_id=station_id,
                session_id=session_id, step="Golden-unit optical reference",
            )
        )
        sequence += 1

        temperature = station_value(
            "skin_temperature", "skin_temperature_offset_c",
            "skin_temperature_drift_per_check_c",
        ) + self.rng.normal(0.0, 0.015)
        records.append(
            self._record(
                seq=sequence, name="skin_temperature", unit="degC", values=temperature,
                fs=None, device_id=config.device_id, station_id=station_id,
                session_id=session_id, step="Golden-unit temperature reference",
            )
        )
        sequence += 1

        if config.include_ecg:
            impedance = station_value(
                "ecg_electrode_impedance", "ecg_impedance_offset_kohm",
                "ecg_impedance_drift_per_check_kohm",
            ) + self.rng.normal(0.0, 0.8)
            records.append(
                self._record(
                    seq=sequence, name="ecg_electrode_impedance", unit="kohm",
                    values=impedance, fs=None, device_id=config.device_id,
                    station_id=station_id, session_id=session_id,
                    step="Golden-unit ECG path reference",
                )
            )

        return records
    def measure_grr_reference(
        self,
        *,
        metric: GrrMetricDefinition,
        reference_value: float,
        station_id: str,
        golden_config: GoldenUnitConfiguration,
        station_check_number: int,
    ) -> float:
        """Simulate one repeated scalar measurement for a GR&R reference unit.

        The known reference value represents the part-to-part component. The
        configured station offset/drift represents reproducibility, while the
        metric's repeatability sigma represents within-station measurement noise.

        Args:
            metric: GR&R metric definition, including repeatability noise.
            reference_value: Known value assigned to the reference unit.
            station_id: Test station making the measurement.
            golden_config: Station offset and drift configuration.
            station_check_number: Current one-based station-health check number used
                to evaluate the station at its present simulated drift state.

        Returns:
            Simulated scalar measurement in the metric's configured unit.

        Raises:
            ValueError: If the metric or station has no configured drift mapping.
        """

        if station_id not in golden_config.station_drift:
            raise ValueError(f"No station behavior is configured for {station_id}.")
        if station_check_number < 1:
            raise ValueError("station_check_number must be at least 1.")

        drift = golden_config.station_drift[station_id]
        mapping = {
            "battery_voltage": ("battery_offset_v", "battery_drift_per_check_v"),
            "accel_x": ("accel_x_offset_g", "accel_x_drift_per_check_g"),
            "accel_y": ("accel_y_offset_g", "accel_y_drift_per_check_g"),
            "accel_z": ("accel_z_offset_g", "accel_z_drift_per_check_g"),
            "gyro_x": ("gyro_x_offset_dps", "gyro_x_drift_per_check_dps"),
            "gyro_y": ("gyro_y_offset_dps", "gyro_y_drift_per_check_dps"),
            "gyro_z": ("gyro_z_offset_dps", "gyro_z_drift_per_check_dps"),
            "ppg_amplitude": ("ppg_amplitude_offset", "ppg_amplitude_drift_per_check"),
            "skin_temperature": (
                "skin_temperature_offset_c", "skin_temperature_drift_per_check_c"
            ),
            "ecg_electrode_impedance": (
                "ecg_impedance_offset_kohm", "ecg_impedance_drift_per_check_kohm"
            ),
        }
        if metric.key not in mapping:
            raise ValueError(f"No GR&R station-bias mapping exists for {metric.key!r}.")

        offset_key, drift_key = mapping[metric.key]
        multiplier = float(station_check_number - 1)
        station_bias = drift[offset_key] + multiplier * drift[drift_key]
        repeatability_noise = self.rng.normal(0.0, metric.repeatability_sigma)
        return float(reference_value + station_bias + repeatability_noise)

