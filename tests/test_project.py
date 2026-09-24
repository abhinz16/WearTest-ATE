"""Critical regression tests for the lean WearTest repository."""

from pathlib import Path
import sys
import json
import math

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from weartest.adapters import ExternalDataImporter
from weartest.data import MeasurementRecord, RecordValidationError, convert_values, read_jsonl, write_jsonl
from weartest.simulator import FaultProfile, WearableSimulator
from weartest.storage import ProductionDatabase, ReliableJsonlSink
from weartest.test_engine import ManufacturingTestEngine


def test_crc_detects_changed_data(tmp_path: Path) -> None:
    """Reject a canonical record when its payload changes after checksum creation.

    Args:
        tmp_path: Temporary pytest directory used for the JSONL file.
    """
    record = MeasurementRecord.create(
        device_id="DUT-1", station_id="ATE-1", session_id="S-1", sequence=0,
        measurement="battery_voltage", unit="V", values=4.05,
    )
    path = tmp_path / "record.jsonl"
    write_jsonl(path, [record])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["values"] = [2.0]
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(RecordValidationError):
        read_jsonl(path)


def test_unit_conversion() -> None:
    """Convert common acquisition units into WearTest canonical engineering units."""
    assert convert_values([4050], "mV", "V") == [4.05]
    assert convert_values([9.80665], "m/s^2", "g") == pytest.approx([1.0])
    assert convert_values([math.pi], "rad/s", "deg/s") == pytest.approx([180.0])


def test_good_device_passes_and_gyro_fault_fails() -> None:
    """Confirm the manufacturing engine accepts a good device and catches gyro bias."""
    simulator = WearableSimulator(rng_seed=12)
    engine = ManufacturingTestEngine()
    good = simulator.acquire("DUT-GOOD", "ATE-1")
    assert engine.evaluate(good).passed
    bad = simulator.acquire("DUT-BAD", "ATE-1", faults=FaultProfile(gyro_bias=True))
    result = engine.evaluate(bad)
    assert not result.passed
    assert any("GYRO" in code for code in result.failure_codes)


def test_external_csv_reaches_same_test_engine() -> None:
    """Send mapped external CSV data through the same acceptance engine as simulation."""
    root = Path(__file__).resolve().parents[1]
    importer = ExternalDataImporter(user_adapter_dir=root / "user_adapters")
    records = importer.import_file(
        root / "examples" / "external_device_example.csv",
        device_id="DUT-CSV", station_id="ATE-2", source_format="csv",
        mapping_path=root / "examples" / "external_device_mapping.json",
    )
    assert records
    assert ManufacturingTestEngine().evaluate(records).passed


def test_storage_fallback_and_database(tmp_path: Path) -> None:
    """Verify production traceability and local fallback storage.

    Args:
        tmp_path: Temporary pytest directory used for database and spool files.
    """
    simulator = WearableSimulator(rng_seed=4)
    records = simulator.acquire("DUT-DB", "ATE-1")
    disposition = ManufacturingTestEngine().evaluate(records)

    db = ProductionDatabase(tmp_path / "production.db")
    db.save_disposition(
        disposition, station_id="ATE-1", product_variant="wearable",
        raw_record_path="records/session.jsonl",
    )
    summary = db.summary()
    assert summary["total"] == 1

    # Make the primary path a directory so opening it as a file fails.
    primary = tmp_path / "primary"
    primary.mkdir()
    spool = tmp_path / "spool.jsonl"
    sink = ReliableJsonlSink(primary, spool)
    assert sink.send(records[0]) == "spool"
    assert read_jsonl(spool)[0].measurement == records[0].measurement


def test_tdms_adapter_when_nptdms_is_available(tmp_path: Path) -> None:
    """Round-trip a real TDMS channel through the LabVIEW adapter when npTDMS exists.

    Args:
        tmp_path: Temporary pytest directory used for the generated TDMS file.
    """
    nptdms = pytest.importorskip("nptdms")
    from nptdms import ChannelObject, GroupObject, RootObject, TdmsWriter
    from weartest.adapters import LabViewTdmsAdapter, ChannelMapping

    path = tmp_path / "sample.tdms"
    with TdmsWriter(path) as writer:
        writer.write_segment([RootObject(), GroupObject("Power"), ChannelObject("Power", "Battery", [4050.0])])

    adapter = LabViewTdmsAdapter({("Power", "Battery"): ChannelMapping("battery_voltage", "mV", "V")})
    records = adapter.convert(path, device_id="DUT-TDMS", station_id="ATE-1", session_id="S-1")
    assert records[0].values == pytest.approx([4.05])


