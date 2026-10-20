import math
import queue
import tempfile
import unittest
from pathlib import Path

import hat_gain_sweep as app_module

from hat_gain_sweep import (
    MeasurementPoint,
    FrequencySweepSettings,
    FrequencySweepWorker,
    RealInstrumentSession,
    SCPIProfile,
    SimulatedInstrumentSession,
    SweepSettings,
    SweepWorker,
    aggregate_amplitude_results,
    average_csv_header,
    build_descending_points,
    build_frequency_points_mhz,
    classify_visa_resources,
    dbm_to_watts,
    linear_mean_dbm,
    load_measurement_csv,
    mvpp_to_dbm,
    parse_scpi_number,
    save_average_table_image,
    write_average_csv,
)


class FakeInstrument:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.writes = []
        self.queries = []

    def write(self, command):
        self.writes.append(command)

    def query(self, command):
        self.queries.append(command)
        return self.responses[command]

    def close(self):
        return None


class SweepCoreTests(unittest.TestCase):
    @staticmethod
    def _measurement(round_index, mvpp, analyzer_dbm):
        input_dbm = mvpp_to_dbm(mvpp, 50)
        corrected = analyzer_dbm + 48
        return MeasurementPoint(
            index=round_index,
            acquired_at="2026-10-20T20:00:00+08:00",
            commanded_mvpp=mvpp,
            peak_frequency_hz=100e6,
            analyzer_peak_dbm=analyzer_dbm,
            analyzer_std_db=0.0,
            external_attenuation_db=48.0,
            corrected_output_dbm=corrected,
            input_dbm=input_dbm,
            gain_db=None if input_dbm is None else corrected - input_dbm,
            output_power_w=dbm_to_watts(corrected),
            samples=1,
            round_index=round_index,
        )

    def test_requested_sweep_has_57_inclusive_points(self):
        points = build_descending_points(580, 300, 5)
        self.assertEqual(len(points), 57)
        self.assertEqual(points[:3], [580.0, 575.0, 570.0])
        self.assertEqual(points[-3:], [310.0, 305.0, 300.0])

    def test_non_divisible_range_includes_exact_stop(self):
        self.assertEqual(build_descending_points(10, 1, 4), [10.0, 6.0, 2.0, 1.0])

    def test_frequency_points_support_both_directions(self):
        self.assertEqual(build_frequency_points_mhz(90, 93, 1), [90.0, 91.0, 92.0, 93.0])
        self.assertEqual(build_frequency_points_mhz(93, 90, 2), [93.0, 91.0, 90.0])

    def test_invalid_direction_is_rejected(self):
        with self.assertRaises(ValueError):
            build_descending_points(300, 580, 5)

    def test_300_mvpp_50_ohm_power(self):
        self.assertAlmostEqual(mvpp_to_dbm(300, 50), -6.478174818886377, places=10)

    def test_dbm_to_watts(self):
        self.assertAlmostEqual(dbm_to_watts(30), 1.0, places=12)
        self.assertAlmostEqual(dbm_to_watts(33.01029995664), 2.0, places=10)

    def test_dbm_average_is_linear_power_average(self):
        expected = 10 * math.log10((1.0 + 10.0) / 2.0)
        self.assertAlmostEqual(linear_mean_dbm([0.0, 10.0]), expected, places=12)

    def test_amplitude_average_preserves_three_rounds_and_uses_linear_power(self):
        raw = [
            self._measurement(1, 300, -20.0),
            self._measurement(2, 300, -19.0),
            self._measurement(3, 300, -21.0),
        ]
        averaged = aggregate_amplitude_results(raw)
        self.assertEqual(len(averaged), 1)
        point = averaged[0]
        self.assertEqual(point.round_count, 3)
        self.assertAlmostEqual(
            point.mean_analyzer_peak_dbm,
            linear_mean_dbm([-20.0, -19.0, -21.0]),
            places=12,
        )
        self.assertAlmostEqual(point.corrected_output_std_db, 1.0, places=12)

    def test_average_csv_contains_summary_and_each_round_raw_values(self):
        raw = [
            self._measurement(1, 300, -20.0),
            self._measurement(2, 300, -19.0),
            self._measurement(3, 300, -21.0),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_path = Path(temp_dir) / "raw.csv"
            with raw_path.open("w", newline="", encoding="utf-8-sig") as handle:
                import csv

                writer = csv.writer(handle)
                writer.writerow(MeasurementPoint.csv_header())
                writer.writerows(point.csv_row() for point in raw)
            loaded = load_measurement_csv(raw_path)
            self.assertEqual([point.round_index for point in loaded], [1, 2, 3])

            average_path = Path(temp_dir) / "average.csv"
            write_average_csv(average_path, aggregate_amplitude_results(loaded))
            text = average_path.read_text(encoding="utf-8-sig")
            self.assertIn("平均增益_dB_线性功率比平均", text)
            self.assertIn("第1轮_频谱仪峰值_dBm", text)
            self.assertIn("第3轮_校正后输出功率_W", text)
            self.assertIn("300", text)

    def test_average_header_has_one_raw_group_per_round(self):
        header = average_csv_header([1, 2, 3])
        self.assertEqual(sum(name.startswith("第1轮_") for name in header), 4)
        self.assertEqual(sum(name.startswith("第2轮_") for name in header), 4)
        self.assertEqual(sum(name.startswith("第3轮_") for name in header), 4)

    def test_average_table_is_rendered_as_standalone_png(self):
        raw = [
            self._measurement(round_index, mvpp, -20.0 + round_index * 0.1)
            for mvpp in (580, 575, 570, 565)
            for round_index in (1, 2, 3)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "average_table.png"
            save_average_table_image(
                target,
                aggregate_amplitude_results(raw),
                source_name="raw.csv",
            )
            self.assertTrue(target.is_file())
            self.assertGreater(target.stat().st_size, 10_000)

    def test_scpi_number_parser(self):
        self.assertAlmostEqual(parse_scpi_number("-2.087000E+01\n"), -20.87)
        self.assertAlmostEqual(parse_scpi_number("value=1.000000E+08"), 100_000_000.0)

    def test_visa_resources_are_classified_by_idn(self):
        identities = {
            "USB0::DG::INSTR": "RIGOL TECHNOLOGIES,DG5352 Pro,DG5Q123,00.01.00",
            "USB0::DSA::INSTR": "RIGOL TECHNOLOGIES,DSA815,DSA8A123,00.01.00",
            "USB0::OTHER::INSTR": "ACME,DMM1000,123,1.0",
        }
        generators, analyzers = classify_visa_resources(identities)
        self.assertEqual(generators, ["USB0::DG::INSTR"])
        self.assertEqual(analyzers, ["USB0::DSA::INSTR"])

    def test_dg5352_and_dsa815_command_sequence(self):
        session = RealInstrumentSession(lambda _message: None)
        session.generator = FakeInstrument()
        session.analyzer = FakeInstrument(
            {
                "*OPC?": "1\n",
                ":CALC:MARK1:Y?": "-1.545000E+01\n",
                ":CALC:MARK1:X?": "1.000000E+08\n",
            }
        )
        profile = SCPIProfile()
        session.prepare(profile)
        session.set_amplitude_mvpp(580, profile)
        level_dbm, frequency_hz, std_db = session.measure_peak(1, 0, profile)
        session.safe_output_off(profile)

        self.assertEqual(
            session.generator.writes,
            [
                ":OUTP1 OFF",
                ":OUTP1:LOAD 50",
                ":SOUR1:VOLT:UNIT VPP",
                ":SOUR1:VOLT 0.58",
                ":OUTP1 ON",
                ":OUTP1 OFF",
            ],
        )
        self.assertEqual(session.analyzer.writes[-2:], [":INIT:IMM", ":CALC:MARK1:MAX:MAX"])
        self.assertEqual(session.analyzer.queries, ["*OPC?", ":CALC:MARK1:Y?", ":CALC:MARK1:X?"])
        self.assertAlmostEqual(level_dbm, -15.45)
        self.assertAlmostEqual(frequency_hz, 100_000_000.0)
        self.assertEqual(std_db, 0.0)

    def test_frequency_commands_move_generator_and_analyzer_together(self):
        session = RealInstrumentSession(lambda _message: None)
        session.generator = FakeInstrument()
        session.analyzer = FakeInstrument()
        session.set_frequency_hz(105_000_000, SCPIProfile())
        self.assertEqual(session.generator.writes, [":SOUR1:FREQ 105000000"])
        self.assertEqual(session.analyzer.writes, [":SENS:FREQ:CENT 105000000"])

    def test_amplitude_worker_runs_each_round_max_to_min(self):
        original_dir = app_module.MEASUREMENT_DIR
        with tempfile.TemporaryDirectory() as temp_dir:
            app_module.MEASUREMENT_DIR = Path(temp_dir)
            try:
                events = queue.Queue()
                session = SimulatedInstrumentSession(lambda _message: None)
                settings = SweepSettings(
                    start_mvpp=10,
                    stop_mvpp=5,
                    step_mvpp=5,
                    settle_s=0.1,
                    rounds=2,
                )
                SweepWorker(session, settings, SCPIProfile(), events, True).run()
                payloads = []
                while not events.empty():
                    kind, payload = events.get()
                    if kind == "point":
                        payloads.append(payload["point"])
                self.assertEqual([point.round_index for point in payloads], [1, 1, 2, 2])
                self.assertEqual([point.commanded_mvpp for point in payloads], [10, 5, 10, 5])
                self.assertFalse(session.output_enabled)
            finally:
                app_module.MEASUREMENT_DIR = original_dir

    def test_frequency_worker_is_independent_from_amplitude_sweep(self):
        original_dir = app_module.MEASUREMENT_DIR
        with tempfile.TemporaryDirectory() as temp_dir:
            app_module.MEASUREMENT_DIR = Path(temp_dir)
            try:
                events = queue.Queue()
                session = SimulatedInstrumentSession(lambda _message: None)
                settings = FrequencySweepSettings(
                    start_mhz=99,
                    stop_mhz=101,
                    step_mhz=1,
                    fixed_mvpp=300,
                    settle_s=0.1,
                )
                FrequencySweepWorker(session, settings, SCPIProfile(), events, True).run()
                frequencies = []
                while not events.empty():
                    kind, payload = events.get()
                    if kind == "frequency_point":
                        frequencies.append(payload["point"].commanded_frequency_hz)
                self.assertEqual(frequencies, [99e6, 100e6, 101e6])
                self.assertFalse(session.output_enabled)
                self.assertEqual(session.current_frequency_hz, 100e6)
            finally:
                app_module.MEASUREMENT_DIR = original_dir


if __name__ == "__main__":
    unittest.main()
