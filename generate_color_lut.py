#!/usr/bin/env python3
"""Capture a live HDMI test chart and build a host-side color correction LUT.

Requires numpy, Tk (included with most Windows Python installers), and ffmpeg
with DirectShow support. The script reads frames from the selected UVC capture
device; it never writes firmware or changes display settings.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tkinter as tk

import numpy as np


OUTPUT_DIR = Path.cwd() / "tmp"
GRID_COLS = 8
GRID_ROWS = 5
CHART_MARGIN = 0.025
CHART_GAP = 0.008
GRAY_LEVELS = [0, 25, 51, 76, 102, 128, 153, 179, 204, 230, 255]


def enumerate_monitors() -> list[dict]:
    if sys.platform != "win32":
        raise RuntimeError("Monitor selection currently uses Windows display enumeration.")
    try:
        # Make Tk coordinates and EnumDisplayMonitors coordinates use the same
        # per-monitor physical-pixel space, including displays left of (0, 0).
        dpi_context = getattr(ctypes.windll.user32, "SetProcessDpiAwarenessContext", None)
        if dpi_context is not None and dpi_context(ctypes.c_void_p(-4)):
            pass
        else:
            ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass

    class RECT(ctypes.Structure):
        _fields_ = [(name, wintypes.LONG) for name in ("left", "top", "right", "bottom")]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    monitors = []
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(RECT), wintypes.LPARAM,
    )

    def callback(handle, _dc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            rect = info.rcMonitor
            monitors.append({
                "device": info.szDevice,
                "left": rect.left,
                "top": rect.top,
                "width": rect.right - rect.left,
                "height": rect.bottom - rect.top,
                "primary": bool(info.dwFlags & 1),
            })
        return True

    callback_ref = callback_type(callback)
    if not ctypes.windll.user32.EnumDisplayMonitors(None, None, callback_ref, 0):
        raise OSError("Windows could not enumerate displays.")
    # Desktop bounds (EnumDisplayMonitors) may differ from the HDMI timing
    # actually transmitted by the GPU. Query the active DisplayConfig paths
    # and attach the sink's active signal mode to each GDI display name.
    signal_modes = query_active_signal_modes()
    for monitor in monitors:
        monitor["active_signal_mode"] = signal_modes.get(monitor["device"])
    return monitors


def query_active_signal_modes() -> dict[str, dict | None]:
    """Return Windows DisplayConfig's active HDMI/output mode by GDI display.

    DISPLAYCONFIG_TARGET_MODE reports the signal sent to the display. This is
    deliberately kept separate from the desktop rectangle dimensions, which
    can differ when GPU scaling is enabled.
    """
    if sys.platform != "win32":
        return {}

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class RATIONAL(ctypes.Structure):
        _fields_ = [("Numerator", wintypes.UINT), ("Denominator", wintypes.UINT)]

    class REGION(ctypes.Structure):
        _fields_ = [("cx", wintypes.UINT), ("cy", wintypes.UINT)]

    class SIGNAL_INFO(ctypes.Structure):
        _fields_ = [
            ("pixelRate", ctypes.c_ulonglong), ("hSyncFreq", RATIONAL),
            ("vSyncFreq", RATIONAL), ("activeSize", REGION), ("totalSize", REGION),
            ("videoStandard", wintypes.UINT), ("scanLineOrdering", wintypes.UINT),
        ]

    class TARGET_MODE(ctypes.Structure):
        _fields_ = [("targetVideoSignalInfo", SIGNAL_INFO)]

    class MODE_UNION(ctypes.Union):
        _fields_ = [("targetMode", TARGET_MODE), ("padding", ctypes.c_byte * 48)]

    class MODE_INFO(ctypes.Structure):
        _fields_ = [
            ("infoType", wintypes.UINT), ("id", wintypes.UINT),
            ("adapterId", LUID), ("mode", MODE_UNION),
        ]

    class SOURCE_INFO(ctypes.Structure):
        _fields_ = [
            ("adapterId", LUID), ("id", wintypes.UINT),
            ("modeInfoIdx", wintypes.UINT), ("statusFlags", wintypes.UINT),
        ]

    class TARGET_INFO(ctypes.Structure):
        _fields_ = [
            ("adapterId", LUID), ("id", wintypes.UINT),
            ("modeInfoIdx", wintypes.UINT), ("outputTechnology", wintypes.UINT),
            ("rotation", wintypes.UINT), ("scaling", wintypes.UINT),
            ("refreshRate", RATIONAL), ("scanLineOrdering", wintypes.UINT),
            ("targetAvailable", wintypes.BOOL), ("statusFlags", wintypes.UINT),
        ]

    class PATH_INFO(ctypes.Structure):
        _fields_ = [
            ("sourceInfo", SOURCE_INFO), ("targetInfo", TARGET_INFO),
            ("flags", wintypes.UINT),
        ]

    class DEVICE_INFO_HEADER(ctypes.Structure):
        _fields_ = [
            ("type", wintypes.UINT), ("size", wintypes.UINT),
            ("adapterId", LUID), ("id", wintypes.UINT),
        ]

    class SOURCE_DEVICE_NAME(ctypes.Structure):
        _fields_ = [("header", DEVICE_INFO_HEADER), ("viewGdiDeviceName", wintypes.WCHAR * 32)]

    user32 = ctypes.windll.user32
    path_count, mode_count = wintypes.UINT(), wintypes.UINT()
    # QDC_ONLY_ACTIVE_PATHS = 0x2
    status = user32.GetDisplayConfigBufferSizes(0x2, ctypes.byref(path_count), ctypes.byref(mode_count))
    if status != 0 or not path_count.value:
        return {}
    paths = (PATH_INFO * path_count.value)()
    modes = (MODE_INFO * mode_count.value)()
    status = user32.QueryDisplayConfig(
        0x2, ctypes.byref(path_count), paths, ctypes.byref(mode_count), modes, None
    )
    if status != 0:
        return {}

    result: dict[str, dict | None] = {}
    for path in paths[:path_count.value]:
        source = SOURCE_DEVICE_NAME()
        source.header = DEVICE_INFO_HEADER(
            1, ctypes.sizeof(SOURCE_DEVICE_NAME), path.sourceInfo.adapterId, path.sourceInfo.id
        )
        status = user32.DisplayConfigGetDeviceInfo(ctypes.byref(source.header))
        if status != 0:
            continue
        display_name = source.viewGdiDeviceName
        idx = path.targetInfo.modeInfoIdx
        if idx >= mode_count.value or modes[idx].infoType != 2:  # DISPLAYCONFIG_MODE_INFO_TYPE_TARGET
            result[display_name] = None
            continue
        info = modes[idx].mode.targetMode.targetVideoSignalInfo
        denom = info.vSyncFreq.Denominator
        result[display_name] = {
            "width": int(info.activeSize.cx),
            "height": int(info.activeSize.cy),
            "refresh_hz": round(info.vSyncFreq.Numerator / denom, 3) if denom else None,
            "pixel_rate_hz": int(info.pixelRate),
            "scan_line_ordering": int(info.scanLineOrdering),
        }
    return result


def position_window_on_monitor(window: tk.Misc, monitor: dict, width: int, height: int) -> None:
    """Place a hidden Tk window using physical virtual-desktop coordinates.

    Tk's geometry syntax treats a negative x offset as right-edge anchoring;
    SetWindowPos accepts the actual negative virtual-screen coordinate.
    """
    window.geometry(f"{width}x{height}+0+0")
    window.update_idletasks()
    user32 = ctypes.windll.user32
    set_window_pos = user32.SetWindowPos
    set_window_pos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    set_window_pos.restype = wintypes.BOOL
    hwnd_topmost = wintypes.HWND(-1)
    flags = 0x0010  # SWP_NOACTIVATE
    # On Windows, Tk's winfo_id() can be the client child HWND. Move the
    # window-manager frame returned by `wm frame` so the outer window itself
    # reaches monitors with negative virtual-screen coordinates.
    try:
        frame_id = window.tk.call("wm", "frame", window._w)
        hwnd_value = int(str(frame_id), 0)
    except (tk.TclError, ValueError):
        hwnd_value = int(window.winfo_id())
    ok = set_window_pos(
        wintypes.HWND(hwnd_value), hwnd_topmost,
        monitor["left"], monitor["top"], width, height, flags,
    )
    if not ok:
        raise ctypes.WinError()


def show_monitor_identification(monitors: list[dict], ffmpeg: str, device: str,
                                width: int, height: int, fps: int,
                                input_pixel_format: str) -> np.ndarray | None:
    """Show a unique corner marker per monitor and sample the card's live input."""
    root = tk.Tk()
    root.withdraw()
    overlays = []
    captured = []
    capture_error = []
    colors = [(220, 32, 32), (32, 200, 48), (40, 64, 220), (220, 176, 24),
              (200, 32, 180), (24, 180, 190), (220, 96, 24), (112, 48, 200)]
    for index, monitor in enumerate(monitors, 1):
        rgb = colors[(index - 1) % len(colors)]
        color = "#%02X%02X%02X" % rgb
        window = tk.Toplevel(root)
        window.withdraw()
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.configure(background="#101010")
        position = {**monitor, "left": monitor["left"] + 18, "top": monitor["top"] + 18}
        label = tk.Label(
            window,
            text=f"{index}  {monitor['device']}",
            font=("Segoe UI", 18, "bold"),
            fg="white",
            bg="#101010",
            padx=10,
            pady=8,
            anchor="nw",
        )
        label.place(x=0, y=0, width=150, height=52)
        swatch = tk.Frame(window, background=color)
        swatch.place(x=155, y=56, width=54, height=36)
        window.deiconify()
        window.update_idletasks()
        position_window_on_monitor(window, position, 220, 100)
        overlays.append(window)

    def capture_probe():
        try:
            captured.append(capture_frame(ffmpeg, device, width, height, fps, input_pixel_format))
        except Exception as exc:
            capture_error.append(exc)

    print("Showing a numbered, color-coded corner marker on every display and sampling the selected capture input.")
    root.after(1300, capture_probe)
    root.after(3200, root.destroy)
    root.bind("<Escape>", lambda _event: root.destroy())
    root.mainloop()
    if capture_error:
        print(f"Display auto-detection probe failed: {capture_error[0]}", file=sys.stderr)
        return None
    return captured[0] if captured else None


