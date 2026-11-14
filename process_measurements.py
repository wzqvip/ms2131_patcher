#!/usr/bin/env python3
"""Rebuild the MS2131 sharpness/color report from saved PNG captures."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parent
PICS = ROOT / "pics"
ARCHIVE = PICS / "nightlight_on_archive"
SAMPLES_PATH = PICS / "ms2131_colorbars_1080p_samples.json"
SOURCE_EDGE = PICS / "ms2131_sharpness_test_1080p.png"
STOCK_EDGE = ARCHIVE / "ms2131_sharpness_chart_before.png"
TEST_EDGE = PICS / "ms2131_sharpness_chart_nightlight_off.png"
COLOR_CAPTURE = PICS / "ms2131_colorbars_capture_nightlight_off.png"
REPORT_PATH = PICS / "ms2131_nightlight_off_measurements.json"
FIGURE_PATH = PICS / "ms2131_sharpness_color_comparison.png"


def rgb_image(path: Path) -> np.ndarray:
    if not path.is_file():
        raise FileNotFoundError(f"Required capture not found: {path}")
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.float64)


def luma(rgb: np.ndarray) -> np.ndarray:
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def edge_profile(path: Path) -> tuple[np.ndarray, dict[str, float]]:
    """Measure the slanted edge with the same fixed ROI on each capture."""
    gray = luma(rgb_image(path))
    profiles = []
    for y in range(220, 930):
        edge_x = 350 + 0.045 * (y - 555)
        profiles.append([gray[y, round(edge_x + d)] for d in range(-20, 21)])
    profile = np.asarray(profiles).mean(axis=0)
    white = float(profile[:11].mean())
    black = float(profile[30:].mean())
    normalized = (profile - black) / (white - black)

    def crossing(level: float) -> float:
        ix = int(np.flatnonzero(normalized <= level)[0])
        a, b = normalized[ix - 1], normalized[ix]
        return float(ix - 1 + (a - level) / (a - b))

    metrics = {
        "white_luma": round(white, 3),
        "black_luma": round(black, 3),
        "esf_10_90_width_px": round(crossing(0.1) - crossing(0.9), 3),
        "overshoot_white_pct": round(float((profile.max() - white) / (white - black) * 100), 3),
        "undershoot_black_pct": round(float((black - profile.min()) / (white - black) * 100), 3),
    }
    return profile, metrics


def sample_colors() -> tuple[list[int], dict[str, dict[str, dict[str, list[float]]]], dict[str, list[float]]]:
    spec = json.loads(SAMPLES_PATH.read_text(encoding="utf-8"))
    image = rgb_image(COLOR_CAPTURE)
    levels, samples = [], {}
    for group in ("bar_samples", "neutral_samples", "primary_samples"):
        samples[group] = {}
        for sample in spec[group]:
            name = sample["name"]
            x, y = sample["sample_xy"]
            patch = image[y - 12 : y + 13, x - 12 : x + 13]
            captured = np.median(patch.reshape(-1, 3), axis=0).round(1).tolist()
            expected = sample["expected_rgb"]
            samples[group][name] = {
                "expected_rgb": expected,
                "captured_rgb": captured,
                "delta_rgb": [round(captured[i] - expected[i], 1) for i in range(3)],
            }
            if group == "neutral_samples":
                levels.append(int(expected[0]))

    neutral_channels = {
        channel: [samples["neutral_samples"][sample["name"]]["captured_rgb"][index] for sample in spec["neutral_samples"]]
        for index, channel in enumerate(("R", "G", "B"))
    }
    return levels, samples, neutral_channels


def main() -> None:
    profiles = {}
    metrics = {}
    for name, path in (
        ("Source chart", SOURCE_EDGE),
        ("Stock firmware (Night Light on)", STOCK_EDGE),
        ("Test firmware (Night Light off)", TEST_EDGE),
    ):
        profiles[name], metrics[name] = edge_profile(path)

    levels, neutral_samples, captured_channels = sample_colors()
    spread = [max(rgb) - min(rgb) for rgb in zip(*(captured_channels[c] for c in "RGB"))]
    tint_rg = [captured_channels["R"][i] - captured_channels["G"][i] for i in range(len(levels))]
    tint_bg = [captured_channels["B"][i] - captured_channels["G"][i] for i in range(len(levels))]

    report = {
        "captured_at_local": "reprocessed from saved capture files",
        "user_confirmed_state": "Windows Night Light disabled for current test-firmware captures",
        "capture_mode": "DISPLAY3 1920x1080; USB3 Video DirectShow YUY2 60fps",
        "color_samples_nightlight_off": neutral_samples,
        "neutral_tint_median_R_minus_G_B_minus_G_nightlight_off": [
            round(float(np.median(tint_rg)), 1), round(float(np.median(tint_bg)), 1)
        ],
        "neutral_grayscale_mean_absolute_channel_spread_nightlight_off": round(float(np.mean(spread)), 3),
        "sharpness": {
            "source_chart": metrics["Source chart"],
            "stock_firmware_nightlight_on": metrics["Stock firmware (Night Light on)"],
            "test_firmware_nightlight_off": metrics["Test firmware (Night Light off)"],
        },
        "artifacts": {
            "color_capture": "pics/ms2131_colorbars_capture_nightlight_off.png",
            "sharpness_capture": "pics/ms2131_sharpness_chart_nightlight_off.png",
            "stock_sharpness_capture": "pics/nightlight_on_archive/ms2131_sharpness_chart_before.png",
        },
        "limitations": "Color values measure the complete GPU HDMI output, capture card, UVC and RGB conversion path; they do not isolate MS2131 processing. Stock edge capture was made with Night Light enabled; normalized edge width is less sensitive to this tone shift.",
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    x = np.arange(-20, 21)
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.2), constrained_layout=True)
    for label, profile in profiles.items():
        white, black = profile[:11].mean(), profile[30:].mean()
        axes[0].plot(x, (profile - black) / (white - black), label=f"{label} ({metrics[label]['esf_10_90_width_px']:.2f}px)")
    axes[0].set(title="Slanted-edge response", xlabel="Pixels from edge", ylabel="Normalized luma", xlim=(-6, 6), ylim=(-0.05, 1.08))
    axes[0].grid(True, alpha=0.3)
    axes[0].legend()

    expected = np.asarray(levels)
    axes[1].plot(expected, expected, "--", color="0.35", label="Expected neutral")
    for channel, color in (("R", "#d62728"), ("G", "#2ca02c"), ("B", "#1f77b4")):
        axes[1].plot(expected, captured_channels[channel], "o-", color=color, label=f"Captured {channel}")
    axes[1].set(title="Grayscale response (Night Light off)", xlabel="Source gray level (8-bit)", ylabel="Captured channel value (8-bit)", xlim=(0, 260), ylim=(0, 265))
    axes[1].grid(True, alpha=0.3)
    axes[1].legend()
    fig.suptitle("MS2131 capture comparison")
    fig.savefig(FIGURE_PATH, dpi=160)
    plt.close(fig)

    print(f"Wrote {REPORT_PATH}")
    print(f"Wrote {FIGURE_PATH}")
    for label, values in metrics.items():
        print(f"{label}: edge width {values['esf_10_90_width_px']:.3f} px")
    print(f"Night Light off neutral channel spread: {report['neutral_grayscale_mean_absolute_channel_spread_nightlight_off']:.3f}")


if __name__ == "__main__":
    main()