def test_production_dashboard_uses_true_first_pass_yield(tmp_path: Path) -> None:
    """Retests stay traceable but do not rewrite a device's first-pass result.

    Args:
        tmp_path: Temporary pytest directory used for the traceability database.
    """

    simulator = WearableSimulator(rng_seed=31)
    engine = ManufacturingTestEngine()
    db = ProductionDatabase(tmp_path / "production.db")

    first_a = engine.evaluate(simulator.acquire("DUT-A", "ATE-01"))
    retest_a = engine.evaluate(
        simulator.acquire("DUT-A", "ATE-02", faults=FaultProfile(gyro_bias=True))
    )
    first_b = engine.evaluate(
        simulator.acquire("DUT-B", "ATE-02", faults=FaultProfile(battery_low=True))
    )

    for disposition, station in (
        (first_a, "ATE-01"),
        (retest_a, "ATE-02"),
        (first_b, "ATE-02"),
    ):
        db.save_disposition(
            disposition,
            station_id=station,
            product_variant="Wearable + ECG",
            raw_record_path=f"records/{disposition.session_id}.jsonl",
        )

    summary = db.summary()
    assert summary["total"] == 2
    assert summary["sessions"] == 3
    assert summary["passed"] == 1
    assert summary["failed"] == 1
    assert summary["fpy_percent"] == pytest.approx(50.0)

    stations = {row["station_id"]: row for row in db.station_summary()}
    assert stations["ATE-01"]["tested"] == 1
    assert stations["ATE-01"]["fpy_percent"] == pytest.approx(100.0)
    assert stations["ATE-02"]["tested"] == 1
    assert stations["ATE-02"]["fpy_percent"] == pytest.approx(0.0)

    failures = db.failure_summary()
    assert any(row["count"] >= 1 for row in failures)

    # A 20-device rolling metric should not report partial windows.
    rolling = db.rolling_first_pass_yield(limit=20, window=20)
    assert rolling == []


def test_rolling_fpy_uses_complete_twenty_device_windows(tmp_path: Path) -> None:
    """Each FPY trend point should represent exactly twenty first attempts.

    Args:
        tmp_path: Temporary pytest directory used for the traceability database.
    """

    simulator = WearableSimulator(rng_seed=91)
    engine = ManufacturingTestEngine()
    db = ProductionDatabase(tmp_path / "production.db")

    for index in range(21):
        faults = FaultProfile(battery_low=True) if index == 0 else FaultProfile()
        disposition = engine.evaluate(
            simulator.acquire(f"DUT-{index:03d}", "ATE-01", faults=faults)
        )
        db.save_disposition(
            disposition,
            station_id="ATE-01",
            product_variant="Wearable + ECG",
            raw_record_path=f"records/{disposition.session_id}.jsonl",
        )

    rolling = db.rolling_first_pass_yield(limit=10, window=20)
    assert len(rolling) == 2
    assert rolling[0]["sequence"] == 20
    assert rolling[0]["fpy_percent"] == pytest.approx(95.0)
    assert rolling[1]["sequence"] == 21
    assert rolling[1]["fpy_percent"] == pytest.approx(100.0)


def test_golden_unit_detects_station_drift_and_tracks_health(tmp_path: Path) -> None:
    """Repeated golden checks should expose progressive tester drift.

    Args:
        tmp_path: Temporary pytest directory used for tester-health traceability.
    """

    from weartest.test_engine import GoldenUnitEvaluator, load_golden_unit_configuration

    root = Path(__file__).resolve().parents[1]
    config = load_golden_unit_configuration(root / "config" / "golden_units.ini")
    evaluator = GoldenUnitEvaluator(config)
    simulator = WearableSimulator(rng_seed=123)
    db = ProductionDatabase(tmp_path / "production.db")

    first = evaluator.evaluate(
        simulator.acquire_golden(config, "ATE-03", 1), check_number=1
    )
    assert first.status == "Healthy"

    warning = evaluator.evaluate(
        simulator.acquire_golden(config, "ATE-03", 5), check_number=5
    )
    assert warning.status == "Warning"

    attention = evaluator.evaluate(
        simulator.acquire_golden(config, "ATE-03", 8), check_number=8
    )
    assert attention.status == "Needs attention"
    assert attention.worst_metric == "Accelerometer X"
    assert attention.max_bias_ratio > 1.0

    for result in (first, warning, attention):
        db.save_golden_check(
            result,
            raw_record_path=f"records/golden_{result.check_id}.jsonl",
        )

    assert db.golden_check_count("ATE-03") == 3
    latest = {row["station_id"]: row for row in db.tester_health_summary()}
    assert latest["ATE-03"]["health_status"] == "Needs attention"
    assert latest["ATE-03"]["check_count"] == 3