def identify_monitor_from_probe(frame: np.ndarray, monitors: list[dict]) -> tuple[dict | None, list[dict]]:
    """Classify the color marker visible at each display's normalized top-left probe point."""
    colors = np.asarray([
        [220, 32, 32], [32, 200, 48], [40, 64, 220], [220, 176, 24],
        [200, 32, 180], [24, 180, 190], [220, 96, 24], [112, 48, 200],
    ], dtype=np.float64)
    height, width = frame.shape[:2]
    results = []
    for index, monitor in enumerate(monitors):
        # The sampled point is inside the marker's blank right-hand area,
        # away from the number glyphs. Scale it from desktop pixels to capture pixels.
        x = round(200 / monitor["width"] * width)
        y = round(92 / monitor["height"] * height)
        radius = max(3, round(8 / monitor["width"] * width))
        roi = frame[max(0, y-radius):min(height, y+radius+1),
                    max(0, x-radius):min(width, x+radius+1)]
        sample = np.median(roi.reshape(-1, 3), axis=0)
        expected = colors[index % len(colors)]
        distance = float(np.linalg.norm(sample - expected))
        results.append({"index": index + 1, "monitor": monitor, "sample_rgb": sample.round(1).tolist(),
                        "expected_marker_rgb": expected.astype(int).tolist(), "distance": round(distance, 1)})
    ranked = sorted(results, key=lambda result: result["distance"])
    # Large distance means the incoming feed likely has another picture or the marker was cropped.
    clear_winner = (
        len(ranked) == 1
        or (len(ranked) > 1 and ranked[1]["distance"] - ranked[0]["distance"] >= 25)
    )
    detected = ranked[0]["monitor"] if ranked and ranked[0]["distance"] <= 110 and clear_winner else None
    return detected, ranked


