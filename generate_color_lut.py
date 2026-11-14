#!/usr/bin/env python3
"""Fit a host-side 3D .cube correction LUT from the MS2131 chart measurements."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parent
PICS = ROOT / "pics"
REPORT = PICS / "ms2131_nightlight_off_measurements.json"
CAPTURE = PICS / "ms2131_colorbars_capture_nightlight_off.png"
DEFAULT_LUT = PICS / "ms2131_color_correction.cube"
DEFAULT_PREVIEW = PICS / "ms2131_colorbars_lut_preview.png"
DEFAULT_CURVES = PICS / "ms2131_color_lut_curves.png"
DEFAULT_RESULT = PICS / "ms2131_color_lut_fit.json"


def build_model(report: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    samples = report["color_samples_nightlight_off"]
    neutral = samples["neutral_samples"]

    # Invert the measured neutral ramp to restore the input gray level.
    measured = [0.0] + [neutral[k]["captured_rgb"][0] for k in neutral] + [255.0]
    target = [0.0] + [neutral[k]["expected_rgb"][0] for k in neutral] + [255.0]
    order = np.argsort(measured)
    in_axis = np.asarray(measured, dtype=np.float64)[order]
    out_axis = np.asarray(target, dtype=np.float64)[order]
    unique, first = np.unique(in_axis, return_index=True)
    in_axis, out_axis = unique, out_axis[first]

    def restore_neutral(rgb: np.ndarray) -> np.ndarray:
        return np.stack([np.interp(rgb[..., channel], in_axis, out_axis) for channel in range(3)], axis=-1)

    # Fit a small affine color transform using the measured primary and
    # secondary patches. The saturation blend below keeps the neutral axis fixed.
    training = []
    for group in ("bar_samples", "primary_samples"):
        for name, sample in samples[group].items():
            if name in ("K", "W"):
                continue
            training.append(sample)
    measured_rgb = np.asarray([s["captured_rgb"] for s in training], dtype=np.float64)
    expected_rgb = np.asarray([s["expected_rgb"] for s in training], dtype=np.float64)
    shaped_rgb = restore_neutral(measured_rgb)
    design = np.column_stack((shaped_rgb, np.ones(len(shaped_rgb))))
    coefficients, *_ = np.linalg.lstsq(design, expected_rgb, rcond=None)
    matrix = coefficients[:3, :].T
    bias = coefficients[3, :]
    return in_axis, out_axis, matrix, bias


def apply_model(rgb: np.ndarray, in_axis: np.ndarray, out_axis: np.ndarray,
                matrix: np.ndarray, bias: np.ndarray) -> np.ndarray:
    rgb = np.asarray(rgb, dtype=np.float64)
    shaped = np.stack([np.interp(rgb[..., c], in_axis, out_axis) for c in range(3)], axis=-1)
    high = shaped.max(axis=-1, keepdims=True)
    low = shaped.min(axis=-1, keepdims=True)
    saturation = np.divide(high - low, np.maximum(high, 1.0))
    affine = shaped @ matrix.T + bias
    corrected = shaped + saturation * (affine - shaped)
    return np.clip(corrected, 0.0, 255.0)


def write_cube(path: Path, size: int, model: tuple[np.ndarray, ...]) -> None:
    in_axis, out_axis, matrix, bias = model
    with path.open("w", encoding="ascii", newline="\n") as f:
        f.write('TITLE "MS2131 measured color correction (experimental)"\n')
        f.write(f"LUT_3D_SIZE {size}\n")
        f.write("DOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1.0 1.0 1.0\n")
        axis = np.linspace(0.0, 255.0, size)
        # .cube order: red varies fastest, then green, then blue.
        for blue in axis:
            for green in axis:
                rgb = np.column_stack((axis, np.full(size, green), np.full(size, blue)))
                values = apply_model(rgb, in_axis, out_axis, matrix, bias) / 255.0
                for r, g, b in values:
                    f.write(f"{r:.7f} {g:.7f} {b:.7f}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=33, help="3D LUT edge size (default: 33)")
    parser.add_argument("--strength", type=float, default=1.0,
                        help="Scale chroma correction from 0 (neutral-only) to 1 (full fit)")
    args = parser.parse_args()
    if args.size < 2:
        parser.error("--size must be at least 2")
    if not 0.0 <= args.strength <= 1.0:
        parser.error("--strength must be between 0 and 1")

    report = json.loads(REPORT.read_text(encoding="utf-8"))
    model = build_model(report)
    in_axis, out_axis, matrix, bias = model

    # Apply optional strength by blending the fitted color transform back toward
    # the neutral-only tone curve. The saved LUT remains a standard .cube file.
    matrix = np.eye(3) + args.strength * (matrix - np.eye(3))
    bias = args.strength * bias
    model = (in_axis, out_axis, matrix, bias)

    write_cube(DEFAULT_LUT, args.size, model)

    capture = np.asarray(Image.open(CAPTURE).convert("RGB"), dtype=np.uint8)
    corrected = np.rint(apply_model(capture, *model)).astype(np.uint8)
    original_image = Image.fromarray(capture)
    corrected_image = Image.fromarray(corrected)
    canvas = Image.new("RGB", (original_image.width * 2, original_image.height), "#222222")
    canvas.paste(original_image, (0, 0))
    canvas.paste(corrected_image, (original_image.width, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, 255, 28), fill="#111111")
    draw.text((8, 7), "Measured capture", fill="white")
    draw.rectangle((original_image.width, 0, original_image.width + 255, 28), fill="#111111")
    draw.text((original_image.width + 8, 7), f"LUT preview (strength {args.strength:g})", fill="white")
    canvas.save(DEFAULT_PREVIEW)

    samples = report["color_samples_nightlight_off"]
    patch_results = {}
    for group in ("bar_samples", "primary_samples"):
        patch_results[group] = {}
        for name, sample in samples[group].items():
            before = np.asarray(sample["captured_rgb"], dtype=np.float64)
            after = apply_model(before, *model)
            expected = np.asarray(sample["expected_rgb"], dtype=np.float64)
            patch_results[group][name] = {
                "expected_rgb": expected.round(1).tolist(),
                "measured_rgb": before.round(1).tolist(),
                "predicted_corrected_rgb": after.round(1).tolist(),
                "residual_before_rgb": (before - expected).round(1).tolist(),
                "residual_after_rgb": (after - expected).round(1).tolist(),
            }

    color_rows = [v for group in patch_results.values() for name, v in group.items() if name not in ("K", "W")]
    before_rmse = float(np.sqrt(np.mean([np.mean(np.square(v["residual_before_rgb"])) for v in color_rows])))
    after_rmse = float(np.sqrt(np.mean([np.mean(np.square(v["residual_after_rgb"])) for v in color_rows])))

    neutral_items = sorted(
        samples["neutral_samples"].items(), key=lambda item: int(item[0].rstrip("%"))
    )
    gray_expected = np.asarray([v["expected_rgb"][0] for _, v in neutral_items], dtype=np.float64)
    gray_before = np.asarray([v["captured_rgb"] for _, v in neutral_items], dtype=np.float64)
    gray_after = apply_model(gray_before, *model)
    color_order = ["R", "Y", "G", "C", "B", "M"]
    color_before = np.asarray([samples["bar_samples"][name]["captured_rgb"] for name in color_order], dtype=np.float64)
    color_after = apply_model(color_before, *model)
    color_expected = np.asarray([samples["bar_samples"][name]["expected_rgb"] for name in color_order], dtype=np.float64)

    curve_data = {
        "neutral_gray_expected_levels": gray_expected.tolist(),
        "neutral_gray_measured_before_rgb": gray_before.tolist(),
        "neutral_gray_predicted_after_rgb": gray_after.round(3).tolist(),
        "color_patch_order": color_order,
        "color_patch_expected_rgb": color_expected.tolist(),
        "color_patch_measured_before_rgb": color_before.tolist(),
        "color_patch_predicted_after_rgb": color_after.round(3).tolist(),
    }

    fig, axes = plt.subplots(1, 2, figsize=(13.5, 5.2), constrained_layout=True)
    axes[0].plot(gray_expected, gray_expected, "k--", linewidth=1.5, label="Expected neutral")
    for channel, index, color in (("R", 0, "#d62728"), ("G", 1, "#2ca02c"), ("B", 2, "#1f77b4")):
        axes[0].plot(gray_expected, gray_before[:, index], "o--", color=color, alpha=0.55, label=f"Before LUT {channel}")
        axes[0].plot(gray_expected, gray_after[:, index], "-", color=color, linewidth=2, label=f"After LUT {channel}")
    axes[0].set(title="Neutral grayscale response", xlabel="Expected gray level (8-bit)", ylabel="Captured channel value (8-bit)", xlim=(0, 260), ylim=(0, 265))
    axes[0].grid(True, alpha=0.3)
    axes[0].legend(fontsize=8, ncol=2)

    patch_x = np.arange(len(color_order))
    for channel, index, color in (("R", 0, "#d62728"), ("G", 1, "#2ca02c"), ("B", 2, "#1f77b4")):
        axes[1].plot(patch_x, color_before[:, index], "o--", color=color, alpha=0.5, label=f"Before LUT {channel}")
        axes[1].plot(patch_x, color_after[:, index], "s-", color=color, linewidth=2, label=f"After LUT {channel}")
        axes[1].plot(patch_x, color_expected[:, index], ":", color=color, linewidth=1.4, alpha=0.9, label=f"Expected {channel}")
    axes[1].set(title="Primary / secondary patch response", xlabel="75% color patch", ylabel="Captured channel value (8-bit)", xticks=patch_x, xticklabels=color_order, ylim=(-8, 265))
    axes[1].grid(True, alpha=0.3)
    axes[1].legend(fontsize=8, ncol=3)
    fig.suptitle(f"MS2131 color response before and after LUT (strength {args.strength:g})")
    fig.savefig(DEFAULT_CURVES, dpi=160)
    plt.close(fig)

    result = {
        "description": "Experimental host-side 3D LUT fitted from this card's 1080p60 chart capture.",
        "lut_file": str(DEFAULT_LUT.relative_to(ROOT)).replace("\\", "/"),
        "lut_size": args.size,
        "strength": args.strength,
        "measurement_source": str(REPORT.relative_to(ROOT)).replace("\\", "/"),
        "neutral_inverse_curve_input_levels": in_axis.tolist(),
        "neutral_inverse_curve_output_levels": out_axis.tolist(),
        "chroma_matrix": matrix.tolist(),
        "chroma_bias": bias.tolist(),
        "color_patch_mean_channel_rmse_before": round(before_rmse, 3),
        "color_patch_mean_channel_rmse_predicted_after": round(after_rmse, 3),
        "patch_predictions": patch_results,
        "response_curves": curve_data,
        "preview": str(DEFAULT_PREVIEW.relative_to(ROOT)).replace("\\", "/"),
        "curves_figure": str(DEFAULT_CURVES.relative_to(ROOT)).replace("\\", "/"),
        "limitations": "The fit uses a small set of 8-bit patches measured through the complete GPU/HDMI/MS2131/UVC/RGB path. It is an approximate host-side correction, not a firmware fix or a calibrated 3D color characterization. Re-measure if GPU range, capture format, driver, or software color pipeline changes.",
    }
    DEFAULT_RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {DEFAULT_LUT}")
    print(f"Wrote {DEFAULT_PREVIEW}")
    print(f"Wrote {DEFAULT_CURVES}")
    print(f"Color-patch mean channel RMSE: {before_rmse:.2f} -> {after_rmse:.2f} levels (predicted)")


if __name__ == "__main__":
    main()