def test_golden_unit_healthy_station_stays_within_reference_limits() -> None:
    """A stable simulated station should keep the known-good reference healthy."""

    from weartest.test_engine import GoldenUnitEvaluator, load_golden_unit_configuration

    config = load_golden_unit_configuration()
    evaluator = GoldenUnitEvaluator(config)
    simulator = WearableSimulator(rng_seed=456)

    for check_number in (1, 5, 10):
        result = evaluator.evaluate(
            simulator.acquire_golden(config, "ATE-01", check_number),
            check_number=check_number,
        )
        assert result.status == "Healthy"
        assert result.max_bias_ratio < config.warning_fraction


def test_product_profiles_are_configurable_and_ecg_requirement_is_enforced() -> None:
    """Configured product profiles should drive which measurements are required."""

    from weartest.test_engine import load_product_profiles

    profiles = {profile.name: profile for profile in load_product_profiles()}
    assert profiles["Wearable"].include_ecg is False
    assert profiles["Wearable + ECG"].include_ecg is True

    simulator = WearableSimulator(rng_seed=808)
    engine = ManufacturingTestEngine()
    no_ecg_records = simulator.acquire(
        "DUT-PROFILE", "ATE-01", include_ecg=False
    )

    base_result = engine.evaluate(no_ecg_records, require_ecg=False)
    assert base_result.passed

    ecg_result = engine.evaluate(no_ecg_records, require_ecg=True)
    assert not ecg_result.passed
    assert "ECG_REQUIRED_MEASUREMENT_MISSING" in ecg_result.failure_codes


def test_crossed_grr_separates_measurement_and_part_variation(tmp_path: Path) -> None:
    """A balanced study should produce finite GR&R components and persist them.

    Args:
        tmp_path: Temporary pytest directory used for GR&R traceability.
    """

    from weartest.grr import (
        GrrObservation,
        build_reference_values,
        load_grr_configuration,
        run_crossed_grr,
    )
    from weartest.test_engine import load_golden_unit_configuration

    grr_config = load_grr_configuration()
    golden_config = load_golden_unit_configuration()
    metric = grr_config.metrics["accel_x"]
    simulator = WearableSimulator(rng_seed=901)
    references = build_reference_values(metric, 5)
    observations = []

    for part_id, reference in references:
        for station_id in sorted(golden_config.station_drift):
            for trial in range(1, 4):
                measured = simulator.measure_grr_reference(
                    metric=metric,
                    reference_value=reference,
                    station_id=station_id,
                    golden_config=golden_config,
                    station_check_number=1,
                )
                observations.append(
                    GrrObservation(part_id, reference, station_id, trial, measured)
                )

    result = run_crossed_grr(observations, metric=metric, config=grr_config)
    assert result.part_count == 5
    assert result.station_count == 3
    assert result.trials_per_station == 3
    assert 0.0 < result.percent_grr < 100.0
    assert result.repeatability_sigma > 0.0
    assert result.part_sigma > 0.0
    assert result.ndc >= 1
    assert set(result.station_biases) == {"ATE-01", "ATE-02", "ATE-03"}

    db = ProductionDatabase(tmp_path / "production.db")
    db.save_grr_study(result)
    studies = db.recent_grr_studies()
    assert len(studies) == 1
    assert studies[0]["study_id"] == result.study_id
    assert len(db.grr_study_measurements(result.study_id)) == 45
    assert len(db.grr_station_biases(result.study_id)) == 3


def test_grr_detects_worse_station_reproducibility_after_drift() -> None:
    """A strongly drifted station should increase measurement-system variation."""

    from weartest.grr import GrrObservation, build_reference_values, load_grr_configuration, run_crossed_grr
    from weartest.test_engine import load_golden_unit_configuration

    grr_config = load_grr_configuration()
    golden_config = load_golden_unit_configuration()
    metric = grr_config.metrics["accel_x"]
    references = build_reference_values(metric, 5)

    def make_result(check_number: int, seed: int):
        """Build one GR&R result with a selected ATE-03 drift state.

        Args:
            check_number: Simulated ATE-03 drift position.
            seed: Repeatable random-number seed.

        Returns:
            Completed crossed GR&R result.
        """
        simulator = WearableSimulator(rng_seed=seed)
        observations = []
        for part_id, reference in references:
            for station_id in sorted(golden_config.station_drift):
                station_check = check_number if station_id == "ATE-03" else 1
                for trial in range(1, 4):
                    observations.append(
                        GrrObservation(
                            part_id,
                            reference,
                            station_id,
                            trial,
                            simulator.measure_grr_reference(
                                metric=metric,
                                reference_value=reference,
                                station_id=station_id,
                                golden_config=golden_config,
                                station_check_number=station_check,
                            ),
                        )
                    )
        return run_crossed_grr(observations, metric=metric, config=grr_config)

    baseline = make_result(1, 902)
    drifted = make_result(9, 902)
    assert abs(drifted.station_biases["ATE-03"]) > abs(baseline.station_biases["ATE-03"])
    assert drifted.reproducibility_sigma > baseline.reproducibility_sigma
    assert drifted.percent_grr > baseline.percent_grr