def choose_monitor(monitors: list[dict], capture_size: tuple[int, int],
                   signal_monitor: dict | None = None) -> dict:
    print("Displays:")
    exact_mode = [
        i for i, m in enumerate(monitors)
        if (m["width"], m["height"]) == capture_size
    ]
    for index, monitor in enumerate(monitors, 1):
        primary = " (primary)" if monitor["primary"] else ""
        likely = " [MOST LIKELY: exact capture resolution]" if index - 1 in exact_mode else ""
        signal = " [CAPTURE INPUT MATCHED THIS DISPLAY]" if signal_monitor == monitor else ""
        active = monitor.get("active_signal_mode")
        active_text = (
            f"; Active signal mode {active['width']}x{active['height']}@{active['refresh_hz']}Hz"
            if active else "; Active signal mode unavailable"
        )
        print(
            f"  {index}. {monitor['device']}  {monitor['width']}x{monitor['height']}"
            f" at ({monitor['left']},{monitor['top']}){active_text}{primary}{likely}{signal}"
        )
    while True:
        answer = input("Select the display connected to the capture card HDMI input: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(monitors):
            return monitors[int(answer) - 1]
        print("Enter one of the listed display numbers.")


def list_video_devices(ffmpeg: str) -> list[dict]:
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        capture_output=True, text=True, encoding="mbcs" if sys.platform == "win32" else "utf-8",
        errors="replace", timeout=20,
    )
    listing = proc.stderr + "\n" + proc.stdout
    devices = []
    current = None
    for line in listing.splitlines():
        # Recent FFmpeg builds may omit the "DirectShow video devices" heading
        # and print the camera records directly, which the original parser missed.
        match = re.search(r'"([^\"]+)"\s+\(video\)', line, re.IGNORECASE)
        if match:
            name = match.group(1)
            if not any(item["name"] == name for item in devices):
                current = {"name": name, "alternative_name": ""}
                devices.append(current)
            else:
                current = next(item for item in devices if item["name"] == name)
            continue
        alt = re.search(r'Alternative name\s+"([^\"]+)"', line, re.IGNORECASE)
        if alt and current is not None:
            current["alternative_name"] = alt.group(1)
    return devices


