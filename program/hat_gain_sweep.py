from __future__ import annotations

import argparse
import csv
import json
import math
import os
import queue
import random
import re
import statistics
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import matplotlib

matplotlib.use("TkAgg")
from matplotlib import rcParams
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

try:
    import pyvisa
except ImportError:  # pragma: no cover - handled in the GUI
    pyvisa = None


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "hat_sweep_settings.json"
MEASUREMENT_DIR = APP_DIR / "measurements"

rcParams["font.sans-serif"] = [
    "Microsoft YaHei",
    "SimHei",
    "Noto Sans CJK SC",
    "Arial Unicode MS",
    "DejaVu Sans",
]
rcParams["axes.unicode_minus"] = False


def build_descending_points(start_mvpp: float, stop_mvpp: float, step_mvpp: float) -> list[float]:
    """Return an inclusive, descending sweep without binary-float drift."""
    start = Decimal(str(start_mvpp))
    stop = Decimal(str(stop_mvpp))
    step = Decimal(str(step_mvpp))
    if step <= 0:
        raise ValueError("步长必须大于 0 mVpp。")
    if start < stop:
        raise ValueError("本程序按从大到小扫描：起点必须大于或等于终点。")
    if start < 0 or stop < 0:
        raise ValueError("幅度不能为负数。")
    if start == stop:
        return [float(start)]

    count = int(((start - stop) / step).to_integral_value(rounding=ROUND_FLOOR))
    points = [start - step * i for i in range(count + 1)]
    if points[-1] != stop:
        points.append(stop)
    return [float(value) for value in points]


def build_frequency_points_mhz(start_mhz: float, stop_mhz: float, step_mhz: float) -> list[float]:
    """Return inclusive frequency points in the requested direction without float drift."""
    start = Decimal(str(start_mhz))
    stop = Decimal(str(stop_mhz))
    step = Decimal(str(step_mhz))
    if step <= 0:
        raise ValueError("频率步长必须大于 0 MHz。")
    if start <= 0 or stop <= 0:
        raise ValueError("起止频率必须大于 0 MHz。")
    if start == stop:
        return [float(start)]

    direction = Decimal("1") if stop > start else Decimal("-1")
    distance = abs(stop - start)
    count = int((distance / step).to_integral_value(rounding=ROUND_FLOOR))
    points = [start + direction * step * i for i in range(count + 1)]
    if points[-1] != stop:
        points.append(stop)
    return [float(value) for value in points]


def mvpp_to_watts(mvpp: float, impedance_ohm: float = 50.0) -> Optional[float]:
    if mvpp <= 0:
        return None
    if impedance_ohm <= 0:
        raise ValueError("阻抗必须大于 0 Ω。")
    vpp = mvpp / 1000.0
    vrms = vpp / (2.0 * math.sqrt(2.0))
    return vrms * vrms / impedance_ohm


def watts_to_dbm(watts: Optional[float]) -> Optional[float]:
    if watts is None or watts <= 0:
        return None
    return 10.0 * math.log10(watts * 1000.0)


def mvpp_to_dbm(mvpp: float, impedance_ohm: float = 50.0) -> Optional[float]:
    return watts_to_dbm(mvpp_to_watts(mvpp, impedance_ohm))


def dbm_to_watts(dbm: float) -> float:
    return 10.0 ** ((dbm - 30.0) / 10.0)


def linear_mean_dbm(values_dbm: Iterable[float]) -> float:
    values = list(values_dbm)
    if not values:
        raise ValueError("没有可平均的读数。")
    mean_mw = sum(10.0 ** (value / 10.0) for value in values) / len(values)
    return 10.0 * math.log10(mean_mw)


def parse_scpi_number(response: str) -> float:
    text = response.strip()
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?", text)
    if not match:
        raise ValueError(f"仪器返回值不是数值：{response!r}")
    value = float(match.group(0))
    if not math.isfinite(value):
        raise ValueError(f"仪器返回了无效数值：{response!r}")
    return value


def iter_scpi_lines(block: str) -> Iterable[str]:
    for raw_line in block.splitlines():
        command = raw_line.strip()
        if command and not command.startswith("#"):
            yield command


def classify_visa_resources(identities: dict[str, str]) -> tuple[list[str], list[str]]:
    """Classify discovered VISA resources from their *IDN? responses."""
    generators: list[str] = []
    analyzers: list[str] = []
    for resource, identity in identities.items():
        upper = identity.upper()
        if re.search(r"(?:^|,)\s*DG\d", upper):
            generators.append(resource)
        if re.search(r"(?:^|,)\s*DSA\d", upper):
            analyzers.append(resource)
    return generators, analyzers


@dataclass(frozen=True)
class SweepSettings:
    start_mvpp: float = 580.0
    stop_mvpp: float = 300.0
    step_mvpp: float = 5.0
    hard_limit_mvpp: float = 580.0
    settle_s: float = 0.2
    samples_per_point: int = 1
    sample_gap_s: float = 0.05
    rounds: int = 2
    inter_round_pause_s: float = 0.0
    attenuation_db: float = 48.0
    impedance_ohm: float = 50.0
    visa_timeout_ms: int = 15000


@dataclass(frozen=True)
class FrequencySweepSettings:
    start_mhz: float = 90.0
    stop_mhz: float = 110.0
    step_mhz: float = 1.0
    fixed_mvpp: float = 300.0
    hard_limit_mvpp: float = 580.0
    settle_s: float = 0.2
    samples_per_point: int = 1
    sample_gap_s: float = 0.05
    attenuation_db: float = 48.0
    impedance_ohm: float = 50.0
    visa_timeout_ms: int = 15000


@dataclass(frozen=True)
class SCPIProfile:
    generator_prepare: str = ":OUTP1:LOAD 50\n:SOUR1:VOLT:UNIT VPP"
    generator_set_amplitude: str = ":SOUR1:VOLT {volts}"
    generator_set_frequency: str = ":SOUR1:FREQ {hz}"
    generator_output_on: str = ":OUTP1 ON"
    generator_output_off: str = ":OUTP1 OFF"
    analyzer_prepare: str = ":CALC:MARK1:STAT ON\n:CALC:MARK1:MODE POS\n:INIT:CONT OFF"
    analyzer_set_center: str = ":SENS:FREQ:CENT {hz}"
    analyzer_trigger: str = ":INIT:IMM"
    analyzer_opc_query: str = "*OPC?"
    analyzer_peak_search: str = ":CALC:MARK1:MAX:MAX"
    analyzer_peak_y_query: str = ":CALC:MARK1:Y?"
    analyzer_peak_x_query: str = ":CALC:MARK1:X?"
    analyzer_restore: str = ":INIT:CONT ON"


@dataclass(frozen=True)
class MeasurementPoint:
    index: int
    acquired_at: str
    commanded_mvpp: float
    peak_frequency_hz: float
    analyzer_peak_dbm: float
    analyzer_std_db: float
    external_attenuation_db: float
    corrected_output_dbm: float
    input_dbm: Optional[float]
    gain_db: Optional[float]
    output_power_w: float
    samples: int
    round_index: int = 1

    @staticmethod
    def csv_header() -> list[str]:
        return [
            "序号",
            "轮次",
            "时间",
            "设定幅度_mVpp",
            "峰值频率_Hz",
            "频谱仪峰值_dBm",
            "峰值标准差_dB",
            "外置衰减_dB",
            "校正后输出_dBm",
            "输入功率_dBm_50ohm",
            "增益_dB",
            "校正后输出功率_W",
            "平均次数",
        ]

    def csv_row(self) -> list[Any]:
        return [
            self.index,
            self.round_index,
            self.acquired_at,
            f"{self.commanded_mvpp:.9g}",
            f"{self.peak_frequency_hz:.12g}",
            f"{self.analyzer_peak_dbm:.9f}",
            f"{self.analyzer_std_db:.9f}",
            f"{self.external_attenuation_db:.9g}",
            f"{self.corrected_output_dbm:.9f}",
            "" if self.input_dbm is None else f"{self.input_dbm:.9f}",
            "" if self.gain_db is None else f"{self.gain_db:.9f}",
            f"{self.output_power_w:.12g}",
            self.samples,
        ]


@dataclass(frozen=True)
class AveragedMeasurementPoint:
    commanded_mvpp: float
    round_points: tuple[MeasurementPoint, ...]
    mean_peak_frequency_hz: float
    mean_analyzer_peak_dbm: float
    mean_corrected_output_dbm: float
    corrected_output_std_db: float
    input_dbm: Optional[float]
    mean_gain_db: Optional[float]
    gain_std_db: Optional[float]
    mean_output_power_w: float

    @property
    def round_count(self) -> int:
        return len(self.round_points)


def aggregate_amplitude_results(
    results: Iterable[MeasurementPoint],
) -> list[AveragedMeasurementPoint]:
    """Average equal-amplitude readings across rounds in linear power space."""
    grouped: dict[float, list[MeasurementPoint]] = {}
    for point in results:
        grouped.setdefault(point.commanded_mvpp, []).append(point)

    averaged: list[AveragedMeasurementPoint] = []
    for mvpp in sorted(grouped, reverse=True):
        round_points = tuple(sorted(grouped[mvpp], key=lambda point: point.round_index))
        corrected = [point.corrected_output_dbm for point in round_points]
        gains = [point.gain_db for point in round_points if point.gain_db is not None]
        averaged.append(
            AveragedMeasurementPoint(
                commanded_mvpp=mvpp,
                round_points=round_points,
                mean_peak_frequency_hz=statistics.fmean(
                    point.peak_frequency_hz for point in round_points
                ),
                mean_analyzer_peak_dbm=linear_mean_dbm(
                    point.analyzer_peak_dbm for point in round_points
                ),
                mean_corrected_output_dbm=linear_mean_dbm(corrected),
                corrected_output_std_db=(
                    statistics.stdev(corrected) if len(corrected) > 1 else 0.0
                ),
                input_dbm=round_points[0].input_dbm,
                mean_gain_db=linear_mean_dbm(gains) if gains else None,
                gain_std_db=statistics.stdev(gains) if len(gains) > 1 else (0.0 if gains else None),
                mean_output_power_w=statistics.fmean(
                    point.output_power_w for point in round_points
                ),
            )
        )
    return averaged


def average_csv_header(round_ids: Iterable[int]) -> list[str]:
    header = [
        "设定幅度_mVpp",
        "有效轮数",
        "平均峰值频率_Hz",
        "平均频谱仪峰值_dBm_线性功率平均",
        "平均校正后输出_dBm_线性功率平均",
        "校正后输出轮间标准差_dB",
        "输入功率_dBm_50ohm",
        "平均增益_dB_线性功率比平均",
        "增益轮间标准差_dB",
        "平均校正后输出功率_W",
    ]
    for round_index in sorted(set(round_ids)):
        header.extend(
            [
                f"第{round_index}轮_频谱仪峰值_dBm",
                f"第{round_index}轮_校正后输出_dBm",
                f"第{round_index}轮_增益_dB",
                f"第{round_index}轮_校正后输出功率_W",
            ]
        )
    return header


def average_csv_row(
    point: AveragedMeasurementPoint, round_ids: Iterable[int]
) -> list[Any]:
    row: list[Any] = [
        f"{point.commanded_mvpp:.9g}",
        point.round_count,
        f"{point.mean_peak_frequency_hz:.12g}",
        f"{point.mean_analyzer_peak_dbm:.9f}",
        f"{point.mean_corrected_output_dbm:.9f}",
        f"{point.corrected_output_std_db:.9f}",
        "" if point.input_dbm is None else f"{point.input_dbm:.9f}",
        "" if point.mean_gain_db is None else f"{point.mean_gain_db:.9f}",
        "" if point.gain_std_db is None else f"{point.gain_std_db:.9f}",
        f"{point.mean_output_power_w:.12g}",
    ]
    by_round = {raw.round_index: raw for raw in point.round_points}
    for round_index in sorted(set(round_ids)):
        raw = by_round.get(round_index)
        if raw is None:
            row.extend(["", "", "", ""])
        else:
            row.extend(
                [
                    f"{raw.analyzer_peak_dbm:.9f}",
                    f"{raw.corrected_output_dbm:.9f}",
                    "" if raw.gain_db is None else f"{raw.gain_db:.9f}",
                    f"{raw.output_power_w:.12g}",
                ]
            )
    return row


def write_average_csv(path: Path, averaged: Iterable[AveragedMeasurementPoint]) -> None:
    rows = list(averaged)
    round_ids = sorted(
        {raw.round_index for point in rows for raw in point.round_points}
    )
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(average_csv_header(round_ids))
        writer.writerows(average_csv_row(point, round_ids) for point in rows)


def load_measurement_csv(path: Path) -> list[MeasurementPoint]:
    points: list[MeasurementPoint] = []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"序号", "轮次", "设定幅度_mVpp", "频谱仪峰值_dBm", "校正后输出_dBm"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError(f"不是本程序的多轮扫幅 CSV：{path.name}")
        for row in reader:
            input_text = row.get("输入功率_dBm_50ohm", "").strip()
            gain_text = row.get("增益_dB", "").strip()
            points.append(
                MeasurementPoint(
                    index=int(row["序号"]),
                    round_index=int(row["轮次"]),
                    acquired_at=row.get("时间", ""),
                    commanded_mvpp=float(row["设定幅度_mVpp"]),
                    peak_frequency_hz=float(row.get("峰值频率_Hz", 0.0)),
                    analyzer_peak_dbm=float(row["频谱仪峰值_dBm"]),
                    analyzer_std_db=float(row.get("峰值标准差_dB", 0.0)),
                    external_attenuation_db=float(row.get("外置衰减_dB", 0.0)),
                    corrected_output_dbm=float(row["校正后输出_dBm"]),
                    input_dbm=float(input_text) if input_text else None,
                    gain_db=float(gain_text) if gain_text else None,
                    output_power_w=float(row.get("校正后输出功率_W", 0.0)),
                    samples=int(row.get("平均次数", 1)),
                )
            )
    return points