def test_cycle_time_optimizer_rejects_coverage_loss_and_selects_fast_validated_sequence(tmp_path: Path) -> None:
    """Faster sequences should be accepted only when simulated defect coverage is retained.

    Args:
        tmp_path: Temporary pytest directory used for cycle-time traceability.
    """

    from weartest.cycle_time import (
        CandidateValidationCounts,
        finalize_cycle_time_study,
        is_faulty,
        load_cycle_time_configuration,
        truncate_records_for_candidate,
        validation_fault_profiles,
    )

    config = load_cycle_time_configuration()
    simulator = WearableSimulator(rng_seed=config.rng_seed)
    engine = ManufacturingTestEngine()
    fault_profiles = validation_fault_profiles(include_ecg=True)
    candidates = (config.baseline, *config.candidates)
    counts = {candidate.key: CandidateValidationCounts() for candidate in candidates}

    for index in range(80):
        fault = fault_profiles[index % len(fault_profiles)]
        records = simulator.acquire(
            f"CYCLE-{index:03d}", "ATE-CYCLE", faults=fault, include_ecg=True
        )
        baseline = engine.evaluate(records, require_ecg=True)
        for candidate in candidates:
            candidate_records = (
                records
                if candidate.baseline
                else truncate_records_for_candidate(records, candidate)
            )
            disposition = engine.evaluate(candidate_records, require_ecg=True)
            counts[candidate.key].record(
                defective=is_faulty(fault),
                baseline_failed=not baseline.passed,
                candidate_failed=not disposition.passed,
            )

    result = finalize_cycle_time_study(
        config=config,
        product_name="Wearable + ECG",
        include_ecg=True,
        counts_by_key=counts,
    )
    by_name = {item.candidate.name: item for item in result.candidates}

    assert result.baseline.cycle_seconds > result.recommended.cycle_seconds
    assert result.recommended.candidate.name == "Aggressive"
    assert result.recommended.validated
    assert result.recommended.detection_retention_percent >= config.min_detection_retention_percent
    assert not by_name["Too short"].validated
    assert by_name["Too short"].escape_rate_percent > config.max_escape_rate_percent

    db = ProductionDatabase(tmp_path / "production.db")
    db.save_cycle_time_study(result)
    studies = db.recent_cycle_time_studies()
    assert len(studies) == 1
    assert studies[0]["study_id"] == result.study_id
    stored_candidates = db.cycle_time_candidates(result.study_id)
    assert len(stored_candidates) == 1 + len(result.candidates)
    assert any(row["validated"] == 0 for row in stored_candidates)


def test_cycle_time_candidate_windows_actually_reduce_waveform_samples() -> None:
    """Candidate acquisition windows should truncate waveforms before re-evaluation."""

    from weartest.cycle_time import load_cycle_time_configuration, truncate_records_for_candidate

    config = load_cycle_time_configuration()
    fastest = min(config.candidates, key=lambda item: item.ppg_window_s)
    records = WearableSimulator(rng_seed=17).acquire(
        "DUT-CYCLE", "ATE-01", faults=FaultProfile(ppg_saturation=True), include_ecg=True
    )
    shortened = truncate_records_for_candidate(records, fastest)
    original_ppg = next(record for record in records if record.measurement == "ppg_optical")
    shortened_ppg = next(record for record in shortened if record.measurement == "ppg_optical")
    original_accel = next(record for record in records if record.measurement == "accel_x")
    shortened_accel = next(record for record in shortened if record.measurement == "accel_x")

    assert len(shortened_ppg.values) < len(original_ppg.values)
    assert len(shortened_accel.values) < len(original_accel.values)
    assert len(shortened_ppg.values) == int(fastest.ppg_window_s * original_ppg.sample_rate_hz)
    assert len(shortened_accel.values) == int(fastest.imu_window_s * original_accel.sample_rate_hz)