def video_device_score(device: dict) -> tuple[int, str]:
    name = device["name"].lower()
    alt = device["alternative_name"].lower()
    score, reason = 0, "no capture-card-specific marker"
    if "vid_345f&pid_2131" in alt:
        score, reason = 100, "USB VID/PID matches the attached MS2131 video interface"
    elif "usb3 video" in name:
        score, reason = 70, "name matches the attached USB3 Video device"
    elif "capture" in name or "video grabber" in name:
        score, reason = 50, "device name suggests a capture input"
    if any(token in name for token in ("virtual", "quest", "immersed", "privacy view", "投屏")) or "@device_sw_" in alt:
        score -= 100
        reason = "virtual camera; unlikely to be the physical capture card"
    return score, reason


def choose_video_device(devices: list[dict]) -> dict:
    if not devices:
        raise RuntimeError(
            "FFmpeg returned no DirectShow video-device entries. Run "
            "`ffmpeg -hide_banner -list_devices true -f dshow -i dummy` and check that the capture card appears."
        )
    ranked = sorted(devices, key=video_device_score, reverse=True)
    best_score = video_device_score(ranked[0])[0]
    print("Video capture devices:")
    for index, device in enumerate(ranked, 1):
        score, reason = video_device_score(device)
        likely = " [MOST LIKELY]" if score == best_score and score > 0 else ""
        alt = f" | {device['alternative_name']}" if device["alternative_name"] else ""
        print(f"  {index}. {device['name']}{likely}{alt}\n     {reason}")
    while True:
        answer = input("Select the capture card (UVC video device): ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(devices):
            return ranked[int(answer) - 1]
        print("Enter one of the listed device numbers.")


def make_chart(width: int, height: int) -> tuple[np.ndarray, list[dict], dict]:
    """Build a full-screen chart with gray, RGB-cube, and edge-response patches."""
    image = np.full((height, width, 3), 18, dtype=np.uint8)
    specs: list[dict] = []
    for value in GRAY_LEVELS:
        specs.append({"name": f"gray_{value:03d}", "rgb": [value] * 3, "kind": "neutral"})
    for red in (0, 128, 255):
        for green in (0, 128, 255):
            for blue in (0, 128, 255):
                if red == green == blue:
                    continue
                specs.append({
                    "name": f"rgb_{red:03d}_{green:03d}_{blue:03d}",
                    "rgb": [red, green, blue],
                    "kind": "color",
                })
    specs.extend([
        {"name": "black_white_edge", "rgb": None, "kind": "edge"},
        {"name": "fine_vertical_lines", "rgb": None, "kind": "lines"},
    ])
    if len(specs) > GRID_COLS * GRID_ROWS:
        raise ValueError("The calibration chart has more patches than grid cells.")

    usable_w = int(width * (1 - 2 * CHART_MARGIN))
    usable_h = int(height * (1 - 2 * CHART_MARGIN))
    cell_w, cell_h = usable_w / GRID_COLS, usable_h / GRID_ROWS
    patch_records = []
    for index, spec in enumerate(specs):
        row, col = divmod(index, GRID_COLS)
        x0 = round(CHART_MARGIN * width + col * cell_w + CHART_GAP * width / 2)
        x1 = round(CHART_MARGIN * width + (col + 1) * cell_w - CHART_GAP * width / 2)
        y0 = round(CHART_MARGIN * height + row * cell_h + CHART_GAP * height / 2)
        y1 = round(CHART_MARGIN * height + (row + 1) * cell_h - CHART_GAP * height / 2)
        if spec["kind"] in ("neutral", "color"):
            image[y0:y1, x0:x1] = np.asarray(spec["rgb"], dtype=np.uint8)
        elif spec["kind"] == "edge":
            yy, xx = np.mgrid[y0:y1, x0:x1]
            boundary = (x0 + x1) / 2 + 0.08 * (yy - (y0 + y1) / 2)
            image[y0:y1, x0:x1] = np.where((xx >= boundary)[..., None], 255, 0)
        else:
            xx = np.arange(x0, x1)
            stripes = ((xx // max(1, width // 480)) % 2) * 255
            image[y0:y1, x0:x1] = np.broadcast_to(stripes[None, :, None], (y1-y0, x1-x0, 3))
        spec.update({
            "sample_xy_normalized": [((x0 + x1) / 2) / width, ((y0 + y1) / 2) / height],
            "bounds_px": [x0, y0, x1, y1],
        })
        patch_records.append(spec)

    edge = next(p for p in patch_records if p["kind"] == "edge")
    chart_info = {
        "width": width,
        "height": height,
        "grid": [GRID_COLS, GRID_ROWS],
        "patches": patch_records,
        "edge_patch": edge,
    }
    return image, patch_records, chart_info


def write_ppm(path: Path, rgb: np.ndarray) -> None:
    height, width = rgb.shape[:2]
    with path.open("wb") as file:
        file.write(f"P6\n{width} {height}\n255\n".encode("ascii"))
        file.write(np.ascontiguousarray(rgb, dtype=np.uint8).tobytes())


def ppm_to_png(ffmpeg: str, source: Path) -> Path:
    output = source.with_suffix(".png")
    subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
         "-frames:v", "1", str(output)],
        check=True, timeout=30,
    )
    return output


def show_chart_on_monitor(chart_path: Path, monitor: dict, capture_callback) -> None:
    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(background="black")
    photo = tk.PhotoImage(file=str(chart_path))
    label = tk.Label(root, image=photo, borderwidth=0, highlightthickness=0)
    label.image = photo
    label.pack(fill="both", expand=True)
    root.deiconify()
    root.update_idletasks()
    position_window_on_monitor(root, monitor, monitor["width"], monitor["height"])
    root.bind("<Escape>", lambda _event: root.destroy())
    root.after(4000, lambda: capture_callback(root))
    root.mainloop()


def capture_frame(ffmpeg: str, device: str, width: int, height: int, fps: int,
                  input_pixel_format: str) -> np.ndarray:
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-rtbufsize", "512M",
        "-f", "dshow", "-framerate", str(fps), "-video_size", f"{width}x{height}",
        "-pixel_format", input_pixel_format, "-i", f"video={device}", "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]
    proc = subprocess.run(command, capture_output=True, timeout=40)
    if proc.returncode:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Could not capture {width}x{height}@{fps} from the selected device. "
            "Windows often reports this when another program (for example OBS, a browser, or Camera) "
            "already has the capture device open. Close that capture source and retry.\n"
            f"{detail}"
        )
    expected = width * height * 3
    if len(proc.stdout) != expected:
        raise RuntimeError(f"Capture returned {len(proc.stdout)} bytes; expected {expected} for one RGB frame.")
    return np.frombuffer(proc.stdout, dtype=np.uint8).reshape(height, width, 3).copy()