def save_average_plot_file(
    path: Path, averaged: Iterable[AveragedMeasurementPoint]
) -> None:
    rows = sorted(list(averaged), key=lambda point: point.commanded_mvpp)
    if not rows:
        raise ValueError("没有可绘制的平均数据。")
    figure = Figure(
        figsize=(12.0, 7.2), dpi=120, facecolor="#ffffff", constrained_layout=True
    )
    ax_level = figure.add_subplot(211)
    ax_gain = figure.add_subplot(212, sharex=ax_level)
    for axis in (ax_level, ax_gain):
        axis.set_facecolor("#fbfcfe")
        axis.grid(True, color="#dfe6ef", linewidth=0.8, alpha=0.9)
        axis.tick_params(colors="#5d6b82", labelsize=9)
        for spine in axis.spines.values():
            spine.set_color("#cbd5e1")

    x = [point.commanded_mvpp for point in rows]
    output = [point.mean_corrected_output_dbm for point in rows]
    output_std = [point.corrected_output_std_db for point in rows]
    gain_rows = [point for point in rows if point.mean_gain_db is not None]
    gain_x = [point.commanded_mvpp for point in gain_rows]
    gain = [point.mean_gain_db for point in gain_rows]
    gain_std = [point.gain_std_db or 0.0 for point in gain_rows]
    ax_level.plot(
        x, output, "o-", color="#1769e0", markersize=3.5, linewidth=2.0,
        label="多轮线性功率平均"
    )
    if any(value > 0 for value in output_std):
        ax_level.fill_between(
            x,
            [value - std for value, std in zip(output, output_std)],
            [value + std for value, std in zip(output, output_std)],
            color="#1769e0",
            alpha=0.16,
            label="轮间 ±1σ",
        )
    ax_gain.plot(
        gain_x, gain, "o-", color="#008a65", markersize=3.5, linewidth=2.0,
        label="多轮平均增益"
    )
    if any(value > 0 for value in gain_std):
        ax_gain.fill_between(
            gain_x,
            [value - std for value, std in zip(gain, gain_std)],
            [value + std for value, std in zip(gain, gain_std)],
            color="#008a65",
            alpha=0.16,
            label="轮间 ±1σ",
        )
    ax_level.set_ylabel("平均输出 (dBm)", color="#152238")
    ax_gain.set_ylabel("平均增益 (dB)", color="#152238")
    ax_gain.set_xlabel("信号发生器幅度 (mVpp)", color="#152238")
    ax_level.set_title("多轮平均输出曲线", loc="left", fontweight="bold")
    ax_gain.set_title("多轮平均增益曲线", loc="left", fontweight="bold")
    ax_level.legend(loc="best", frameon=True)
    ax_gain.legend(loc="best", frameon=True)
    figure.savefig(path, dpi=180, facecolor="#ffffff")


def save_average_table_image(
    path: Path,
    averaged: Iterable[AveragedMeasurementPoint],
    source_name: str = "",
) -> None:
    """Render all averaged amplitude points as one dense, zoomable PNG table."""
    rows = sorted(list(averaged), key=lambda point: point.commanded_mvpp, reverse=True)
    if not rows:
        raise ValueError("没有可绘制的平均数据。")

    panel_count = min(4, len(rows))
    rows_per_panel = math.ceil(len(rows) / panel_count)
    chunks = [
        rows[index : index + rows_per_panel]
        for index in range(0, len(rows), rows_per_panel)
    ]
    panel_rows = 2 if len(chunks) > 2 else 1
    panel_cols = 2 if len(chunks) > 1 else 1
    figure = Figure(
        figsize=(28.0 if panel_cols == 2 else 14.0, 17.0 if panel_rows == 2 else 9.0),
        dpi=160,
        facecolor="#ffffff",
    )
    grid = figure.add_gridspec(
        panel_rows,
        panel_cols,
        left=0.018,
        right=0.992,
        bottom=0.025,
        top=0.925,
        wspace=0.035,
        hspace=0.10,
    )

    column_labels = [
        "幅度\n(mVpp)",
        "轮数",
        "峰值频率\n(MHz)",
        "平均 DSA\n(dBm)",
        "平均输出\n(dBm)",
        "输出 σ\n(dB)",
        "输入\n(dBm)",
        "平均增益\n(dB)",
        "增益 σ\n(dB)",
        "平均输出\n(W)",
    ]
    column_widths = [0.085, 0.055, 0.115, 0.105, 0.105, 0.085, 0.095, 0.10, 0.085, 0.11]

    for panel_index, chunk in enumerate(chunks):
        axis = figure.add_subplot(grid[panel_index // panel_cols, panel_index % panel_cols])
        axis.axis("off")
        cell_text = []
        for point in chunk:
            cell_text.append(
                [
                    f"{point.commanded_mvpp:g}",
                    str(point.round_count),
                    f"{point.mean_peak_frequency_hz / 1e6:.6f}",
                    f"{point.mean_analyzer_peak_dbm:.4f}",
                    f"{point.mean_corrected_output_dbm:.4f}",
                    f"{point.corrected_output_std_db:.4f}",
                    "—" if point.input_dbm is None else f"{point.input_dbm:.4f}",
                    "—" if point.mean_gain_db is None else f"{point.mean_gain_db:.4f}",
                    "—" if point.gain_std_db is None else f"{point.gain_std_db:.4f}",
                    f"{point.mean_output_power_w:.5f}",
                ]
            )
        table = axis.table(
            cellText=cell_text,
            colLabels=column_labels,
            colWidths=column_widths,
            cellLoc="center",
            colLoc="center",
            loc="center",
            bbox=[0.0, 0.0, 1.0, 1.0],
        )
        table.auto_set_font_size(False)
        table.set_fontsize(7.3)
        for (row_index, _), cell in table.get_celld().items():
            cell.set_edgecolor("#cbd5e1")
            cell.set_linewidth(0.45)
            if row_index == 0:
                cell.set_facecolor("#15304f")
                cell.set_text_props(color="white", weight="bold")
            else:
                cell.set_facecolor("#f5f8fc" if row_index % 2 == 0 else "#ffffff")
                cell.set_text_props(color="#152238")
        axis.set_title(
            f"{chunk[0].commanded_mvpp:g} → {chunk[-1].commanded_mvpp:g} mVpp",
            loc="left",
            fontsize=10,
            fontweight="bold",
            color="#152238",
            pad=5,
        )

    rounds = sorted({raw.round_index for point in rows for raw in point.round_points})
    source_text = f"  ｜  数据源：{source_name}" if source_name else ""
    figure.suptitle(
        f"多轮扫幅平均数据表（{len(rows)} 个幅度点，{len(rounds)} 轮）{source_text}",
        x=0.02,
        ha="left",
        y=0.985,
        fontsize=17,
        fontweight="bold",
        color="#152238",
    )
    figure.text(
        0.02,
        0.952,
        "dBm 平均按线性功率计算；σ 为各轮 dB 数值的样本标准差。完整逐轮原始值保存在同名 _average.csv。",
        ha="left",
        va="top",
        fontsize=9,
        color="#5d6b82",
    )
    figure.savefig(path, dpi=160, facecolor="#ffffff")


@dataclass(frozen=True)
class FrequencyMeasurementPoint:
    index: int
    acquired_at: str
    commanded_frequency_hz: float
    commanded_mvpp: float
    peak_frequency_hz: float
    analyzer_peak_dbm: float
    analyzer_std_db: float
    external_attenuation_db: float
    corrected_output_dbm: float
    input_dbm: Optional[float]
    gain_db: Optional[float]
    output_power_w: float
    samples: int

    @staticmethod
    def csv_header() -> list[str]:
        return [
            "序号",
            "时间",
            "设定频率_Hz",
            "固定幅度_mVpp",
            "峰值频率_Hz",
            "频谱仪峰值_dBm",
            "峰值标准差_dB",
            "外置衰减_dB",
            "校正后输出_dBm",
            "输入功率_dBm_50ohm",
            "增益_dB",
            "校正后输出功率_W",
            "平均次数",
        ]

    def csv_row(self) -> list[Any]:
        return [
            self.index,
            self.acquired_at,
            f"{self.commanded_frequency_hz:.12g}",
            f"{self.commanded_mvpp:.9g}",
            f"{self.peak_frequency_hz:.12g}",
            f"{self.analyzer_peak_dbm:.9f}",
            f"{self.analyzer_std_db:.9f}",
            f"{self.external_attenuation_db:.9g}",
            f"{self.corrected_output_dbm:.9f}",
            "" if self.input_dbm is None else f"{self.input_dbm:.9f}",
            "" if self.gain_db is None else f"{self.gain_db:.9f}",
            f"{self.output_power_w:.12g}",
            self.samples,
        ]


class RealInstrumentSession:
    def __init__(self, log: Callable[[str], None]) -> None:
        self.log = log
        self.resource_manager: Any = None
        self.generator: Any = None
        self.analyzer: Any = None
        self.generator_resource = ""
        self.analyzer_resource = ""
        self.generator_idn = ""
        self.analyzer_idn = ""
        self.output_enabled = False
        # Keep generator and analyzer I/O independent. A long DSA *OPC? wait must
        # never prevent the RF-off command from reaching the generator.
        self.lifecycle_lock = threading.RLock()
        self.generator_lock = threading.RLock()
        self.analyzer_lock = threading.RLock()

    @property
    def connected(self) -> bool:
        return self.generator is not None and self.analyzer is not None

    def connect(self, generator_resource: str, analyzer_resource: str, timeout_ms: int) -> tuple[str, str]:
        if pyvisa is None:
            raise RuntimeError("未安装 PyVISA。请运行 install_dependencies.cmd。")
        if not generator_resource.strip() or not analyzer_resource.strip():
            raise ValueError("请选择或输入两台仪器的 VISA 地址。")
        if generator_resource.strip() == analyzer_resource.strip():
            raise ValueError("信号发生器和频谱仪不能选择同一个 VISA 地址。")

        with self.lifecycle_lock:
            self.close()
            self.log("正在打开 VISA ResourceManager…")
            self.resource_manager = pyvisa.ResourceManager()
            try:
                self.generator = self.resource_manager.open_resource(generator_resource.strip())
                self.analyzer = self.resource_manager.open_resource(analyzer_resource.strip())
                for instrument in (self.generator, self.analyzer):
                    instrument.timeout = timeout_ms
                    instrument.read_termination = "\n"
                    instrument.write_termination = "\n"
                self.generator_idn = self.generator.query("*IDN?").strip()
                self.analyzer_idn = self.analyzer.query("*IDN?").strip()
                generators, analyzers = classify_visa_resources(
                    {
                        generator_resource.strip(): self.generator_idn,
                        analyzer_resource.strip(): self.analyzer_idn,
                    }
                )
                if generator_resource.strip() not in generators:
                    raise ValueError(
                        "发生器地址返回的不是 RIGOL DG 系列："
                        f"{self.generator_idn or '空响应'}。请重新扫描并选择 DG5352 Pro。"
                    )
                if analyzer_resource.strip() not in analyzers:
                    raise ValueError(
                        "频谱仪地址返回的不是 RIGOL DSA 系列："
                        f"{self.analyzer_idn or '空响应'}。请重新扫描并选择 DSA815。"
                    )
                self.generator_resource = generator_resource.strip()
                self.analyzer_resource = analyzer_resource.strip()
                self.log(f"信号发生器：{self.generator_idn}")
                self.log(f"频谱仪：{self.analyzer_idn}")
                return self.generator_idn, self.analyzer_idn
            except Exception:
                self.close()
                raise

    def close(self) -> None:
        with self.lifecycle_lock:
            with self.generator_lock:
                if self.generator is not None:
                    try:
                        self.generator.close()
                    except Exception:
                        pass
                self.generator = None
                self.output_enabled = False
            with self.analyzer_lock:
                if self.analyzer is not None:
                    try:
                        self.analyzer.close()
                    except Exception:
                        pass
                self.analyzer = None
            if self.resource_manager is not None:
                try:
                    self.resource_manager.close()
                except Exception:
                    pass
            self.resource_manager = None

    def _write(self, instrument: Any, command: str, label: str) -> None:
        self.log(f"{label}  <<  {command}")
        instrument.write(command)

    def _query(self, instrument: Any, command: str, label: str, verbose: bool = False) -> str:
        if verbose:
            self.log(f"{label}  ??  {command}")
        response = instrument.query(command).strip()
        if verbose:
            self.log(f"{label}  >>  {response}")
        return response

    def prepare(self, profile: SCPIProfile) -> None:
        if not self.connected:
            raise RuntimeError("真机尚未连接。")
        self.safe_output_off(profile, quiet=True)
        with self.generator_lock:
            for command in iter_scpi_lines(profile.generator_prepare):
                self._write(self.generator, command, "GEN")
        with self.analyzer_lock:
            for command in iter_scpi_lines(profile.analyzer_prepare):
                self._write(self.analyzer, command, "DSA")

    def set_amplitude_mvpp(self, mvpp: float, profile: SCPIProfile) -> None:
        with self.generator_lock:
            if mvpp <= 0:
                self.safe_output_off(profile)
                return
            try:
                command = profile.generator_set_amplitude.format(
                    volts=f"{mvpp / 1000.0:.12g}", mvpp=f"{mvpp:.12g}"
                )
            except KeyError as exc:
                raise ValueError("幅度命令模板只支持 {volts} 和 {mvpp}。") from exc
            self._write(self.generator, command, "GEN")
            if not self.output_enabled:
                self._write(self.generator, profile.generator_output_on.strip(), "GEN")
                self.output_enabled = True

    def set_frequency_hz(self, frequency_hz: float, profile: SCPIProfile) -> None:
        if frequency_hz <= 0:
            raise ValueError("频率必须大于 0 Hz。")
        try:
            generator_command = profile.generator_set_frequency.format(
                hz=f"{frequency_hz:.12g}", mhz=f"{frequency_hz / 1e6:.12g}"
            )
            analyzer_command = profile.analyzer_set_center.format(
                hz=f"{frequency_hz:.12g}", mhz=f"{frequency_hz / 1e6:.12g}"
            )
        except KeyError as exc:
            raise ValueError("频率命令模板只支持 {hz} 和 {mhz}。") from exc
        with self.generator_lock:
            self._write(self.generator, generator_command.strip(), "GEN")
        with self.analyzer_lock:
            self._write(self.analyzer, analyzer_command.strip(), "DSA")

    def get_frequency_hz(self) -> float:
        with self.generator_lock:
            return parse_scpi_number(self._query(self.generator, ":SOUR1:FREQ?", "GEN"))

    def measure_peak(self, samples: int, sample_gap_s: float, profile: SCPIProfile) -> tuple[float, float, float]:
        y_values: list[float] = []
        x_values: list[float] = []
        with self.analyzer_lock:
            for sample_index in range(samples):
                for command in iter_scpi_lines(profile.analyzer_trigger):
                    self._write(self.analyzer, command, "DSA")
                if profile.analyzer_opc_query.strip():
                    self._query(self.analyzer, profile.analyzer_opc_query.strip(), "DSA")
                for command in iter_scpi_lines(profile.analyzer_peak_search):
                    self._write(self.analyzer, command, "DSA")
                y_values.append(
                    parse_scpi_number(
                        self._query(self.analyzer, profile.analyzer_peak_y_query.strip(), "DSA")
                    )
                )
                x_values.append(
                    parse_scpi_number(
                        self._query(self.analyzer, profile.analyzer_peak_x_query.strip(), "DSA")
                    )
                )
                if sample_index + 1 < samples and sample_gap_s > 0:
                    time.sleep(sample_gap_s)
        mean_dbm = linear_mean_dbm(y_values)
        std_db = statistics.stdev(y_values) if len(y_values) > 1 else 0.0
        mean_hz = statistics.fmean(x_values)
        return mean_dbm, mean_hz, std_db

    def safe_output_off(self, profile: SCPIProfile, quiet: bool = False) -> None:
        with self.generator_lock:
            if self.generator is None:
                return
            try:
                command = profile.generator_output_off.strip()
                if command:
                    if quiet:
                        self.generator.write(command)
                    else:
                        self._write(self.generator, command, "GEN")
            finally:
                self.output_enabled = False

    def restore_analyzer(self, profile: SCPIProfile) -> None:
        with self.analyzer_lock:
            if self.analyzer is None:
                return
            for command in iter_scpi_lines(profile.analyzer_restore):
                try:
                    self._write(self.analyzer, command, "DSA")
                except Exception as exc:
                    self.log(f"恢复频谱仪连续扫描失败：{exc}")


class SimulatedInstrumentSession:
    def __init__(self, log: Callable[[str], None]) -> None:
        self.log = log
        self.current_mvpp = 0.0
        self.current_frequency_hz = 100_000_000.0
        self.output_enabled = False
        self.rng = random.Random(20261020)

    def prepare(self, profile: SCPIProfile) -> None:
        self.current_mvpp = 0.0
        self.current_frequency_hz = 100_000_000.0
        self.output_enabled = False
        self.log("模拟仪器已准备：中心频率 100 MHz，标称增益约 33.7 dB。")

    def set_amplitude_mvpp(self, mvpp: float, profile: SCPIProfile) -> None:
        self.current_mvpp = mvpp
        self.output_enabled = mvpp > 0

    def set_frequency_hz(self, frequency_hz: float, profile: SCPIProfile) -> None:
        self.current_frequency_hz = frequency_hz

    def get_frequency_hz(self) -> float:
        return self.current_frequency_hz

    def measure_peak(self, samples: int, sample_gap_s: float, profile: SCPIProfile) -> tuple[float, float, float]:
        readings: list[float] = []
        frequencies: list[float] = []
        for _ in range(samples):
            input_dbm = mvpp_to_dbm(self.current_mvpp, 50.0)
            if input_dbm is None or not self.output_enabled:
                analyzer_dbm = -96.0 + self.rng.gauss(0.0, 0.35)
            else:
                compression = 0.18 * max(0.0, (self.current_mvpp - 430.0) / 150.0) ** 2
                offset_mhz = (self.current_frequency_hz - 100_000_000.0) / 1e6
                frequency_shape = -0.0022 * offset_mhz * offset_mhz + 0.018 * math.sin(offset_mhz / 2.8)
                gain_db = 33.72 - compression + frequency_shape
                analyzer_dbm = input_dbm + gain_db - 48.0 + self.rng.gauss(0.0, 0.045)
            readings.append(analyzer_dbm)
            frequencies.append(self.current_frequency_hz + self.rng.gauss(0.0, 120.0))
        mean_dbm = linear_mean_dbm(readings)
        std_db = statistics.stdev(readings) if len(readings) > 1 else 0.0
        return mean_dbm, statistics.fmean(frequencies), std_db

    def safe_output_off(self, profile: SCPIProfile, quiet: bool = False) -> None:
        self.output_enabled = False
        self.current_mvpp = 0.0

    def restore_analyzer(self, profile: SCPIProfile) -> None:
        return


class SweepWorker(threading.Thread):
    def __init__(
        self,
        session: Any,
        settings: SweepSettings,
        profile: SCPIProfile,
        event_queue: queue.Queue,
        simulated: bool,
    ) -> None:
        super().__init__(daemon=True)
        self.session = session
        self.settings = settings
        self.profile = profile
        self.event_queue = event_queue
        self.simulated = simulated
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()

    def emit(self, kind: str, payload: Any = None) -> None:
        self.event_queue.put((kind, payload))

    def request_stop(self) -> None:
        self.stop_event.set()
        self.pause_event.clear()

    def set_paused(self, paused: bool) -> None:
        if paused:
            self.pause_event.set()
        else:
            self.pause_event.clear()

    def _wait_interruptibly(self, seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if self.stop_event.wait(min(0.05, max(0.0, deadline - time.monotonic()))):
                return False
        return not self.stop_event.is_set()

    def _wait_if_paused(self) -> bool:
        announced = False
        while self.pause_event.is_set():
            if not announced:
                self.emit("status", "已暂停；当前 RF 幅度保持不变。按“继续”恢复。")
                announced = True
            if self.stop_event.wait(0.1):
                return False
        if announced:
            self.emit("status", "继续扫描。")
        return not self.stop_event.is_set()

    def run(self) -> None:
        points = build_descending_points(
            self.settings.start_mvpp, self.settings.stop_mvpp, self.settings.step_mvpp
        )
        total_points = len(points) * self.settings.rounds
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        mode_suffix = "_SIM" if self.simulated else ""
        MEASUREMENT_DIR.mkdir(parents=True, exist_ok=True)
        csv_path = MEASUREMENT_DIR / (
            f"amplitude_sweep_{self.settings.rounds}rounds_{timestamp}{mode_suffix}.csv"
        )
        metadata_path = csv_path.with_suffix(".json")
        results: list[MeasurementPoint] = []
        stopped = False

        self.emit("started", {"total": total_points, "csv_path": str(csv_path), "sweep_type": "amplitude"})
        try:
            self.session.prepare(self.profile)
            self.emit(
                "status",
                f"准备完成：共 {self.settings.rounds} 轮，每轮 {points[0]:g} → {points[-1]:g} mVpp。",
            )
            with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(MeasurementPoint.csv_header())
                handle.flush()

                global_index = 0
                for round_index in range(1, self.settings.rounds + 1):
                    if round_index > 1:
                        self.session.safe_output_off(self.profile)
                        pause = (
                            min(self.settings.inter_round_pause_s, 0.05)
                            if self.simulated
                            else self.settings.inter_round_pause_s
                        )
                        self.emit(
                            "status",
                            f"第 {round_index - 1} 轮完成，RF 已关闭；准备第 {round_index} 轮。",
                        )
                        if not self._wait_interruptibly(pause):
                            stopped = True
                            break

                    for round_point_index, mvpp in enumerate(points, start=1):
                        if self.stop_event.is_set() or not self._wait_if_paused():
                            stopped = True
                            break

                        global_index += 1
                        self.emit(
                            "status",
                            f"第 {round_index}/{self.settings.rounds} 轮，"
                            f"点 {round_point_index}/{len(points)}：{mvpp:g} mVpp…",
                        )
                        self.session.set_amplitude_mvpp(mvpp, self.profile)
                        requested_settle = max(
                            self.settings.settle_s,
                            0.5 if round_point_index == 1 else 0.0,
                        )
                        settle = min(requested_settle, 0.035) if self.simulated else requested_settle
                        if not self._wait_interruptibly(settle):
                            stopped = True
                            break

                        analyzer_dbm, peak_hz, std_db = self.session.measure_peak(
                            self.settings.samples_per_point,
                            self.settings.sample_gap_s,
                            self.profile,
                        )
                        corrected_dbm = analyzer_dbm + self.settings.attenuation_db
                        input_dbm = mvpp_to_dbm(mvpp, self.settings.impedance_ohm)
                        gain_db = None if input_dbm is None else corrected_dbm - input_dbm
                        point = MeasurementPoint(
                            index=global_index,
                            acquired_at=datetime.now().astimezone().isoformat(timespec="milliseconds"),
                            commanded_mvpp=mvpp,
                            peak_frequency_hz=peak_hz,
                            analyzer_peak_dbm=analyzer_dbm,
                            analyzer_std_db=std_db,
                            external_attenuation_db=self.settings.attenuation_db,
                            corrected_output_dbm=corrected_dbm,
                            input_dbm=input_dbm,
                            gain_db=gain_db,
                            output_power_w=dbm_to_watts(corrected_dbm),
                            samples=self.settings.samples_per_point,
                            round_index=round_index,
                        )
                        results.append(point)
                        writer.writerow(point.csv_row())
                        handle.flush()
                        self.emit(
                            "point",
                            {
                                "point": point,
                                "index": global_index,
                                "total": total_points,
                                "round": round_index,
                                "round_point": round_point_index,
                            },
                        )
                    if stopped:
                        break

            metadata = {
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "mode": "simulation" if self.simulated else "visa",
                "status": "stopped" if stopped else "complete",
                "point_count": len(results),
                "settings": asdict(self.settings),
                "scpi_profile": asdict(self.profile),
                "csv_file": csv_path.name,
            }
            metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            if stopped:
                self.emit(
                    "stopped",
                    {"results": results, "csv_path": str(csv_path), "metadata_path": str(metadata_path)},
                )
            else:
                self.emit(
                    "complete",
                    {"results": results, "csv_path": str(csv_path), "metadata_path": str(metadata_path)},
                )
        except Exception as exc:
            self.emit(
                "error",
                {
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                    "results": results,
                    "csv_path": str(csv_path),
                },
            )
        finally:
            try:
                self.session.safe_output_off(self.profile)
            except Exception as exc:
                self.emit("log", f"自动关闭 RF 时出错：{exc}")
            try:
                self.session.restore_analyzer(self.profile)
            except Exception as exc:
                self.emit("log", f"恢复频谱仪时出错：{exc}")
            self.emit("rf_off", None)


class FrequencySweepWorker(SweepWorker):
    settings: FrequencySweepSettings

    def run(self) -> None:
        points_mhz = build_frequency_points_mhz(
            self.settings.start_mhz, self.settings.stop_mhz, self.settings.step_mhz
        )
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        mode_suffix = "_SIM" if self.simulated else ""
        MEASUREMENT_DIR.mkdir(parents=True, exist_ok=True)
        csv_path = MEASUREMENT_DIR / f"frequency_sweep_{timestamp}{mode_suffix}.csv"
        metadata_path = csv_path.with_suffix(".json")
        results: list[FrequencyMeasurementPoint] = []
        stopped = False
        original_frequency_hz: Optional[float] = None

        self.emit(
            "frequency_started",
            {"total": len(points_mhz), "csv_path": str(csv_path), "sweep_type": "frequency"},
        )
        try:
            original_frequency_hz = self.session.get_frequency_hz()
            self.session.prepare(self.profile)
            self.emit(
                "status",
                f"扫频准备完成：{points_mhz[0]:g} → {points_mhz[-1]:g} MHz，"
                f"固定 {self.settings.fixed_mvpp:g} mVpp。",
            )
            with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(FrequencyMeasurementPoint.csv_header())
                handle.flush()

                for index, frequency_mhz in enumerate(points_mhz, start=1):
                    if self.stop_event.is_set() or not self._wait_if_paused():
                        stopped = True
                        break

                    frequency_hz = frequency_mhz * 1e6
                    self.emit(
                        "status",
                        f"扫频第 {index}/{len(points_mhz)} 点：{frequency_mhz:g} MHz…",
                    )
                    self.session.set_frequency_hz(frequency_hz, self.profile)
                    if index == 1:
                        self.session.set_amplitude_mvpp(self.settings.fixed_mvpp, self.profile)
                    requested_settle = max(self.settings.settle_s, 0.5 if index == 1 else 0.0)
                    settle = min(requested_settle, 0.035) if self.simulated else requested_settle
                    if not self._wait_interruptibly(settle):
                        stopped = True
                        break

                    analyzer_dbm, peak_hz, std_db = self.session.measure_peak(
                        self.settings.samples_per_point,
                        self.settings.sample_gap_s,
                        self.profile,
                    )
                    corrected_dbm = analyzer_dbm + self.settings.attenuation_db
                    input_dbm = mvpp_to_dbm(self.settings.fixed_mvpp, self.settings.impedance_ohm)
                    gain_db = None if input_dbm is None else corrected_dbm - input_dbm
                    point = FrequencyMeasurementPoint(
                        index=index,
                        acquired_at=datetime.now().astimezone().isoformat(timespec="milliseconds"),
                        commanded_frequency_hz=frequency_hz,
                        commanded_mvpp=self.settings.fixed_mvpp,
                        peak_frequency_hz=peak_hz,
                        analyzer_peak_dbm=analyzer_dbm,
                        analyzer_std_db=std_db,
                        external_attenuation_db=self.settings.attenuation_db,
                        corrected_output_dbm=corrected_dbm,
                        input_dbm=input_dbm,
                        gain_db=gain_db,
                        output_power_w=dbm_to_watts(corrected_dbm),
                        samples=self.settings.samples_per_point,
                    )
                    results.append(point)
                    writer.writerow(point.csv_row())
                    handle.flush()
                    self.emit(
                        "frequency_point",
                        {"point": point, "index": index, "total": len(points_mhz)},
                    )

            metadata = {
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "mode": "simulation" if self.simulated else "visa",
                "sweep_type": "frequency",
                "status": "stopped" if stopped else "complete",
                "point_count": len(results),
                "settings": asdict(self.settings),
                "scpi_profile": asdict(self.profile),
                "csv_file": csv_path.name,
            }
            metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            event = "frequency_stopped" if stopped else "frequency_complete"
            self.emit(
                event,
                {"results": results, "csv_path": str(csv_path), "metadata_path": str(metadata_path)},
            )
        except Exception as exc:
            self.emit(
                "frequency_error",
                {
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                    "results": results,
                    "csv_path": str(csv_path),
                },
            )
        finally:
            try:
                self.session.safe_output_off(self.profile)
            except Exception as exc:
                self.emit("log", f"扫频结束自动关闭 RF 时出错：{exc}")
            if original_frequency_hz is not None:
                try:
                    self.session.set_frequency_hz(original_frequency_hz, self.profile)
                    self.emit("log", f"扫频结束已恢复原频率：{original_frequency_hz / 1e6:g} MHz")
                except Exception as exc:
                    self.emit("log", f"扫频结束恢复原频率失败：{exc}")
            try:
                self.session.restore_analyzer(self.profile)
            except Exception as exc:
                self.emit("log", f"扫频结束恢复频谱仪时出错：{exc}")
            self.emit("rf_off", None)


class SweepApp:
    BG = "#f3f6fa"
    CARD = "#ffffff"
    INK = "#152238"
    MUTED = "#5d6b82"
    BLUE = "#1769e0"
    GREEN = "#008a65"
    RED = "#c73532"
    ORANGE = "#d97706"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("HAT 衰减链 · 功放扫幅 / 扫频")
        self.root.geometry("1460x920")
        self.root.minsize(1180, 760)
        self.root.configure(bg=self.BG)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.event_queue: queue.Queue = queue.Queue()
        self.real_session: Optional[RealInstrumentSession] = None
        self.worker: Optional[SweepWorker] = None
        self.results: list[MeasurementPoint] = []
        self.frequency_results: list[FrequencyMeasurementPoint] = []
        self.current_csv_path: Optional[Path] = None
        self.current_png_path: Optional[Path] = None
        self.current_average_csv_path: Optional[Path] = None
        self.current_average_png_path: Optional[Path] = None
        self.current_average_table_png_path: Optional[Path] = None
        self.current_frequency_csv_path: Optional[Path] = None
        self.current_frequency_png_path: Optional[Path] = None
        self.connected_resources: tuple[str, str] = ("", "")
        self._last_plot_update = 0.0
        self._last_frequency_plot_update = 0.0
        self.active_sweep_type: Optional[str] = None

        self._configure_style()
        self._make_variables()
        self._build_ui()
        self._load_config()
        self._update_estimate()
        self._set_run_state("idle")
        self.root.after(100, self._poll_events)
        self.root.after(450, self.refresh_resources)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("TFrame", background=self.BG)
        style.configure("Card.TFrame", background=self.CARD)
        style.configure("TLabel", background=self.BG, foreground=self.INK, font=("Microsoft YaHei UI", 9))
        style.configure("Card.TLabel", background=self.CARD, foreground=self.INK, font=("Microsoft YaHei UI", 9))
        style.configure("Title.TLabel", background=self.INK, foreground="white", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Subtitle.TLabel", background=self.INK, foreground="#c9d6ea", font=("Microsoft YaHei UI", 9))
        style.configure("Section.TLabel", background=self.CARD, foreground=self.INK, font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("MetricName.TLabel", background=self.CARD, foreground=self.MUTED, font=("Microsoft YaHei UI", 8))
        style.configure("MetricValue.TLabel", background=self.CARD, foreground=self.BLUE, font=("Segoe UI", 16, "bold"))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(12, 7))
        style.configure("Danger.TButton", font=("Microsoft YaHei UI", 9, "bold"), padding=(9, 7))
        style.configure("Treeview", rowheight=25, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TNotebook.Tab", font=("Microsoft YaHei UI", 10), padding=(16, 8))

    def _make_variables(self) -> None:
        self.mode_var = tk.StringVar(value="simulation")
        self.start_var = tk.StringVar(value="580")
        self.stop_var = tk.StringVar(value="300")
        self.step_var = tk.StringVar(value="5")
        self.hard_limit_var = tk.StringVar(value="580")
        self.settle_var = tk.StringVar(value="0.2")
        self.samples_var = tk.StringVar(value="1")
        self.sample_gap_var = tk.StringVar(value="0.05")
        self.rounds_var = tk.StringVar(value="2")
        self.inter_round_pause_var = tk.StringVar(value="0")
        self.attenuation_var = tk.StringVar(value="48")
        self.impedance_var = tk.StringVar(value="50")
        self.timeout_var = tk.StringVar(value="15000")
        self.generator_resource_var = tk.StringVar()
        self.analyzer_resource_var = tk.StringVar()
        self.generator_idn_var = tk.StringVar(value="未连接")
        self.analyzer_idn_var = tk.StringVar(value="未连接")
        self.chain_confirm_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="就绪")
        self.progress_text_var = tk.StringVar(value="0 / 114")
        self.estimate_var = tk.StringVar(value="每轮 57 点，共 114 点")
        self.live_mvpp_var = tk.StringVar(value="—")
        self.live_peak_var = tk.StringVar(value="—")
        self.live_gain_var = tk.StringVar(value="—")
        self.live_power_var = tk.StringVar(value="—")
        self.frequency_start_var = tk.StringVar(value="90")
        self.frequency_stop_var = tk.StringVar(value="110")
        self.frequency_step_var = tk.StringVar(value="1")
        self.frequency_amplitude_var = tk.StringVar(value="300")
        self.frequency_settle_var = tk.StringVar(value="0.2")
        self.frequency_samples_var = tk.StringVar(value="1")
        self.frequency_estimate_var = tk.StringVar(value="21 点")
        self.frequency_progress_text_var = tk.StringVar(value="0 / 21")
        self.live_frequency_var = tk.StringVar(value="—")
        self.frequency_live_peak_var = tk.StringVar(value="—")
        self.frequency_live_gain_var = tk.StringVar(value="—")
        self.frequency_live_power_var = tk.StringVar(value="—")
        self.gen_set_var = tk.StringVar(value=SCPIProfile.generator_set_amplitude)
        self.gen_frequency_var = tk.StringVar(value=SCPIProfile.generator_set_frequency)
        self.gen_on_var = tk.StringVar(value=SCPIProfile.generator_output_on)
        self.gen_off_var = tk.StringVar(value=SCPIProfile.generator_output_off)
        self.sa_trigger_var = tk.StringVar(value=SCPIProfile.analyzer_trigger)
        self.sa_opc_var = tk.StringVar(value=SCPIProfile.analyzer_opc_query)
        self.sa_peak_var = tk.StringVar(value=SCPIProfile.analyzer_peak_search)
        self.sa_y_var = tk.StringVar(value=SCPIProfile.analyzer_peak_y_query)
        self.sa_x_var = tk.StringVar(value=SCPIProfile.analyzer_peak_x_query)
        self.sa_center_var = tk.StringVar(value=SCPIProfile.analyzer_set_center)

        for variable in (
            self.start_var,
            self.stop_var,
            self.step_var,
            self.settle_var,
            self.samples_var,
            self.rounds_var,
            self.inter_round_pause_var,
        ):
            variable.trace_add("write", lambda *_: self._update_estimate())
        for variable in (
            self.frequency_start_var,
            self.frequency_stop_var,
            self.frequency_step_var,
            self.frequency_amplitude_var,
            self.frequency_settle_var,
            self.frequency_samples_var,
        ):
            variable.trace_add("write", lambda *_: self._update_frequency_estimate())
        self.mode_var.trace_add("write", lambda *_: self._mode_changed())

    def _build_ui(self) -> None:
        banner = tk.Frame(self.root, bg=self.INK, height=78)
        banner.pack(fill="x")
        banner.pack_propagate(False)
        title_box = tk.Frame(banner, bg=self.INK)
        title_box.pack(side="left", padx=24, pady=12)
        ttk.Label(title_box, text="HAT 衰减链 · 功放扫幅 / 扫频", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            title_box,
            text="扫幅：每轮最大 → 最小，默认 2 轮 ｜ 扫频：独立设置频率范围 ｜ 外置衰减 48 dB",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(3, 0))
        self.mode_badge = tk.Label(
            banner,
            text="模拟模式",
            bg="#264b77",
            fg="white",
            font=("Microsoft YaHei UI", 10, "bold"),
            padx=14,
            pady=7,
        )
        self.mode_badge.pack(side="right", padx=24)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(10, 6))
        self.measure_tab = ttk.Frame(self.notebook)
        self.frequency_tab = ttk.Frame(self.notebook)
        self.connection_tab = ttk.Frame(self.notebook)
        self.log_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.measure_tab, text="扫幅测量（多轮）")
        self.notebook.add(self.frequency_tab, text="扫频测量")
        self.notebook.add(self.connection_tab, text="仪器连接 / SCPI")
        self.notebook.add(self.log_tab, text="运行日志")

        self._build_measure_tab()
        self._build_frequency_tab()
        self._build_connection_tab()
        self._build_log_tab()

        status_bar = tk.Frame(self.root, bg="#e7edf6", height=28)
        status_bar.pack(fill="x", side="bottom")
        status_bar.pack_propagate(False)
        tk.Label(
            status_bar,
            textvariable=self.status_var,
            bg="#e7edf6",
            fg=self.INK,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=12)
        tk.Label(
            status_bar,
            text="输入按 50 Ω Vpp 换算；输出峰值加回外置衰减",
            bg="#e7edf6",
            fg=self.MUTED,
            font=("Microsoft YaHei UI", 8),
        ).pack(side="right", padx=12)

    def _build_measure_tab(self) -> None:
        self.measure_tab.columnconfigure(1, weight=1)
        self.measure_tab.rowconfigure(0, weight=1)
        self.measure_tab.rowconfigure(1, weight=0)

        controls = ttk.Frame(self.measure_tab, style="Card.TFrame", padding=14)
        controls.grid(row=0, column=0, sticky="nsw", padx=(0, 10), pady=(0, 8))

        ttk.Label(controls, text="多轮扫幅参数", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 9))
        self._labeled_entry(controls, 1, "起点", self.start_var, "mVpp")
        self._labeled_entry(controls, 2, "终点", self.stop_var, "mVpp")
        self._labeled_entry(controls, 3, "步长", self.step_var, "mVpp")
        self._labeled_entry(controls, 4, "硬上限", self.hard_limit_var, "mVpp")
        self._labeled_entry(controls, 5, "稳定等待", self.settle_var, "s")
        self._labeled_entry(controls, 6, "每点读数", self.samples_var, "次")
        self._labeled_entry(controls, 7, "重复轮数", self.rounds_var, "轮")
        self._labeled_entry(controls, 8, "轮间关 RF", self.inter_round_pause_var, "s")

        ttk.Separator(controls).grid(row=9, column=0, columnspan=3, sticky="ew", pady=12)
        ttk.Label(controls, text="换算参数", style="Section.TLabel").grid(row=10, column=0, columnspan=3, sticky="w", pady=(0, 9))
        self._labeled_entry(controls, 11, "外置衰减", self.attenuation_var, "dB")
        self._labeled_entry(controls, 12, "系统阻抗", self.impedance_var, "Ω")
        ttk.Label(
            controls,
            text="当前链路：1 + 2 + 3 + 30 + 12 = 48 dB",
            style="Card.TLabel",
            foreground=self.MUTED,
        ).grid(row=13, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(
            controls,
            textvariable=self.estimate_var,
            style="Card.TLabel",
            foreground=self.BLUE,
        ).grid(row=14, column=0, columnspan=3, sticky="w", pady=(6, 0))

        ttk.Separator(controls).grid(row=15, column=0, columnspan=3, sticky="ew", pady=12)
        confirm = ttk.Checkbutton(
            controls,
            variable=self.chain_confirm_var,
            text="真机：已确认五只衰减器顺序、\n50 Ω 接线及 580 mVpp 首点",
        )
        confirm.grid(row=16, column=0, columnspan=3, sticky="w")
        ttk.Label(
            controls,
            text="稳定等待最小 0.1 s，建议 0.2 s；\n每轮首点固定至少等待 0.5 s。",
            style="Card.TLabel",
            foreground=self.MUTED,
        ).grid(row=17, column=0, columnspan=3, sticky="w", pady=(12, 0))

        plot_card = ttk.Frame(self.measure_tab, style="Card.TFrame", padding=8)
        plot_card.grid(row=0, column=1, sticky="nsew", pady=(0, 8))
        plot_card.columnconfigure(0, weight=1)
        plot_card.rowconfigure(2, weight=1)

        metrics = ttk.Frame(plot_card, style="Card.TFrame")
        metrics.grid(row=0, column=0, sticky="ew", padx=6, pady=(2, 5))
        for col in range(4):
            metrics.columnconfigure(col, weight=1)
        self._metric(metrics, 0, "当前轮次 / 幅度", self.live_mvpp_var)
        self._metric(metrics, 1, "频谱仪峰值", self.live_peak_var)
        self._metric(metrics, 2, "校正后增益", self.live_gain_var)
        self._metric(metrics, 3, "校正后输出功率", self.live_power_var)

        runbar = ttk.Frame(plot_card, style="Card.TFrame", padding=(6, 2))
        runbar.grid(row=1, column=0, sticky="ew", pady=(0, 3))
        runbar.columnconfigure(4, weight=1)
        self.start_button = ttk.Button(runbar, text="开始多轮扫幅", style="Primary.TButton", command=self.start_sweep)
        self.start_button.grid(row=0, column=0, sticky="w", padx=(0, 5))
        self.pause_button = ttk.Button(runbar, text="暂停", command=self.toggle_pause)
        self.pause_button.grid(row=0, column=1, sticky="w", padx=3)
        self.stop_button = ttk.Button(runbar, text="停止", command=self.stop_sweep)
        self.stop_button.grid(row=0, column=2, sticky="w", padx=3)
        self.emergency_button = ttk.Button(
            runbar, text="RF 紧急关闭", style="Danger.TButton", command=self.emergency_off
        )
        self.emergency_button.grid(row=0, column=3, sticky="w", padx=(3, 10))
        progress_box = ttk.Frame(runbar, style="Card.TFrame")
        progress_box.grid(row=0, column=4, sticky="ew", padx=4)
        progress_box.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_box, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_box, textvariable=self.progress_text_var, style="Card.TLabel").grid(row=0, column=1, padx=(7, 0))
        ttk.Button(runbar, text="另存", command=self.export_results).grid(row=0, column=5, padx=(10, 3))
        ttk.Button(runbar, text="数据目录", command=self.open_measurement_dir).grid(row=0, column=6, padx=(3, 0))

        self.figure = Figure(figsize=(9.0, 5.5), dpi=100, facecolor=self.CARD, constrained_layout=True)
        self.ax_level = self.figure.add_subplot(211)
        self.ax_gain = self.figure.add_subplot(212, sharex=self.ax_level)
        self.canvas = FigureCanvasTkAgg(self.figure, master=plot_card)
        self.canvas.get_tk_widget().grid(row=2, column=0, sticky="nsew")
        toolbar_frame = ttk.Frame(plot_card, style="Card.TFrame")
        toolbar_frame.grid(row=3, column=0, sticky="ew")
        toolbar = NavigationToolbar2Tk(self.canvas, toolbar_frame, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(side="left")
        self._draw_plot(force=True)

        table_card = ttk.Frame(self.measure_tab, style="Card.TFrame", padding=(8, 6))
        table_card.grid(row=1, column=0, columnspan=2, sticky="nsew")
        table_card.columnconfigure(0, weight=1)
        table_card.rowconfigure(1, weight=1)
        ttk.Label(table_card, text="采集数据（按实际测量顺序）", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 5))
        columns = ("n", "round", "mvpp", "freq", "peak", "corrected", "input", "gain", "power")
        self.tree = ttk.Treeview(table_card, columns=columns, show="headings", height=7)
        headings = {
            "n": ("#", 48),
            "round": ("轮次", 58),
            "mvpp": ("设定 mVpp", 95),
            "freq": ("峰值 MHz", 105),
            "peak": ("DSA 峰值 dBm", 115),
            "corrected": ("校正输出 dBm", 120),
            "input": ("输入 dBm", 100),
            "gain": ("增益 dB", 90),
            "power": ("输出 W", 95),
        }
        for key, (text, width) in headings.items():
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, minwidth=55, anchor="center", stretch=True)
        yscroll = ttk.Scrollbar(table_card, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(table_card, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        yscroll.grid(row=1, column=1, sticky="ns")
        xscroll.grid(row=2, column=0, sticky="ew")

    def _build_frequency_tab(self) -> None:
        self.frequency_tab.columnconfigure(1, weight=1)
        self.frequency_tab.rowconfigure(0, weight=1)
        self.frequency_tab.rowconfigure(1, weight=0)

        controls = ttk.Frame(self.frequency_tab, style="Card.TFrame", padding=14)
        controls.grid(row=0, column=0, sticky="nsw", padx=(0, 10), pady=(0, 8))
        ttk.Label(controls, text="独立扫频参数", style="Section.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 9)
        )
        self._labeled_entry(controls, 1, "起始频率", self.frequency_start_var, "MHz")
        self._labeled_entry(controls, 2, "终止频率", self.frequency_stop_var, "MHz")
        self._labeled_entry(controls, 3, "频率步长", self.frequency_step_var, "MHz")
        self._labeled_entry(controls, 4, "固定幅度", self.frequency_amplitude_var, "mVpp")
        self._labeled_entry(controls, 5, "稳定等待", self.frequency_settle_var, "s")
        self._labeled_entry(controls, 6, "每点读数", self.frequency_samples_var, "次")
        ttk.Separator(controls).grid(row=7, column=0, columnspan=3, sticky="ew", pady=12)
        ttk.Label(controls, text="换算参数", style="Section.TLabel").grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(0, 9)
        )
        self._labeled_entry(controls, 9, "外置衰减", self.attenuation_var, "dB")
        self._labeled_entry(controls, 10, "系统阻抗", self.impedance_var, "Ω")
        ttk.Label(
            controls,
            textvariable=self.frequency_estimate_var,
            style="Card.TLabel",
            foreground=self.BLUE,
        ).grid(row=11, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(
            controls,
            text="每个频点同时设置 DG 频率和 DSA 中心频率；\n"
            "稳定等待最小 0.1 s，首点至少 0.5 s。",
            style="Card.TLabel",
            foreground=self.MUTED,
        ).grid(row=12, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Separator(controls).grid(row=13, column=0, columnspan=3, sticky="ew", pady=12)
        ttk.Checkbutton(
            controls,
            variable=self.chain_confirm_var,
            text="真机：已确认五只衰减器顺序、\n50 Ω 接线及固定幅度",
        ).grid(row=14, column=0, columnspan=3, sticky="w")

        plot_card = ttk.Frame(self.frequency_tab, style="Card.TFrame", padding=8)
        plot_card.grid(row=0, column=1, sticky="nsew", pady=(0, 8))
        plot_card.columnconfigure(0, weight=1)
        plot_card.rowconfigure(2, weight=1)

        metrics = ttk.Frame(plot_card, style="Card.TFrame")
        metrics.grid(row=0, column=0, sticky="ew", padx=6, pady=(2, 5))
        for col in range(4):
            metrics.columnconfigure(col, weight=1)
        self._metric(metrics, 0, "当前频率", self.live_frequency_var)
        self._metric(metrics, 1, "频谱仪峰值", self.frequency_live_peak_var)
        self._metric(metrics, 2, "校正后增益", self.frequency_live_gain_var)
        self._metric(metrics, 3, "校正后输出功率", self.frequency_live_power_var)

        runbar = ttk.Frame(plot_card, style="Card.TFrame", padding=(6, 2))
        runbar.grid(row=1, column=0, sticky="ew", pady=(0, 3))
        runbar.columnconfigure(4, weight=1)
        self.frequency_start_button = ttk.Button(
            runbar, text="开始扫频", style="Primary.TButton", command=self.start_frequency_sweep
        )
        self.frequency_start_button.grid(row=0, column=0, sticky="w", padx=(0, 5))
        self.frequency_pause_button = ttk.Button(runbar, text="暂停", command=self.toggle_pause)
        self.frequency_pause_button.grid(row=0, column=1, sticky="w", padx=3)
        self.frequency_stop_button = ttk.Button(runbar, text="停止", command=self.stop_sweep)
        self.frequency_stop_button.grid(row=0, column=2, sticky="w", padx=3)
        self.frequency_emergency_button = ttk.Button(
            runbar, text="RF 紧急关闭", style="Danger.TButton", command=self.emergency_off
        )
        self.frequency_emergency_button.grid(row=0, column=3, sticky="w", padx=(3, 10))
        progress_box = ttk.Frame(runbar, style="Card.TFrame")
        progress_box.grid(row=0, column=4, sticky="ew", padx=4)
        progress_box.columnconfigure(0, weight=1)
        self.frequency_progress = ttk.Progressbar(progress_box, mode="determinate", maximum=100)
        self.frequency_progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(
            progress_box, textvariable=self.frequency_progress_text_var, style="Card.TLabel"
        ).grid(row=0, column=1, padx=(7, 0))
        ttk.Button(runbar, text="另存", command=self.export_frequency_results).grid(
            row=0, column=5, padx=(10, 3)
        )
        ttk.Button(runbar, text="数据目录", command=self.open_measurement_dir).grid(
            row=0, column=6, padx=(3, 0)
        )

        self.frequency_figure = Figure(
            figsize=(9.0, 5.5), dpi=100, facecolor=self.CARD, constrained_layout=True
        )
        self.frequency_ax_level = self.frequency_figure.add_subplot(211)
        self.frequency_ax_gain = self.frequency_figure.add_subplot(212, sharex=self.frequency_ax_level)
        self.frequency_canvas = FigureCanvasTkAgg(self.frequency_figure, master=plot_card)
        self.frequency_canvas.get_tk_widget().grid(row=2, column=0, sticky="nsew")
        toolbar_frame = ttk.Frame(plot_card, style="Card.TFrame")
        toolbar_frame.grid(row=3, column=0, sticky="ew")
        toolbar = NavigationToolbar2Tk(self.frequency_canvas, toolbar_frame, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(side="left")
        self._draw_frequency_plot(force=True)

        table_card = ttk.Frame(self.frequency_tab, style="Card.TFrame", padding=(8, 6))
        table_card.grid(row=1, column=0, columnspan=2, sticky="nsew")
        table_card.columnconfigure(0, weight=1)
        table_card.rowconfigure(1, weight=1)
        ttk.Label(table_card, text="扫频采集数据", style="Section.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 5)
        )
        columns = ("n", "setfreq", "peakfreq", "mvpp", "peak", "corrected", "gain", "power")
        self.frequency_tree = ttk.Treeview(table_card, columns=columns, show="headings", height=7)
        headings = {
            "n": ("#", 48),
            "setfreq": ("设定 MHz", 95),
            "peakfreq": ("峰值 MHz", 100),
            "mvpp": ("固定 mVpp", 90),
            "peak": ("DSA 峰值 dBm", 110),
            "corrected": ("校正输出 dBm", 115),
            "gain": ("增益 dB", 90),
            "power": ("输出 W", 90),
        }
        for key, (text, width) in headings.items():
            self.frequency_tree.heading(key, text=text)
            self.frequency_tree.column(key, width=width, minwidth=55, anchor="center", stretch=True)
        yscroll = ttk.Scrollbar(table_card, orient="vertical", command=self.frequency_tree.yview)
        xscroll = ttk.Scrollbar(table_card, orient="horizontal", command=self.frequency_tree.xview)
        self.frequency_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.frequency_tree.grid(row=1, column=0, sticky="nsew")
        yscroll.grid(row=1, column=1, sticky="ns")
        xscroll.grid(row=2, column=0, sticky="ew")

    def _build_connection_tab(self) -> None:
        outer = ttk.Frame(self.connection_tab, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(1, weight=1)

        connection = ttk.Frame(outer, style="Card.TFrame", padding=16)
        connection.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        connection.columnconfigure(1, weight=1)
        ttk.Label(connection, text="连接方式", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        mode_box = ttk.Frame(connection, style="Card.TFrame")
        mode_box.grid(row=0, column=1, sticky="w", padx=(16, 0))
        ttk.Radiobutton(mode_box, text="模拟仪器（先验流程）", value="simulation", variable=self.mode_var).pack(side="left")
        ttk.Radiobutton(mode_box, text="真实 VISA 仪器", value="visa", variable=self.mode_var).pack(side="left", padx=(18, 0))

        ttk.Label(connection, text="信号发生器 VISA", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=(12, 4))
        self.generator_combo = ttk.Combobox(connection, textvariable=self.generator_resource_var, width=75)
        self.generator_combo.grid(row=1, column=1, sticky="ew", padx=(16, 8), pady=(12, 4))
        ttk.Label(connection, text="频谱仪 VISA", style="Card.TLabel").grid(row=2, column=0, sticky="w", pady=4)
        self.analyzer_combo = ttk.Combobox(connection, textvariable=self.analyzer_resource_var, width=75)
        self.analyzer_combo.grid(row=2, column=1, sticky="ew", padx=(16, 8), pady=4)
        resource_buttons = ttk.Frame(connection, style="Card.TFrame")
        resource_buttons.grid(row=1, column=2, rowspan=2, sticky="ns")
        ttk.Button(resource_buttons, text="扫描 VISA", command=self.refresh_resources).pack(fill="x", pady=(10, 3))
        self.connect_button = ttk.Button(resource_buttons, text="连接并识别", command=self.connect_instruments)
        self.connect_button.pack(fill="x", pady=3)

        ttk.Label(connection, text="发生器 ID", style="Card.TLabel").grid(row=3, column=0, sticky="nw", pady=(10, 2))
        ttk.Label(connection, textvariable=self.generator_idn_var, style="Card.TLabel", foreground=self.BLUE, wraplength=850).grid(
            row=3, column=1, columnspan=2, sticky="w", padx=(16, 0), pady=(10, 2)
        )
        ttk.Label(connection, text="频谱仪 ID", style="Card.TLabel").grid(row=4, column=0, sticky="nw", pady=2)
        ttk.Label(connection, textvariable=self.analyzer_idn_var, style="Card.TLabel", foreground=self.BLUE, wraplength=850).grid(
            row=4, column=1, columnspan=2, sticky="w", padx=(16, 0), pady=2
        )
        ttk.Label(
            connection,
            text="DG5352 Pro：接后面板 USB DEVICE（USB-B），不要接正面 USB HOST。电脑仅有 Type-C 时用 USB-C→USB-B 数据线。",
            style="Card.TLabel",
            foreground=self.MUTED,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))

        generator_card = ttk.Frame(outer, style="Card.TFrame", padding=16)
        generator_card.grid(row=1, column=0, sticky="nsew", padx=(0, 5))
        analyzer_card = ttk.Frame(outer, style="Card.TFrame", padding=16)
        analyzer_card.grid(row=1, column=1, sticky="nsew", padx=(5, 0))
        generator_card.columnconfigure(1, weight=1)
        analyzer_card.columnconfigure(1, weight=1)

        ttk.Label(generator_card, text="信号发生器 SCPI", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            generator_card,
            text="已按照片中的 RIGOL DG5352 Pro（CH1、50 Ω、Vpp）配置。若以后换型号，只需改这里。",
            style="Card.TLabel",
            foreground=self.MUTED,
            wraplength=550,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 12))
        ttk.Label(generator_card, text="扫描前命令（每行一条）", style="Card.TLabel").grid(row=2, column=0, columnspan=2, sticky="w")
        self.gen_prepare_text = tk.Text(generator_card, height=5, wrap="none", font=("Consolas", 9), relief="solid", bd=1)
        self.gen_prepare_text.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(3, 8))
        self.gen_prepare_text.insert("1.0", SCPIProfile.generator_prepare)
        self._scpi_entry(generator_card, 4, "设幅度", self.gen_set_var)
        self._scpi_entry(generator_card, 5, "设频率", self.gen_frequency_var)
        self._scpi_entry(generator_card, 6, "RF 开", self.gen_on_var)
        self._scpi_entry(generator_card, 7, "RF 关", self.gen_off_var)
        ttk.Label(
            generator_card,
            text="幅度模板可用 {volts}/{mvpp}；频率模板可用 {hz}/{mhz}。",
            style="Card.TLabel",
            foreground=self.MUTED,
            wraplength=540,
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(8, 0))

        ttk.Label(analyzer_card, text="RIGOL DSA815 SCPI", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            analyzer_card,
            text="每个幅度点触发一次单扫，等待 *OPC?，然后峰值搜索并读取光标 X/Y。",
            style="Card.TLabel",
            foreground=self.MUTED,
            wraplength=550,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 12))
        ttk.Label(analyzer_card, text="扫描前命令（每行一条）", style="Card.TLabel").grid(row=2, column=0, columnspan=2, sticky="w")
        self.sa_prepare_text = tk.Text(analyzer_card, height=5, wrap="none", font=("Consolas", 9), relief="solid", bd=1)
        self.sa_prepare_text.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(3, 8))
        self.sa_prepare_text.insert("1.0", SCPIProfile.analyzer_prepare)
        self._scpi_entry(analyzer_card, 4, "设中心频率", self.sa_center_var)
        self._scpi_entry(analyzer_card, 5, "触发单扫", self.sa_trigger_var)
        self._scpi_entry(analyzer_card, 6, "等待完成", self.sa_opc_var)
        self._scpi_entry(analyzer_card, 7, "峰值搜索", self.sa_peak_var)
        self._scpi_entry(analyzer_card, 8, "读取幅度", self.sa_y_var)
        self._scpi_entry(analyzer_card, 9, "读取频率", self.sa_x_var)
        ttk.Label(analyzer_card, text="结束恢复命令", style="Card.TLabel").grid(row=10, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.sa_restore_text = tk.Text(analyzer_card, height=3, wrap="none", font=("Consolas", 9), relief="solid", bd=1)
        self.sa_restore_text.grid(row=11, column=0, columnspan=2, sticky="ew", pady=(3, 0))
        self.sa_restore_text.insert("1.0", SCPIProfile.analyzer_restore)

    def _build_log_tab(self) -> None:
        frame = ttk.Frame(self.log_tab, padding=10)
        frame.pack(fill="both", expand=True)
        top = ttk.Frame(frame)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="SCPI 与运行日志", style="Section.TLabel").pack(side="left")
        ttk.Button(top, text="清空日志", command=lambda: self.log_text.delete("1.0", "end")).pack(side="right")
        self.log_text = tk.Text(
            frame,
            wrap="word",
            font=("Consolas", 9),
            bg="#101827",
            fg="#dce7f8",
            insertbackground="white",
            padx=10,
            pady=10,
        )
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _labeled_entry(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, unit: str) -> None:
        ttk.Label(parent, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=variable, width=11, justify="right").grid(row=row, column=1, sticky="ew", padx=(12, 6), pady=3)
        ttk.Label(parent, text=unit, style="Card.TLabel", foreground=self.MUTED).grid(row=row, column=2, sticky="w", pady=3)

    def _scpi_entry(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable, font=("Consolas", 9)).grid(row=row, column=1, sticky="ew", padx=(10, 0), pady=4)

    def _metric(self, parent: ttk.Frame, column: int, name: str, variable: tk.StringVar) -> None:
        card = ttk.Frame(parent, style="Card.TFrame", padding=(10, 4))
        card.grid(row=0, column=column, sticky="ew", padx=3)
        ttk.Label(card, text=name, style="MetricName.TLabel").pack(anchor="w")
        ttk.Label(card, textvariable=variable, style="MetricValue.TLabel").pack(anchor="w")

    def log(self, message: str) -> None:
        if threading.current_thread() is threading.main_thread():
            self._append_log(message)
        else:
            self.event_queue.put(("log", message))

    def _append_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.log_text.insert("end", f"[{stamp}] {message}\n")
        self.log_text.see("end")

    def _mode_changed(self) -> None:
        is_sim = self.mode_var.get() == "simulation"
        self.mode_badge.configure(
            text="模拟模式" if is_sim else "真实 VISA",
            bg="#264b77" if is_sim else self.GREEN,
        )
        self.status_var.set("模拟模式：不会向真实仪器发送命令。" if is_sim else "真实 VISA 模式：启动前请先连接并识别两台仪器。")

    def _update_estimate(self) -> None:
        if not hasattr(self, "estimate_var"):
            return
        try:
            points = build_descending_points(
                float(self.start_var.get()), float(self.stop_var.get()), float(self.step_var.get())
            )
            settle = max(0.0, float(self.settle_var.get()))
            samples = max(1, int(self.samples_var.get()))
            rounds = max(1, int(self.rounds_var.get()))
            inter_round_pause = max(0.0, float(self.inter_round_pause_var.get()))
            total = len(points) * rounds
            rough_seconds = total * (settle + 0.25 * samples) + max(0, rounds - 1) * inter_round_pause
            self.estimate_var.set(
                f"每轮 {len(points)} 点 × {rounds} 轮 = {total} 点；约 "
                f"{self._format_duration(rough_seconds)} + 仪器扫描时间"
            )
            if not self.worker or not self.worker.is_alive():
                self.progress_text_var.set(f"0 / {total}")
        except (ValueError, tk.TclError):
            self.estimate_var.set("参数尚未完整")

    def _update_frequency_estimate(self) -> None:
        if not hasattr(self, "frequency_estimate_var"):
            return
        try:
            points = build_frequency_points_mhz(
                float(self.frequency_start_var.get()),
                float(self.frequency_stop_var.get()),
                float(self.frequency_step_var.get()),
            )
            settle = max(0.0, float(self.frequency_settle_var.get()))
            samples = max(1, int(self.frequency_samples_var.get()))
            rough_seconds = len(points) * (settle + 0.25 * samples)
            self.frequency_estimate_var.set(
                f"{len(points)} 个频点；约 {self._format_duration(rough_seconds)} + 仪器扫描时间"
            )
            if not self.worker or not self.worker.is_alive():
                self.frequency_progress_text_var.set(f"0 / {len(points)}")
        except (ValueError, tk.TclError):
            self.frequency_estimate_var.set("参数尚未完整")

    @staticmethod
    def _format_duration(seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:.0f} 秒"
        return f"{seconds / 60.0:.1f} 分钟"

    def _read_settings(self) -> SweepSettings:
        try:
            settings = SweepSettings(
                start_mvpp=float(self.start_var.get()),
                stop_mvpp=float(self.stop_var.get()),
                step_mvpp=float(self.step_var.get()),
                hard_limit_mvpp=float(self.hard_limit_var.get()),
                settle_s=float(self.settle_var.get()),
                samples_per_point=int(self.samples_var.get()),
                sample_gap_s=float(self.sample_gap_var.get()),
                rounds=int(self.rounds_var.get()),
                inter_round_pause_s=float(self.inter_round_pause_var.get()),
                attenuation_db=float(self.attenuation_var.get()),
                impedance_ohm=float(self.impedance_var.get()),
                visa_timeout_ms=int(self.timeout_var.get()),
            )
        except ValueError as exc:
            raise ValueError("扫描参数中有无法解析的数字。") from exc

        points = build_descending_points(settings.start_mvpp, settings.stop_mvpp, settings.step_mvpp)
        if max(points) > settings.hard_limit_mvpp + 1e-12:
            raise ValueError(f"扫描起点 {max(points):g} mVpp 超过硬上限 {settings.hard_limit_mvpp:g} mVpp。")
        if settings.hard_limit_mvpp <= 0:
            raise ValueError("硬上限必须大于 0 mVpp。")
        if not 0.1 <= settings.settle_s <= 60:
            raise ValueError("稳定等待必须在 0.1–60 秒之间；建议使用 0.2 秒。")
        if not 1 <= settings.samples_per_point <= 100:
            raise ValueError("每点读数次数必须在 1–100 之间。")
        if not 0 <= settings.sample_gap_s <= 10:
            raise ValueError("重复读数间隔必须在 0–10 秒之间。")
        if not 1 <= settings.rounds <= 20:
            raise ValueError("扫幅轮数必须在 1–20 轮之间。")
        if not 0 <= settings.inter_round_pause_s <= 3600:
            raise ValueError("轮间关 RF 时间必须在 0–3600 秒之间。")
        if settings.attenuation_db < 0:
            raise ValueError("外置衰减不能为负数。")
        if settings.impedance_ohm <= 0:
            raise ValueError("系统阻抗必须大于 0 Ω。")
        if not 1000 <= settings.visa_timeout_ms <= 120000:
            raise ValueError("VISA 超时必须在 1000–120000 ms 之间。")
        return settings

    def _read_frequency_settings(self) -> FrequencySweepSettings:
        try:
            settings = FrequencySweepSettings(
                start_mhz=float(self.frequency_start_var.get()),
                stop_mhz=float(self.frequency_stop_var.get()),
                step_mhz=float(self.frequency_step_var.get()),
                fixed_mvpp=float(self.frequency_amplitude_var.get()),
                hard_limit_mvpp=float(self.hard_limit_var.get()),
                settle_s=float(self.frequency_settle_var.get()),
                samples_per_point=int(self.frequency_samples_var.get()),
                sample_gap_s=float(self.sample_gap_var.get()),
                attenuation_db=float(self.attenuation_var.get()),
                impedance_ohm=float(self.impedance_var.get()),
                visa_timeout_ms=int(self.timeout_var.get()),
            )
        except ValueError as exc:
            raise ValueError("扫频参数中有无法解析的数字。") from exc

        points = build_frequency_points_mhz(settings.start_mhz, settings.stop_mhz, settings.step_mhz)
        if min(points) <= 0 or max(points) > 350:
            raise ValueError("DG5352 Pro 扫频范围必须大于 0 且不超过 350 MHz。")
        if settings.fixed_mvpp <= 0:
            raise ValueError("扫频固定幅度必须大于 0 mVpp。")
        if settings.fixed_mvpp > settings.hard_limit_mvpp + 1e-12:
            raise ValueError(
                f"扫频固定幅度 {settings.fixed_mvpp:g} mVpp 超过硬上限 "
                f"{settings.hard_limit_mvpp:g} mVpp。"
            )
        if not 0.1 <= settings.settle_s <= 60:
            raise ValueError("扫频稳定等待必须在 0.1–60 秒之间；建议使用 0.2 秒。")
        if not 1 <= settings.samples_per_point <= 100:
            raise ValueError("扫频每点读数次数必须在 1–100 之间。")
        if settings.attenuation_db < 0 or settings.impedance_ohm <= 0:
            raise ValueError("外置衰减不能为负，系统阻抗必须大于 0 Ω。")
        return settings

    def _read_profile(self) -> SCPIProfile:
        profile = SCPIProfile(
            generator_prepare=self.gen_prepare_text.get("1.0", "end").strip(),
            generator_set_amplitude=self.gen_set_var.get().strip(),
            generator_set_frequency=self.gen_frequency_var.get().strip(),
            generator_output_on=self.gen_on_var.get().strip(),
            generator_output_off=self.gen_off_var.get().strip(),
            analyzer_prepare=self.sa_prepare_text.get("1.0", "end").strip(),
            analyzer_set_center=self.sa_center_var.get().strip(),
            analyzer_trigger=self.sa_trigger_var.get().strip(),
            analyzer_opc_query=self.sa_opc_var.get().strip(),
            analyzer_peak_search=self.sa_peak_var.get().strip(),
            analyzer_peak_y_query=self.sa_y_var.get().strip(),
            analyzer_peak_x_query=self.sa_x_var.get().strip(),
            analyzer_restore=self.sa_restore_text.get("1.0", "end").strip(),
        )
        required = {
            "发生器幅度命令": profile.generator_set_amplitude,
            "发生器频率命令": profile.generator_set_frequency,
            "发生器 RF 开命令": profile.generator_output_on,
            "发生器 RF 关命令": profile.generator_output_off,
            "频谱仪峰值 Y 查询": profile.analyzer_peak_y_query,
            "频谱仪峰值 X 查询": profile.analyzer_peak_x_query,
            "频谱仪中心频率命令": profile.analyzer_set_center,
        }
        empty = [name for name, value in required.items() if not value]
        if empty:
            raise ValueError("以下 SCPI 配置不能为空：" + "、".join(empty))
        try:
            profile.generator_set_amplitude.format(volts="0.58", mvpp="580")
        except (KeyError, ValueError) as exc:
            raise ValueError("幅度命令模板无效；只允许使用 {volts} 或 {mvpp}。") from exc
        try:
            profile.generator_set_frequency.format(hz="100000000", mhz="100")
            profile.analyzer_set_center.format(hz="100000000", mhz="100")
        except (KeyError, ValueError) as exc:
            raise ValueError("频率命令模板无效；只允许使用 {hz} 或 {mhz}。") from exc
        return profile

    def refresh_resources(self) -> None:
        if pyvisa is None:
            messagebox.showerror("缺少依赖", "未安装 PyVISA。请运行 install_dependencies.cmd。")
            return
        self.status_var.set("正在扫描 VISA 资源…")
        self.connect_button.configure(state="disabled")

        def task() -> None:
            try:
                manager = pyvisa.ResourceManager()
                try:
                    resources = list(manager.list_resources())
                    backend = str(manager.visalib)
                    probes: list[dict[str, str]] = []
                    for resource in resources:
                        instrument = None
                        try:
                            instrument = manager.open_resource(resource)
                            instrument.timeout = 2500
                            instrument.read_termination = "\n"
                            instrument.write_termination = "\n"
                            identity = instrument.query("*IDN?").strip()
                            probes.append({"resource": resource, "idn": identity, "error": ""})
                        except Exception as probe_exc:
                            probes.append(
                                {
                                    "resource": resource,
                                    "idn": "",
                                    "error": f"{type(probe_exc).__name__}: {probe_exc}",
                                }
                            )
                        finally:
                            if instrument is not None:
                                try:
                                    instrument.close()
                                except Exception:
                                    pass
                finally:
                    manager.close()
                self.event_queue.put(
                    ("resources", {"resources": resources, "backend": backend, "probes": probes})
                )
            except Exception as exc:
                self.event_queue.put(("connect_error", f"扫描 VISA 失败：{exc}"))

        threading.Thread(target=task, daemon=True).start()

    def connect_instruments(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("正在扫描", "扫描运行中，不能重新连接仪器。")
            return
        try:
            settings = self._read_settings()
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        generator_resource = self.generator_resource_var.get().strip()
        analyzer_resource = self.analyzer_resource_var.get().strip()
        self.connect_button.configure(state="disabled")
        self.status_var.set("正在连接并读取 *IDN?…")
        self.log(f"连接请求：GEN={generator_resource}；DSA={analyzer_resource}")

        def task() -> None:
            session = RealInstrumentSession(self.log)
            try:
                gen_idn, sa_idn = session.connect(generator_resource, analyzer_resource, settings.visa_timeout_ms)
                self.event_queue.put(
                    (
                        "connected",
                        {
                            "session": session,
                            "gen_idn": gen_idn,
                            "sa_idn": sa_idn,
                            "resources": (generator_resource, analyzer_resource),
                        },
                    )
                )
            except Exception as exc:
                session.close()
                self.event_queue.put(("connect_error", f"连接失败：{exc}"))

        threading.Thread(target=task, daemon=True).start()

    def start_sweep(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            settings = self._read_settings()
            profile = self._read_profile()
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            return

        simulated = self.mode_var.get() == "simulation"
        if simulated:
            session: Any = SimulatedInstrumentSession(self.log)
        else:
            selected = (self.generator_resource_var.get().strip(), self.analyzer_resource_var.get().strip())
            if self.real_session is None or not self.real_session.connected or selected != self.connected_resources:
                messagebox.showerror("尚未连接", "请先到“仪器连接 / SCPI”页选择真实 VISA 仪器，并点击“连接并识别”。")
                self.notebook.select(self.connection_tab)
                return
            if not self.chain_confirm_var.get():
                messagebox.showwarning(
                    "需要接线确认",
                    "请确认功放 → 1 dB → 2 dB → 3 dB → 30 dB → 12 dB → DSA815，\n并勾选已确认 580 mVpp 首点。",
                )
                return
            session = self.real_session

        points = build_descending_points(settings.start_mvpp, settings.stop_mvpp, settings.step_mvpp)
        if not messagebox.askokcancel(
            "开始多轮扫幅",
            f"模式：{'模拟仪器' if simulated else '真实 VISA'}\n"
            f"每轮：{points[0]:g} → {points[-1]:g} mVpp\n"
            f"步长：{settings.step_mvpp:g} mVpp，{settings.rounds} 轮，"
            f"共 {len(points) * settings.rounds} 点\n"
            f"外置衰减：{settings.attenuation_db:g} dB\n\n"
            "每轮都从最大幅度向最小幅度扫描；轮间先关闭 RF。",
        ):
            return

        self._save_config()
        self.results.clear()
        self.current_csv_path = None
        self.current_png_path = None
        self.current_average_csv_path = None
        self.current_average_png_path = None
        self.current_average_table_png_path = None
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._draw_plot(force=True)
        self.progress.configure(value=0)
        self.progress_text_var.set(f"0 / {len(points) * settings.rounds}")
        self.active_sweep_type = "amplitude"
        self.worker = SweepWorker(session, settings, profile, self.event_queue, simulated)
        self.worker.start()
        self._set_run_state("running")

    def start_frequency_sweep(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            settings = self._read_frequency_settings()
            profile = self._read_profile()
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            return

        simulated = self.mode_var.get() == "simulation"
        if simulated:
            session: Any = SimulatedInstrumentSession(self.log)
        else:
            selected = (self.generator_resource_var.get().strip(), self.analyzer_resource_var.get().strip())
            if self.real_session is None or not self.real_session.connected or selected != self.connected_resources:
                messagebox.showerror("尚未连接", "两台真实 VISA 仪器尚未连接。")
                self.notebook.select(self.connection_tab)
                return
            if not self.chain_confirm_var.get():
                messagebox.showwarning(
                    "需要接线确认",
                    "请确认功放 → 1 dB → 2 dB → 3 dB → 30 dB → 12 dB → DSA815，"
                    "并确认固定幅度。",
                )
                return
            session = self.real_session

        points = build_frequency_points_mhz(
            settings.start_mhz, settings.stop_mhz, settings.step_mhz
        )
        if not messagebox.askokcancel(
            "开始扫频",
            f"模式：{'模拟仪器' if simulated else '真实 VISA'}\n"
            f"频率：{points[0]:g} → {points[-1]:g} MHz\n"
            f"步长：{settings.step_mhz:g} MHz，共 {len(points)} 点\n"
            f"固定幅度：{settings.fixed_mvpp:g} mVpp\n"
            f"外置衰减：{settings.attenuation_db:g} dB\n\n"
            "每个点会同步设置发生器频率和频谱仪中心频率。",
        ):
            return

        self._save_config()
        self.frequency_results.clear()
        self.current_frequency_csv_path = None
        self.current_frequency_png_path = None
        for item in self.frequency_tree.get_children():
            self.frequency_tree.delete(item)
        self._draw_frequency_plot(force=True)
        self.frequency_progress.configure(value=0)
        self.frequency_progress_text_var.set(f"0 / {len(points)}")
        self.active_sweep_type = "frequency"
        self.worker = FrequencySweepWorker(session, settings, profile, self.event_queue, simulated)
        self.worker.start()
        self._set_run_state("running")

    def toggle_pause(self) -> None:
        if not self.worker or not self.worker.is_alive():
            return
        paused = not self.worker.pause_event.is_set()
        self.worker.set_paused(paused)
        self.pause_button.configure(text="继续" if paused else "暂停")
        self.frequency_pause_button.configure(text="继续" if paused else "暂停")
        self.status_var.set("已请求暂停；完成当前仪器通信后生效。" if paused else "正在继续扫描…")

    def stop_sweep(self) -> None:
        if self.worker and self.worker.is_alive():
            self.worker.request_stop()
            self.status_var.set("正在停止；将关闭 RF 并保留已采数据…")
            self.stop_button.configure(state="disabled")
            self.frequency_stop_button.configure(state="disabled")

    def emergency_off(self) -> None:
        if self.worker and self.worker.is_alive():
            self.worker.request_stop()
        self.status_var.set("正在执行 RF 紧急关闭…")
        profile = self._read_profile_safely()

        def task() -> None:
            try:
                if self.real_session and self.real_session.connected:
                    self.real_session.safe_output_off(profile)
                self.event_queue.put(("emergency_done", None))
            except Exception as exc:
                self.event_queue.put(("emergency_error", str(exc)))

        threading.Thread(target=task, daemon=True).start()

    def _read_profile_safely(self) -> SCPIProfile:
        try:
            return self._read_profile()
        except Exception:
            return SCPIProfile()

    def _set_run_state(self, state: str) -> None:
        running = state == "running"
        self.start_button.configure(state="disabled" if running else "normal")
        self.frequency_start_button.configure(state="disabled" if running else "normal")
        self.pause_button.configure(state="normal" if running else "disabled", text="暂停")
        self.frequency_pause_button.configure(state="normal" if running else "disabled", text="暂停")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.frequency_stop_button.configure(state="normal" if running else "disabled")
        self.connect_button.configure(state="disabled" if running else "normal")

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.event_queue.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _handle_event(self, kind: str, payload: Any) -> None:
        if kind == "log":
            self._append_log(str(payload))
        elif kind == "resources":
            resources = payload["resources"]
            probes = payload.get("probes", [])
            identities = {
                probe["resource"]: probe["idn"]
                for probe in probes
                if probe.get("idn")
            }
            generators, analyzers = classify_visa_resources(identities)
            self.generator_combo["values"] = resources
            self.analyzer_combo["values"] = resources
            self.generator_resource_var.set(generators[0] if generators else "")
            self.analyzer_resource_var.set(analyzers[0] if analyzers else "")
            self.generator_idn_var.set(
                f"已发现：{identities[generators[0]]}" if generators else "未发现 DG 系列发生器"
            )
            self.analyzer_idn_var.set(
                f"已发现：{identities[analyzers[0]]}" if analyzers else "未发现 DSA 系列频谱仪"
            )
            self.connect_button.configure(state="normal")
            self.status_var.set(
                f"VISA 共 {len(resources)} 个；DG {len(generators)} 台，DSA {len(analyzers)} 台。"
            )
            self.log(f"VISA 后端：{payload['backend']}")
            self.log("VISA 资源：" + (", ".join(resources) if resources else "未发现；可手动输入地址"))
            for probe in probes:
                if probe.get("idn"):
                    self.log(f"识别：{probe['resource']}  =>  {probe['idn']}")
                else:
                    self.log(f"无法识别：{probe['resource']}  =>  {probe.get('error', '无响应')}")
            if not generators or not analyzers:
                missing = []
                if not generators:
                    missing.append("DG5352 Pro")
                if not analyzers:
                    missing.append("DSA815")
                messagebox.showwarning(
                    "仪器未全部识别",
                    "当前缺少：" + "、".join(missing) + "。\n\n"
                    "USB 连接请使用仪器背面的 USB DEVICE，并确认仪器已开机、线缆支持数据传输。\n"
                    "如果使用 LAN，可在对应地址框中手动输入 TCPIP0::<IP>::INSTR。",
                )
            elif not (
                self.real_session is not None
                and self.real_session.connected
                and self.connected_resources == (generators[0], analyzers[0])
            ):
                self.status_var.set("两台仪器已识别，正在自动连接…")
                self.root.after(10, self.connect_instruments)
        elif kind == "connected":
            if self.real_session is not None and self.real_session is not payload["session"]:
                self.real_session.close()
            self.real_session = payload["session"]
            self.generator_idn_var.set(payload["gen_idn"])
            self.analyzer_idn_var.set(payload["sa_idn"])
            self.connected_resources = tuple(payload["resources"])
            self.connect_button.configure(state="normal")
            self.mode_var.set("visa")
            self.status_var.set("两台仪器已连接并识别；可返回扫幅页。")
            if "DSA8" not in payload["sa_idn"].upper():
                messagebox.showwarning("频谱仪型号提示", "已连接，但频谱仪 ID 中未识别到 DSA8xx；请核对 SCPI 命令。")
        elif kind == "connect_error":
            self.connect_button.configure(state="normal")
            self.status_var.set(str(payload))
            self.log(str(payload))
            messagebox.showerror("VISA", str(payload))
        elif kind == "started":
            self.current_csv_path = Path(payload["csv_path"])
            self.log(f"多轮扫幅数据将逐点写入：{self.current_csv_path}")
        elif kind == "frequency_started":
            self.current_frequency_csv_path = Path(payload["csv_path"])
            self.log(f"扫频数据将逐点写入：{self.current_frequency_csv_path}")
        elif kind == "status":
            self.status_var.set(str(payload))
        elif kind == "point":
            point: MeasurementPoint = payload["point"]
            self.results.append(point)
            self._append_point(point)
            index = payload["index"]
            total = payload["total"]
            self.progress.configure(value=100.0 * index / total)
            self.progress_text_var.set(f"{index} / {total}")
            self.live_mvpp_var.set(f"第 {point.round_index} 轮 · {point.commanded_mvpp:g} mVpp")
            self.live_peak_var.set(f"{point.analyzer_peak_dbm:.2f} dBm")
            self.live_gain_var.set("—" if point.gain_db is None else f"{point.gain_db:.2f} dB")
            self.live_power_var.set(f"{point.output_power_w:.3f} W")
            now = time.monotonic()
            self._draw_plot(force=(index == total or now - self._last_plot_update > 0.25))
        elif kind == "frequency_point":
            point: FrequencyMeasurementPoint = payload["point"]
            self.frequency_results.append(point)
            self._append_frequency_point(point)
            index = payload["index"]
            total = payload["total"]
            self.frequency_progress.configure(value=100.0 * index / total)
            self.frequency_progress_text_var.set(f"{index} / {total}")
            self.live_frequency_var.set(f"{point.commanded_frequency_hz / 1e6:g} MHz")
            self.frequency_live_peak_var.set(f"{point.analyzer_peak_dbm:.2f} dBm")
            self.frequency_live_gain_var.set(
                "—" if point.gain_db is None else f"{point.gain_db:.2f} dB"
            )
            self.frequency_live_power_var.set(f"{point.output_power_w:.3f} W")
            now = time.monotonic()
            self._draw_frequency_plot(
                force=(index == total or now - self._last_frequency_plot_update > 0.25)
            )
        elif kind in ("complete", "stopped"):
            self._set_run_state("idle")
            self.active_sweep_type = None
            self.current_csv_path = Path(payload["csv_path"])
            self._draw_plot(force=True)
            self.current_png_path = self.current_csv_path.with_suffix(".png")
            try:
                self.figure.savefig(self.current_png_path, dpi=170, facecolor=self.CARD)
                self.log(f"曲线已保存：{self.current_png_path}")
            except Exception as exc:
                self.log(f"保存曲线失败：{exc}")
            try:
                self._save_average_outputs()
            except Exception as exc:
                self.log(f"生成独立平均文件失败：{exc}")
            if kind == "complete":
                rounds = len({point.round_index for point in self.results})
                self.status_var.set(f"多轮扫幅完成：{rounds} 轮、{len(self.results)} 点；RF 已关闭。")
                messagebox.showinfo(
                    "多轮扫幅完成",
                    f"已完成 {rounds} 轮、{len(self.results)} 个点，RF 已自动关闭。"
                    f"\n\n原始 CSV：{self.current_csv_path}\n各轮曲线：{self.current_png_path}"
                    f"\n平均表：{self.current_average_csv_path}"
                    f"\n独立平均曲线：{self.current_average_png_path}"
                    f"\n平均数据表图片：{self.current_average_table_png_path}",
                )
            else:
                self.status_var.set(f"扫描已停止：保留 {len(self.results)} 点；RF 已关闭。")
                messagebox.showinfo(
                    "扫描已停止",
                    f"已保留 {len(self.results)} 个点，RF 已关闭。"
                    f"\n原始 CSV：{self.current_csv_path}"
                    f"\n当前可用平均表：{self.current_average_csv_path}"
                    f"\n平均数据表图片：{self.current_average_table_png_path}",
                )
        elif kind in ("frequency_complete", "frequency_stopped"):
            self._set_run_state("idle")
            self.active_sweep_type = None
            self.current_frequency_csv_path = Path(payload["csv_path"])
            self._draw_frequency_plot(force=True)
            self.current_frequency_png_path = self.current_frequency_csv_path.with_suffix(".png")
            try:
                self.frequency_figure.savefig(
                    self.current_frequency_png_path, dpi=170, facecolor=self.CARD
                )
                self.log(f"扫频曲线已保存：{self.current_frequency_png_path}")
            except Exception as exc:
                self.log(f"保存扫频曲线失败：{exc}")
            if kind == "frequency_complete":
                self.status_var.set(f"扫频完成：{len(self.frequency_results)} 点；RF 已关闭。")
                messagebox.showinfo(
                    "扫频完成",
                    f"已完成 {len(self.frequency_results)} 个频点，RF 已自动关闭。"
                    f"\n\nCSV：{self.current_frequency_csv_path}"
                    f"\n曲线：{self.current_frequency_png_path}",
                )
            else:
                self.status_var.set(
                    f"扫频已停止：保留 {len(self.frequency_results)} 点；RF 已关闭。"
                )
                messagebox.showinfo(
                    "扫频已停止",
                    f"已保留 {len(self.frequency_results)} 个频点，RF 已关闭。"
                    f"\nCSV：{self.current_frequency_csv_path}",
                )
        elif kind == "error":
            self._set_run_state("idle")
            self.active_sweep_type = None
            self.status_var.set("扫描出错；已执行 RF 关闭。")
            self.log(payload["traceback"])
            if payload.get("csv_path"):
                self.current_csv_path = Path(payload["csv_path"])
            messagebox.showerror("扫描出错", f"{payload['message']}\n\n已请求关闭 RF；已写入的数据仍保留在 CSV。")
        elif kind == "frequency_error":
            self._set_run_state("idle")
            self.active_sweep_type = None
            self.status_var.set("扫频出错；已执行 RF 关闭。")
            self.log(payload["traceback"])
            if payload.get("csv_path"):
                self.current_frequency_csv_path = Path(payload["csv_path"])
            messagebox.showerror(
                "扫频出错",
                f"{payload['message']}\n\n已请求关闭 RF；已写入的数据仍保留在 CSV。",
            )
        elif kind == "rf_off":
            self.log("RF 输出关闭命令已执行。")
        elif kind == "emergency_done":
            self.status_var.set("RF 紧急关闭命令已执行。")
            self.log("用户执行 RF 紧急关闭。")
        elif kind == "emergency_error":
            self.status_var.set("RF 紧急关闭失败；请立即在信号发生器面板手动关输出。")
            messagebox.showerror("紧急关闭失败", f"{payload}\n\n请立即在信号发生器面板手动关闭 RF 输出。")

    def _save_average_outputs(self) -> None:
        if self.current_csv_path is None or not self.results:
            return
        averaged = aggregate_amplitude_results(self.results)
        stem = self.current_csv_path.stem
        self.current_average_csv_path = self.current_csv_path.with_name(
            f"{stem}_average.csv"
        )
        self.current_average_png_path = self.current_csv_path.with_name(
            f"{stem}_average.png"
        )
        self.current_average_table_png_path = self.current_csv_path.with_name(
            f"{stem}_average_table.png"
        )
        write_average_csv(self.current_average_csv_path, averaged)
        save_average_plot_file(self.current_average_png_path, averaged)
        save_average_table_image(
            self.current_average_table_png_path,
            averaged,
            source_name=self.current_csv_path.name,
        )
        self.log(f"平均数据 CSV 已保存：{self.current_average_csv_path}")
        self.log(f"独立平均曲线已保存：{self.current_average_png_path}")
        self.log(f"平均数据表图片已保存：{self.current_average_table_png_path}")

    def _append_point(self, point: MeasurementPoint, reveal: bool = True) -> None:
        values = (
            point.index,
            point.round_index,
            f"{point.commanded_mvpp:g}",
            f"{point.peak_frequency_hz / 1e6:.6f}",
            f"{point.analyzer_peak_dbm:.3f}",
            f"{point.corrected_output_dbm:.3f}",
            "—" if point.input_dbm is None else f"{point.input_dbm:.3f}",
            "—" if point.gain_db is None else f"{point.gain_db:.3f}",
            f"{point.output_power_w:.4f}",
        )
        item = self.tree.insert("", "end", values=values)
        if reveal:
            self.tree.see(item)

    def _append_frequency_point(self, point: FrequencyMeasurementPoint) -> None:
        values = (
            point.index,
            f"{point.commanded_frequency_hz / 1e6:g}",
            f"{point.peak_frequency_hz / 1e6:.6f}",
            f"{point.commanded_mvpp:g}",
            f"{point.analyzer_peak_dbm:.3f}",
            f"{point.corrected_output_dbm:.3f}",
            "—" if point.gain_db is None else f"{point.gain_db:.3f}",
            f"{point.output_power_w:.4f}",
        )
        item = self.frequency_tree.insert("", "end", values=values)
        self.frequency_tree.see(item)

    def _draw_plot(self, force: bool = False) -> None:
        if not force:
            return
        self._last_plot_update = time.monotonic()
        self.ax_level.clear()
        self.ax_gain.clear()
        for axis in (self.ax_level, self.ax_gain):
            axis.set_facecolor("#fbfcfe")
            axis.grid(True, color="#dfe6ef", linewidth=0.8, alpha=0.9)
            axis.tick_params(colors=self.MUTED, labelsize=8)
            for spine in axis.spines.values():
                spine.set_color("#cbd5e1")

        if self.results:
            colors = ["#1769e0", "#e07a1f", "#008a65", "#8b5cf6", "#dc3c68"]
            round_ids = sorted({point.round_index for point in self.results})
            by_mvpp_corrected: dict[float, list[float]] = {}
            by_mvpp_gain: dict[float, list[float]] = {}
            for round_index in round_ids:
                ordered = sorted(
                    (point for point in self.results if point.round_index == round_index),
                    key=lambda point: point.commanded_mvpp,
                )
                color = colors[(round_index - 1) % len(colors)]
                x = [point.commanded_mvpp for point in ordered]
                corrected = [point.corrected_output_dbm for point in ordered]
                gain_x = [point.commanded_mvpp for point in ordered if point.gain_db is not None]
                gain = [point.gain_db for point in ordered if point.gain_db is not None]
                self.ax_level.plot(
                    x, corrected, "o-", color=color, markersize=3.2, linewidth=1.25,
                    alpha=0.88, label=f"第 {round_index} 轮"
                )
                self.ax_gain.plot(
                    gain_x, gain, "o-", color=color, markersize=3.2, linewidth=1.3,
                    alpha=0.9, label=f"第 {round_index} 轮"
                )
                for point in ordered:
                    by_mvpp_corrected.setdefault(point.commanded_mvpp, []).append(
                        point.corrected_output_dbm
                    )
                    if point.gain_db is not None:
                        by_mvpp_gain.setdefault(point.commanded_mvpp, []).append(point.gain_db)

            if len(round_ids) > 1:
                avg_x = sorted(by_mvpp_corrected)
                avg_corrected = [linear_mean_dbm(by_mvpp_corrected[x]) for x in avg_x]
                avg_gain_x = sorted(by_mvpp_gain)
                avg_gain = [linear_mean_dbm(by_mvpp_gain[x]) for x in avg_gain_x]
                self.ax_level.plot(
                    avg_x, avg_corrected, "k--", linewidth=2.2, label="多轮平均"
                )
                self.ax_gain.plot(
                    avg_gain_x, avg_gain, "k--", linewidth=2.2, label="多轮平均"
                )
            self.ax_level.legend(loc="best", frameon=True, fontsize=8)
            self.ax_gain.legend(loc="best", frameon=True, fontsize=8)
        else:
            self.ax_level.text(0.5, 0.5, "等待测量数据", transform=self.ax_level.transAxes, ha="center", va="center", color="#94a3b8", fontsize=13)
            self.ax_gain.text(0.5, 0.5, "G = (频谱仪峰值 + 外置衰减) − 输入功率", transform=self.ax_gain.transAxes, ha="center", va="center", color="#94a3b8", fontsize=10)

        self.ax_level.set_ylabel("校正输出 (dBm)", color=self.INK, fontsize=9)
        self.ax_gain.set_ylabel("增益 (dB)", color=self.INK, fontsize=9)
        self.ax_gain.set_xlabel("信号发生器幅度 (mVpp；实际采集方向 580 → 300)", color=self.INK, fontsize=9)
        self.ax_level.set_title("各轮输出对比", loc="left", color=self.INK, fontsize=11, fontweight="bold")
        self.ax_gain.set_title("各轮增益与平均曲线", loc="left", color=self.INK, fontsize=11, fontweight="bold")
        self.canvas.draw_idle()

    def _draw_frequency_plot(self, force: bool = False) -> None:
        if not force:
            return
        self._last_frequency_plot_update = time.monotonic()
        self.frequency_ax_level.clear()
        self.frequency_ax_gain.clear()
        for axis in (self.frequency_ax_level, self.frequency_ax_gain):
            axis.set_facecolor("#fbfcfe")
            axis.grid(True, color="#dfe6ef", linewidth=0.8, alpha=0.9)
            axis.tick_params(colors=self.MUTED, labelsize=8)
            for spine in axis.spines.values():
                spine.set_color("#cbd5e1")

        if self.frequency_results:
            ordered = sorted(
                self.frequency_results, key=lambda point: point.commanded_frequency_hz
            )
            x = [point.commanded_frequency_hz / 1e6 for point in ordered]
            corrected = [point.corrected_output_dbm for point in ordered]
            gain_x = [
                point.commanded_frequency_hz / 1e6
                for point in ordered
                if point.gain_db is not None
            ]
            gain = [point.gain_db for point in ordered if point.gain_db is not None]
            self.frequency_ax_level.plot(
                x, corrected, "o-", color="#e07a1f", markersize=4, linewidth=1.6,
                label="校正输出"
            )
            self.frequency_ax_gain.plot(
                gain_x, gain, "o-", color=self.GREEN, markersize=4, linewidth=1.8,
                label="功率增益"
            )
            self.frequency_ax_level.legend(loc="best", frameon=True, fontsize=8)
            self.frequency_ax_gain.legend(loc="best", frameon=True, fontsize=8)
        else:
            self.frequency_ax_level.text(
                0.5, 0.5, "等待扫频数据", transform=self.frequency_ax_level.transAxes,
                ha="center", va="center", color="#94a3b8", fontsize=13
            )
            self.frequency_ax_gain.text(
                0.5, 0.5, "固定幅度，逐点同步移动 DG 与 DSA 中心频率",
                transform=self.frequency_ax_gain.transAxes, ha="center", va="center",
                color="#94a3b8", fontsize=10
            )
        self.frequency_ax_level.set_ylabel("校正输出 (dBm)", color=self.INK, fontsize=9)
        self.frequency_ax_gain.set_ylabel("增益 (dB)", color=self.INK, fontsize=9)
        self.frequency_ax_gain.set_xlabel("设定频率 (MHz)", color=self.INK, fontsize=9)
        self.frequency_ax_level.set_title(
            "频率响应：输出", loc="left", color=self.INK, fontsize=11, fontweight="bold"
        )
        self.frequency_ax_gain.set_title(
            "频率响应：增益", loc="left", color=self.INK, fontsize=11, fontweight="bold"
        )
        self.frequency_canvas.draw_idle()

    def export_results(self) -> None:
        if not self.results:
            messagebox.showinfo("没有数据", "当前还没有可导出的测量数据。")
            return
        default_name = f"gain_sweep_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        target = filedialog.asksaveasfilename(
            title="另存测量数据",
            defaultextension=".csv",
            initialfile=default_name,
            filetypes=[("CSV 数据", "*.csv")],
        )
        if not target:
            return
        csv_target = Path(target)
        with csv_target.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(MeasurementPoint.csv_header())
            writer.writerows(point.csv_row() for point in self.results)
        png_target = csv_target.with_suffix(".png")
        self.figure.savefig(png_target, dpi=170, facecolor=self.CARD)
        self.status_var.set(f"已导出：{csv_target.name} 和 {png_target.name}")

    def export_frequency_results(self) -> None:
        if not self.frequency_results:
            messagebox.showinfo("没有数据", "当前还没有可导出的扫频数据。")
            return
        default_name = f"frequency_sweep_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        target = filedialog.asksaveasfilename(
            title="另存扫频数据",
            defaultextension=".csv",
            initialfile=default_name,
            filetypes=[("CSV 数据", "*.csv")],
        )
        if not target:
            return
        csv_target = Path(target)
        with csv_target.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(FrequencyMeasurementPoint.csv_header())
            writer.writerows(point.csv_row() for point in self.frequency_results)
        png_target = csv_target.with_suffix(".png")
        self.frequency_figure.savefig(png_target, dpi=170, facecolor=self.CARD)
        self.status_var.set(f"已导出扫频数据：{csv_target.name} 和 {png_target.name}")

    def open_measurement_dir(self) -> None:
        MEASUREMENT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(MEASUREMENT_DIR)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("无法打开目录", str(exc))

    def _settings_payload(self) -> dict[str, Any]:
        generator_resource = self.generator_resource_var.get().strip()
        analyzer_resource = self.analyzer_resource_var.get().strip()
        if generator_resource and generator_resource == analyzer_resource:
            analyzer_resource = ""
        return {
            "mode": self.mode_var.get(),
            "start_mvpp": self.start_var.get(),
            "stop_mvpp": self.stop_var.get(),
            "step_mvpp": self.step_var.get(),
            "hard_limit_mvpp": self.hard_limit_var.get(),
            "settle_s": self.settle_var.get(),
            "samples": self.samples_var.get(),
            "sample_gap_s": self.sample_gap_var.get(),
            "rounds": self.rounds_var.get(),
            "inter_round_pause_s": self.inter_round_pause_var.get(),
            "frequency_start_mhz": self.frequency_start_var.get(),
            "frequency_stop_mhz": self.frequency_stop_var.get(),
            "frequency_step_mhz": self.frequency_step_var.get(),
            "frequency_amplitude_mvpp": self.frequency_amplitude_var.get(),
            "frequency_settle_s": self.frequency_settle_var.get(),
            "frequency_samples": self.frequency_samples_var.get(),
            "attenuation_db": self.attenuation_var.get(),
            "impedance_ohm": self.impedance_var.get(),
            "timeout_ms": self.timeout_var.get(),
            "generator_resource": generator_resource,
            "analyzer_resource": analyzer_resource,
            "profile": asdict(self._read_profile_safely()),
        }

    def _save_config(self) -> None:
        try:
            CONFIG_PATH.write_text(json.dumps(self._settings_payload(), ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self.log(f"保存设置失败：{exc}")

    def _load_config(self) -> None:
        if not CONFIG_PATH.exists():
            return
        try:
            payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            mappings = {
                "mode": self.mode_var,
                "start_mvpp": self.start_var,
                "stop_mvpp": self.stop_var,
                "step_mvpp": self.step_var,
                "hard_limit_mvpp": self.hard_limit_var,
                "settle_s": self.settle_var,
                "samples": self.samples_var,
                "sample_gap_s": self.sample_gap_var,
                "rounds": self.rounds_var,
                "inter_round_pause_s": self.inter_round_pause_var,
                "frequency_start_mhz": self.frequency_start_var,
                "frequency_stop_mhz": self.frequency_stop_var,
                "frequency_step_mhz": self.frequency_step_var,
                "frequency_amplitude_mvpp": self.frequency_amplitude_var,
                "frequency_settle_s": self.frequency_settle_var,
                "frequency_samples": self.frequency_samples_var,
                "attenuation_db": self.attenuation_var,
                "impedance_ohm": self.impedance_var,
                "timeout_ms": self.timeout_var,
                "generator_resource": self.generator_resource_var,
                "analyzer_resource": self.analyzer_resource_var,
            }
            for key, variable in mappings.items():
                if key in payload:
                    variable.set(str(payload[key]))
            if (
                self.generator_resource_var.get().strip()
                and self.generator_resource_var.get().strip() == self.analyzer_resource_var.get().strip()
            ):
                self.analyzer_resource_var.set("")
                self.log("已清除重复的频谱仪地址；同一个 VISA 资源不能代表两台仪器。")
            profile = payload.get("profile", {})
            variable_mappings = {
                "generator_set_amplitude": self.gen_set_var,
                "generator_set_frequency": self.gen_frequency_var,
                "generator_output_on": self.gen_on_var,
                "generator_output_off": self.gen_off_var,
                "analyzer_trigger": self.sa_trigger_var,
                "analyzer_opc_query": self.sa_opc_var,
                "analyzer_peak_search": self.sa_peak_var,
                "analyzer_peak_y_query": self.sa_y_var,
                "analyzer_peak_x_query": self.sa_x_var,
                "analyzer_set_center": self.sa_center_var,
            }
            for key, variable in variable_mappings.items():
                if key in profile:
                    variable.set(str(profile[key]))
            text_mappings = {
                "generator_prepare": self.gen_prepare_text,
                "analyzer_prepare": self.sa_prepare_text,
                "analyzer_restore": self.sa_restore_text,
            }
            for key, widget in text_mappings.items():
                if key in profile:
                    widget.delete("1.0", "end")
                    widget.insert("1.0", str(profile[key]))
        except Exception as exc:
            self.log(f"读取设置失败，已使用默认值：{exc}")

    def on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("扫描仍在运行", "关闭窗口会停止扫描并关闭 RF。确定退出吗？"):
                return
            self.worker.request_stop()
            profile = self._read_profile_safely()
            if self.real_session and self.real_session.connected:
                try:
                    self.real_session.safe_output_off(profile)
                except Exception:
                    pass
        self._save_config()
        if self.real_session is not None:
            self.real_session.close()
        self.root.destroy()


def run_self_test() -> None:
    points = build_descending_points(580, 300, 5)
    assert len(points) == 57
    assert points[0] == 580.0 and points[-1] == 300.0
    two_round_sequence = [(round_index, point) for round_index in (1, 2) for point in points]
    assert len(two_round_sequence) == 114
    assert two_round_sequence[0] == (1, 580.0)
    assert two_round_sequence[56] == (1, 300.0)
    assert two_round_sequence[57] == (2, 580.0)
    assert two_round_sequence[-1] == (2, 300.0)
    assert build_frequency_points_mhz(99, 101, 1) == [99.0, 100.0, 101.0]
    assert abs(mvpp_to_dbm(300, 50) - (-6.478174818886377)) < 1e-9
    assert abs(dbm_to_watts(30.0) - 1.0) < 1e-12
    assert abs(linear_mean_dbm([0.0, 0.0])) < 1e-12
    assert parse_scpi_number("-2.087000E+01\n") == -20.87
    synthetic = [
        MeasurementPoint(
            index=index,
            acquired_at="",
            commanded_mvpp=300.0,
            peak_frequency_hz=100e6,
            analyzer_peak_dbm=analyzer_dbm,
            analyzer_std_db=0.0,
            external_attenuation_db=48.0,
            corrected_output_dbm=analyzer_dbm + 48.0,
            input_dbm=-6.478174818886377,
            gain_db=analyzer_dbm + 48.0 + 6.478174818886377,
            output_power_w=dbm_to_watts(analyzer_dbm + 48.0),
            samples=1,
            round_index=index,
        )
        for index, analyzer_dbm in enumerate((-20.0, -19.0, -21.0), start=1)
    ]
    averaged = aggregate_amplitude_results(synthetic)
    assert len(averaged) == 1 and averaged[0].round_count == 3
    assert abs(averaged[0].mean_analyzer_peak_dbm - linear_mean_dbm([-20, -19, -21])) < 1e-12
    print(
        "SELF-TEST PASS: multi-round amplitude averages, 580->300 mVpp sweep, "
        "frequency points, and power conversions are valid."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="HAT attenuator-chain amplifier gain sweep GUI")
    parser.add_argument("--self-test", action="store_true", help="run calculation checks without opening the GUI")
    parser.add_argument(
        "--average-from",
        type=Path,
        help="build an average CSV, curve PNG, and table PNG from an amplitude-sweep CSV",
    )
    args = parser.parse_args()
    if args.self_test:
        run_self_test()
        return
    if args.average_from is not None:
        source = args.average_from.resolve()
        averaged = aggregate_amplitude_results(load_measurement_csv(source))
        average_csv = source.with_name(f"{source.stem}_average.csv")
        average_png = source.with_name(f"{source.stem}_average.png")
        average_table_png = source.with_name(f"{source.stem}_average_table.png")
        write_average_csv(average_csv, averaged)
        save_average_plot_file(average_png, averaged)
        save_average_table_image(average_table_png, averaged, source_name=source.name)
        print(f"AVERAGE_CSV={average_csv}")
        print(f"AVERAGE_PNG={average_png}")
        print(f"AVERAGE_TABLE_PNG={average_table_png}")
        return
    root = tk.Tk()
    SweepApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