def sample_patches(frame: np.ndarray, patches: list[dict]) -> dict[str, list[float]]:
    height, width = frame.shape[:2]
    sampled = {}
    radius = max(2, min(width // GRID_COLS, height // GRID_ROWS) // 12)
    for patch in patches:
        nx, ny = patch["sample_xy_normalized"]
        x, y = round(nx * width), round(ny * height)
        roi = frame[max(0, y-radius):min(height, y+radius+1), max(0, x-radius):min(width, x+radius+1)]
        sampled[patch["name"]] = np.median(roi.reshape(-1, 3), axis=0).tolist()
    return sampled


def channel_curves(patches: list[dict], measured: dict[str, list[float]]) -> tuple[list[np.ndarray], list[np.ndarray]]:
    gray = [p for p in patches if p["kind"] == "neutral"]
    expected = np.asarray([p["rgb"] for p in gray], dtype=np.float64)
    captured = np.asarray([measured[p["name"]] for p in gray], dtype=np.float64)
    inputs, outputs = [], []
    for channel in range(3):
        order = np.argsort(captured[:, channel])
        x, y = captured[order, channel], expected[order, channel]
        unique = np.unique(x)
        means = np.asarray([y[x == value].mean() for value in unique])
        if unique.size < 2:
            raise RuntimeError("Grayscale samples did not produce a usable response curve.")
        inputs.append(unique)
        outputs.append(means)
    return inputs, outputs


def apply_tone(rgb: np.ndarray, inputs: np.ndarray, outputs: np.ndarray) -> np.ndarray:
    rgb = np.asarray(rgb, dtype=np.float64)
    return np.stack([
        np.interp(rgb[..., channel], inputs[channel], outputs[channel])
        for channel in range(3)
    ], axis=-1)


def fit_model(patches: list[dict], measured: dict[str, list[float]]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    inputs, outputs = channel_curves(patches, measured)
    colors = [p for p in patches if p["kind"] == "color"]
    raw = np.asarray([measured[p["name"]] for p in colors], dtype=np.float64)
    target = np.asarray([p["rgb"] for p in colors], dtype=np.float64)
    shaped = apply_tone(raw, inputs, outputs)
    design = np.column_stack((shaped, np.ones(len(shaped))))
    coefficients, *_ = np.linalg.lstsq(design, target, rcond=None)
    matrix, bias = coefficients[:3, :].T, coefficients[3, :]
    return inputs, outputs, matrix, bias


def apply_model(rgb: np.ndarray, model: tuple[np.ndarray, ...], strength: float) -> np.ndarray:
    inputs, outputs, matrix, bias = model
    shaped = apply_tone(rgb, inputs, outputs)
    high = shaped.max(axis=-1, keepdims=True)
    low = shaped.min(axis=-1, keepdims=True)
    saturation = np.divide(high - low, np.maximum(high, 1.0))
    affine = shaped @ matrix.T + bias
    return np.clip(shaped + strength * saturation * (affine - shaped), 0, 255)


def write_cube(path: Path, size: int, model: tuple[np.ndarray, ...], strength: float) -> None:
    with path.open("w", encoding="ascii", newline="\n") as file:
        file.write('TITLE "MS2131 live HDMI capture correction (experimental)"\n')
        file.write(f"LUT_3D_SIZE {size}\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n")
        axis = np.linspace(0, 255, size)
        for blue in axis:
            for green in axis:
                rgb = np.column_stack((axis, np.full(size, green), np.full(size, blue)))
                corrected = apply_model(rgb, model, strength) / 255.0
                for row in corrected:
                    file.write(f"{row[0]:.7f} {row[1]:.7f} {row[2]:.7f}\n")


def edge_metrics(frame: np.ndarray, edge_patch: dict, chart_width: int, chart_height: int) -> dict:
    """Measure a slanted-edge profile; LUT output is pointwise and cannot restore lost detail."""
    x0, y0, x1, y1 = edge_patch["bounds_px"]
    rgb = frame.astype(np.float64)
    gray = rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114
    # Bounds are scaled from the monitor-sized source chart into the captured frame.
    sx = frame.shape[1] / chart_width
    sy = frame.shape[0] / chart_height
    x0, x1, y0, y1 = round(x0*sx), round(x1*sx), round(y0*sy), round(y1*sy)
    profiles = []
    half = max(4, (x1-x0)//6)
    for y in range(y0+2, y1-2, max(1, (y1-y0)//300)):
        boundary = (x0+x1)/2 + 0.08*(y-(y0+y1)/2)*sx
        samples = [gray[y, min(frame.shape[1]-1, max(0, round(boundary+d)))]
                   for d in range(-half, half+1)]
        profiles.append(samples)
    profile = np.mean(profiles, axis=0)
    white, black = float(profile[-5:].mean()), float(profile[:5].mean())
    if white < black:
        profile = profile[::-1]
        white, black = black, white
    span = max(white-black, 1e-9)
    normalized = (profile-black)/span

    def cross(level: float) -> float:
        indices = np.flatnonzero(normalized >= level)
        if not len(indices):
            return float("nan")
        i = int(indices[0])
        if i == 0:
            return 0.0
        a, b = normalized[i-1], normalized[i]
        return float(i-1 + (level-a)/(b-a))

    return {
        "esf_10_90_width_px": round((cross(0.9)-cross(0.1)), 3),
        "overshoot_white_pct": round(max(0.0, (float(profile.max())-white)/span*100), 3),
        "undershoot_black_pct": round(max(0.0, (black-float(profile.min()))/span*100), 3),
        "black_luma": round(black, 3),
        "white_luma": round(white, 3),
    }


def convert_and_save(ffmpeg: str, source: Path) -> None:
    try:
        ppm_to_png(ffmpeg, source)
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"Could not make PNG preview for {source.name}: {exc}", file=sys.stderr)


def main() -> None:
    # Device names may contain characters outside the active Windows console
    # code page; preserve enumeration and replace only unprintable console glyphs.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-size", default="1920x1080", help="DirectShow capture mode (default: 1920x1080)")
    parser.add_argument("--fps", type=int, default=60, help="capture frame rate (default: 60)")
    parser.add_argument("--input-pixel-format", default="yuyv422",
                        help="DirectShow input format (default: yuyv422; use mjpeg only if required)")
    parser.add_argument("--size", type=int, default=33, help="3D LUT edge size (default: 33)")
    parser.add_argument("--strength", type=float, default=1.0, help="correction strength from 0 to 1")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable")
    args = parser.parse_args()
    try:
        width, height = (int(part) for part in args.capture_size.lower().split("x", 1))
    except ValueError:
        parser.error("--capture-size must look like 1920x1080")
    if width < 320 or height < 240 or args.fps < 1 or args.size < 2:
        parser.error("Capture dimensions, frame rate, and LUT size must be positive and usable.")
    if not 0 <= args.strength <= 1:
        parser.error("--strength must be between 0 and 1")
    ffmpeg = shutil.which(args.ffmpeg)
    if not ffmpeg:
        parser.error(f"ffmpeg executable not found: {args.ffmpeg}")

    monitors = enumerate_monitors()
    devices = list_video_devices(ffmpeg)
    device_info = choose_video_device(devices)
    device = device_info["name"]
    while True:
        probe = show_monitor_identification(
            monitors, ffmpeg, device, width, height, args.fps, args.input_pixel_format
        )
        if probe is not None:
            break
        retry = input(
            "Close any app using the capture card, then press Enter to retry this device; "
            "type D to choose a different device or Q to quit: "
        ).strip().lower()
        if retry == "q":
            return
        if retry == "d":
            device_info = choose_video_device(devices)
            device = device_info["name"]
    signal_monitor = None
    if probe is not None:
        signal_monitor, rankings = identify_monitor_from_probe(probe, monitors)
        print("Display-marker probe (closest marker color first):")
        for item in rankings:
            m = item["monitor"]
            marker = " [MATCH]" if m == signal_monitor else ""
            print(
                f"  {item['index']}. {m['device']}: sampled RGB {item['sample_rgb']}, "
                f"marker distance {item['distance']}{marker}"
            )
        if signal_monitor is None:
            print("No clear display marker was found in the capture; select the display manually.")
    monitor = choose_monitor(monitors, (width, height), signal_monitor)

    print("\nFor a source-faithful baseline, disable Windows Night Light/HDR and GPU/video enhancements,")
    print("use 1:1 desktop scaling, and keep the capture format/range fixed during calibration.")
    print("The script captures UVC frames directly; it does not use screenshots of the preview window.\n")
    input("Press Enter when the source and capture card are connected and ready. ")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    chart, patches, chart_info = make_chart(monitor["width"], monitor["height"])
    chart_path = OUTPUT_DIR / "calibration_source.ppm"
    write_ppm(chart_path, chart)
    convert_and_save(ffmpeg, chart_path)
    print(f"Showing the test chart on {monitor['device']} ({monitor['width']}x{monitor['height']}).")
    print("Leave the chart visible; capture starts automatically after four seconds. Press Esc to cancel.")

    capture_error: list[Exception] = []
    captured: list[np.ndarray] = []

    def do_capture(root):
        try:
            captured.append(capture_frame(
                ffmpeg, device, width, height, args.fps, args.input_pixel_format
            ))
        except Exception as exc:  # surfaced in the console after the display window closes
            capture_error.append(exc)
        finally:
            root.destroy()

    show_chart_on_monitor(chart_path, monitor, do_capture)
    if capture_error:
        raise capture_error[0]
    if not captured:
        print("Calibration cancelled.")
        return
    before = captured[0]
    if abs(before.shape[1] / before.shape[0] - monitor["width"] / monitor["height"]) > 0.01:
        raise RuntimeError("Capture aspect ratio differs from the selected monitor; set a matching mode and retry.")

    before_path = OUTPUT_DIR / "capture_before_lut.ppm"
    write_ppm(before_path, before)
    convert_and_save(ffmpeg, before_path)
    measured = sample_patches(before, patches)
    model = fit_model(patches, measured)
    corrected = np.rint(apply_model(before, model, args.strength)).astype(np.uint8)
    after_path = OUTPUT_DIR / "capture_after_lut_preview.ppm"
    write_ppm(after_path, corrected)
    convert_and_save(ffmpeg, after_path)

    comparison = np.concatenate((before, corrected), axis=1)
    comparison_path = OUTPUT_DIR / "capture_before_after_comparison.ppm"
    write_ppm(comparison_path, comparison)
    convert_and_save(ffmpeg, comparison_path)

    lut_path = OUTPUT_DIR / "ms2131_color_correction.cube"
    write_cube(lut_path, args.size, model, args.strength)
    predicted = sample_patches(corrected, patches)
    data_rows = []
    for patch in patches:
        if patch["kind"] not in ("neutral", "color"):
            continue
        expected = patch["rgb"]
        pre = measured[patch["name"]]
        post = predicted[patch["name"]]
        data_rows.append({
            "name": patch["name"], "kind": patch["kind"], "expected_rgb": expected,
            "measured_before_rgb": pre, "predicted_after_rgb": post,
            "error_before_rgb": [round(pre[c]-expected[c], 2) for c in range(3)],
            "error_after_rgb": [round(post[c]-expected[c], 2) for c in range(3)],
        })
    before_rmse = float(np.sqrt(np.mean([np.mean(np.square(row["error_before_rgb"])) for row in data_rows])))
    after_rmse = float(np.sqrt(np.mean([np.mean(np.square(row["error_after_rgb"])) for row in data_rows])))

    edge_patch = next(p for p in patches if p["kind"] == "edge")
    source_edge_response = edge_metrics(chart, edge_patch, monitor["width"], monitor["height"])
    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "monitor": monitor,
        "capture_device": {
            "name": device,
            "alternative_name": device_info["alternative_name"],
            "score_and_reason": video_device_score(device_info),
        },
        "capture_mode": {
            "width": width, "height": height, "fps": args.fps,
            "device_pixel_format": args.input_pixel_format,
            "saved_pixel_format": "RGB24 decoded by FFmpeg from DirectShow",
        },
        "chart": chart_info,
        "patch_measurements": data_rows,
        "edge_response_source_chart": source_edge_response,
        "edge_response_before": edge_metrics(before, edge_patch, monitor["width"], monitor["height"]),
        "edge_response_after_lut_prediction": edge_metrics(
            corrected, edge_patch, monitor["width"], monitor["height"]
        ),
        "color_patch_rmse_before": round(before_rmse, 3),
        "color_patch_rmse_after_lut_prediction": round(after_rmse, 3),
        "tone_curve_input_levels_by_channel": [axis.tolist() for axis in model[0]],
        "tone_curve_output_levels_by_channel": [axis.tolist() for axis in model[1]],
        "chroma_matrix": model[2].tolist(),
        "chroma_bias": model[3].tolist(),
        "lut_strength": args.strength,
        "lut_size": args.size,
        "artifacts": {
            "source_chart": str(chart_path), "capture_before_lut": str(before_path),
            "capture_after_lut_preview": str(after_path), "comparison": str(comparison_path),
            "lut": str(lut_path),
        },
        "limitations": (
            "After-LUT values are a software prediction from the captured frame, not a second physical capture. "
            "A pointwise LUT cannot restore spatial detail lost in the capture chain. The result depends on the "
            "selected display, GPU output mode, capture format/range, and Windows color pipeline."
        ),
    }
    report_path = OUTPUT_DIR / "calibration_data.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote source chart: {chart_path}")
    print(f"Wrote before capture: {before_path}")
    print(f"Wrote predicted after-LUT preview: {after_path}")
    print(f"Wrote comparison: {comparison_path}")
    print(f"Wrote measured data: {report_path}")
    print(f"Wrote 3D LUT: {lut_path}")
    print(f"Color patch RMSE: {before_rmse:.2f} -> {after_rmse:.2f} (predicted, 8-bit levels)")
    print(
        "Edge width source / captured / predicted after LUT: "
        f"{source_edge_response['esf_10_90_width_px']} / "
        f"{report['edge_response_before']['esf_10_90_width_px']} / "
        f"{report['edge_response_after_lut_prediction']['esf_10_90_width_px']} px"
    )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError, tk.TclError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
