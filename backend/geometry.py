"""Crop coordinate math, bounds clamping, keyframe interpolation, and FFmpeg filter builders."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from backend.schemas import CropKeyframe

COORD_EPSILON = 1e-6


class GeometryValidationError(ValueError):
    """Raised when crop keyframes or geometry parameters violate bounds or ordering."""


@dataclass(frozen=True)
class CropBox:
    """Evaluated pixel-space 9:16 crop box at a given timestamp."""

    x_px: float
    y_px: float
    w_px: float
    h_px: float
    zoom: float

    def even_size(self) -> tuple[int, int]:
        """Return even integer (width, height) aligned for yuv420p chroma subsampling."""
        return round_even(self.w_px), round_even(self.h_px)


def round_even(value: float) -> int:
    """Round a positive dimension to the nearest even integer >= 2."""
    return max(2, int(round(value / 2.0)) * 2)


def compute_ideal_crop_size(
    width: int,
    height: int,
    zoom: float,
    output_width: int = 1080,
    output_height: int = 1920,
) -> tuple[float, float]:
    """Compute ideal (unrounded) crop dimensions in source pixels for target output aspect and zoom."""
    if width <= 0 or height <= 0:
        raise GeometryValidationError(f"Source dimensions must be positive, got {width}x{height}")
    if output_width <= 0 or output_height <= 0:
        raise GeometryValidationError(
            f"Output dimensions must be positive, got {output_width}x{output_height}"
        )
    if not math.isfinite(zoom) or zoom < 1.0 or zoom > 3.0:
        raise GeometryValidationError(f"zoom must be finite and in [1.0, 3.0], got {zoom!r}")

    target_aspect = float(output_width) / float(output_height)
    source_aspect = float(width) / float(height)
    if source_aspect >= target_aspect:
        base_crop_h = float(height)
        base_crop_w = float(height) * target_aspect
    else:
        base_crop_w = float(width)
        base_crop_h = float(width) / target_aspect

    ideal_crop_h = base_crop_h / zoom
    ideal_crop_w = base_crop_w / zoom
    return ideal_crop_w, ideal_crop_h


def clamp_normalized_coords(
    x: float,
    y: float,
    zoom: float,
    width: int,
    height: int,
    output_width: int = 1080,
    output_height: int = 1920,
) -> tuple[float, float]:
    """Clamp normalized top-left (x, y) so the crop box stays inside [0, W] x [0, H]."""
    ideal_w, ideal_h = compute_ideal_crop_size(
        width, height, zoom, output_width=output_width, output_height=output_height
    )
    max_x_px = max(0.0, float(width) - ideal_w)
    max_y_px = max(0.0, float(height) - ideal_h)

    x_px = min(max(float(x) * float(width), 0.0), max_x_px)
    y_px = min(max(float(y) * float(height), 0.0), max_y_px)
    return x_px / float(width), y_px / float(height)


def validate_keyframes(
    keyframes: Sequence[CropKeyframe],
    duration: float,
    width: int,
    height: int,
    output_width: int = 1080,
    output_height: int = 1920,
) -> None:
    """Validate keyframe timestamps, zoom range, and persisted normalized top-left bounds."""
    if not math.isfinite(duration) or duration <= 0.0:
        raise GeometryValidationError(f"duration must be finite and > 0, got {duration!r}")
    if width <= 0 or height <= 0:
        raise GeometryValidationError(f"Source dimensions must be positive, got {width}x{height}")
    if output_width <= 0 or output_height <= 0:
        raise GeometryValidationError(
            f"Output dimensions must be positive, got {output_width}x{output_height}"
        )
    if not keyframes:
        raise GeometryValidationError("keyframes must be non-empty")

    if not math.isfinite(keyframes[0].t) or keyframes[0].t != 0.0:
        raise GeometryValidationError(
            f"First keyframe must be at t == 0.0, got {keyframes[0].t!r}"
        )

    prev_t: float | None = None
    for idx, kf in enumerate(keyframes):
        if not math.isfinite(kf.t):
            raise GeometryValidationError(f"Keyframe {idx} timestamp t is not finite: {kf.t!r}")
        if prev_t is not None and kf.t <= prev_t:
            raise GeometryValidationError(
                f"Keyframe timestamps must be strictly increasing (index {idx}: {kf.t} <= {prev_t})"
            )
        if kf.t > duration + COORD_EPSILON:
            raise GeometryValidationError(
                f"Keyframe {idx} timestamp t={kf.t} exceeds segment duration {duration}"
            )
        prev_t = kf.t

        if not (math.isfinite(kf.x) and math.isfinite(kf.y) and math.isfinite(kf.zoom)):
            raise GeometryValidationError(f"Keyframe {idx} has non-finite coordinates or zoom")

        ideal_w, ideal_h = compute_ideal_crop_size(
            width, height, kf.zoom, output_width=output_width, output_height=output_height
        )
        max_x_norm = max(0.0, (float(width) - ideal_w) / float(width))
        max_y_norm = max(0.0, (float(height) - ideal_h) / float(height))

        if kf.x < -COORD_EPSILON or kf.x > max_x_norm + COORD_EPSILON:
            raise GeometryValidationError(
                f"Keyframe {idx} x={kf.x} out of bounds [0.0, {max_x_norm:.6f}] for zoom={kf.zoom}"
            )
        if kf.y < -COORD_EPSILON or kf.y > max_y_norm + COORD_EPSILON:
            raise GeometryValidationError(
                f"Keyframe {idx} y={kf.y} out of bounds [0.0, {max_y_norm:.6f}] for zoom={kf.zoom}"
            )


def _apply_easing(u: float, transition: str) -> float:
    """Map linear progress u in [0, 1] through the selected keyframe easing curve."""
    u = max(0.0, min(1.0, u))
    if transition == "ease_in":
        return u * u
    if transition == "ease_out":
        inv = 1.0 - u
        return 1.0 - inv * inv
    if transition == "ease_in_out":
        return u * u * (3.0 - 2.0 * u)
    return u


def evaluate_crop_at(
    keyframes: Sequence[CropKeyframe],
    t: float,
    width: int,
    height: int,
    output_width: int = 1080,
    output_height: int = 1920,
) -> CropBox:
    """Interpolate (x, y, zoom) at timestamp t with easing/snap, clamping t <= 0 and holding final keyframe."""
    if not keyframes:
        raise GeometryValidationError("keyframes must be non-empty")
    if width <= 0 or height <= 0:
        raise GeometryValidationError(f"Source dimensions must be positive, got {width}x{height}")

    if t <= keyframes[0].t:
        x_norm, y_norm, zoom = keyframes[0].x, keyframes[0].y, keyframes[0].zoom
    elif t >= keyframes[-1].t:
        x_norm, y_norm, zoom = keyframes[-1].x, keyframes[-1].y, keyframes[-1].zoom
    else:
        x_norm, y_norm, zoom = keyframes[-1].x, keyframes[-1].y, keyframes[-1].zoom
        for idx in range(len(keyframes) - 1):
            k0 = keyframes[idx]
            k1 = keyframes[idx + 1]
            if k0.t <= t < k1.t or (idx == len(keyframes) - 2 and k0.t <= t <= k1.t):
                dt = k1.t - k0.t
                if k0.transition == "snap" and t < k1.t:
                    alpha = 0.0
                else:
                    raw_u = 0.0 if dt <= 0.0 else (t - k0.t) / dt
                    alpha = _apply_easing(raw_u, k0.transition)
                x_norm = k0.x + (k1.x - k0.x) * alpha
                y_norm = k0.y + (k1.y - k0.y) * alpha
                zoom = k0.zoom + (k1.zoom - k0.zoom) * alpha
                break

    ideal_w, ideal_h = compute_ideal_crop_size(
        width, height, zoom, output_width=output_width, output_height=output_height
    )
    max_x_px = max(0.0, float(width) - ideal_w)
    max_y_px = max(0.0, float(height) - ideal_h)

    x_px = min(max(x_norm * float(width), 0.0), max_x_px)
    y_px = min(max(y_norm * float(height), 0.0), max_y_px)
    return CropBox(x_px=x_px, y_px=y_px, w_px=ideal_w, h_px=ideal_h, zoom=zoom)


def slice_camera_keyframes_for_segment(
    camera_keyframes: Sequence[CropKeyframe],
    clip_start: float,
    clip_dur: float,
    width: int,
    height: int,
    output_width: int = 1080,
    output_height: int = 1920,
) -> list[CropKeyframe]:
    """Project-level camera_keyframes -> segment-local keyframes over [clip_start, clip_start + clip_dur]."""
    if not camera_keyframes:
        raise GeometryValidationError("camera_keyframes must be non-empty")

    def _active_transition_at(t_global: float) -> str:
        for idx in range(len(camera_keyframes) - 1):
            k0 = camera_keyframes[idx]
            k1 = camera_keyframes[idx + 1]
            if k0.t <= t_global < k1.t:
                return k0.transition
        return "tween"

    box_start = evaluate_crop_at(
        camera_keyframes,
        clip_start,
        width,
        height,
        output_width=output_width,
        output_height=output_height,
    )
    start_x, start_y = clamp_normalized_coords(
        box_start.x_px / float(width),
        box_start.y_px / float(height),
        box_start.zoom,
        width,
        height,
        output_width=output_width,
        output_height=output_height,
    )
    result: list[CropKeyframe] = [
        CropKeyframe(
            t=0.0,
            x=round(start_x, 6),
            y=round(start_y, 6),
            zoom=round(box_start.zoom, 6),
            transition=_active_transition_at(clip_start),  # type: ignore[arg-type]
        )
    ]

    clip_end = clip_start + max(0.0, clip_dur)
    last_global_t = clip_start
    for kf in camera_keyframes:
        if clip_start + 1e-4 < kf.t < clip_end - 1e-4:
            cx, cy = clamp_normalized_coords(
                kf.x,
                kf.y,
                kf.zoom,
                width,
                height,
                output_width=output_width,
                output_height=output_height,
            )
            result.append(
                CropKeyframe(
                    t=round(kf.t - clip_start, 6),
                    x=round(cx, 6),
                    y=round(cy, 6),
                    zoom=round(kf.zoom, 6),
                    transition=kf.transition,
                )
            )
            last_global_t = kf.t

    if clip_dur > 1e-4 and camera_keyframes[-1].t > last_global_t + 1e-4:
        # Check if there is a keyframe at or after clip_end
        if any(kf.t >= clip_end - 1e-4 for kf in camera_keyframes):
            box_end = evaluate_crop_at(
                camera_keyframes,
                clip_end,
                width,
                height,
                output_width=output_width,
                output_height=output_height,
            )
            end_x, end_y = clamp_normalized_coords(
                box_end.x_px / float(width),
                box_end.y_px / float(height),
                box_end.zoom,
                width,
                height,
                output_width=output_width,
                output_height=output_height,
            )
            local_end = round(clip_dur, 6)
            if local_end > result[-1].t + 1e-5:
                result.append(
                    CropKeyframe(
                        t=local_end,
                        x=round(end_x, 6),
                        y=round(end_y, 6),
                        zoom=round(box_end.zoom, 6),
                        transition="tween",
                    )
                )

    return result


def detect_trim_out_of_range(
    keyframes: Sequence[CropKeyframe],
    new_duration: float,
) -> list[int]:
    """Return indices of keyframes whose timestamps exceed new_duration (never silently discarding)."""
    return [idx for idx, kf in enumerate(keyframes) if kf.t > new_duration]


def _piecewise_linear_expr(
    times: Sequence[float],
    values: Sequence[float],
    time_var: str = "t",
    transitions: Sequence[str] | None = None,
) -> str:
    """Build a nested FFmpeg piecewise-linear/eased/step expression over `time_var` with final hold."""
    if len(values) == 1:
        return f"{values[0]:.6f}"

    expr = f"{values[-1]:.6f}"
    for idx in range(len(values) - 2, -1, -1):
        t0 = times[idx]
        t1 = times[idx + 1]
        v0 = values[idx]
        v1 = values[idx + 1]
        dt = t1 - t0
        tr = transitions[idx] if transitions is not None and idx < len(transitions) else "tween"
        if dt <= 0.0 or tr == "snap" or abs(v1 - v0) < 1e-12:
            seg_expr = f"{v0:.6f}"
        else:
            dv = v1 - v0
            u_expr = f"clip(({time_var}-{t0:.6f})/{dt:.6f},0,1)"
            if tr == "ease_in":
                alpha_expr = f"pow({u_expr},2)"
            elif tr == "ease_out":
                alpha_expr = f"(1-pow(1-{u_expr},2))"
            elif tr == "ease_in_out":
                alpha_expr = f"(3*pow({u_expr},2)-2*pow({u_expr},3))"
            else:
                alpha_expr = f"({time_var}-{t0:.6f})/{dt:.6f}"
            seg_expr = f"({v0:.6f}+({dv:.6f})*{alpha_expr})"
        expr = f"if(lt({time_var},{t1:.6f}),{seg_expr},{expr})"

    return expr


def build_crop_filter(
    keyframes: Sequence[CropKeyframe],
    width: int,
    height: int,
    duration: float,
    fps: int = 30,
    output_width: int = 1080,
    output_height: int = 1920,
) -> str:
    """Build an FFmpeg video filtergraph string for static or animated crop/zoom to output_width x output_height."""
    validate_keyframes(
        keyframes,
        duration=duration,
        width=width,
        height=height,
        output_width=output_width,
        output_height=output_height,
    )
    if fps <= 0:
        raise GeometryValidationError(f"fps must be positive, got {fps}")

    out_w = round_even(float(output_width))
    out_h = round_even(float(output_height))
    constant_zoom = all(abs(kf.zoom - keyframes[0].zoom) < 1e-9 for kf in keyframes)

    if constant_zoom:
        zoom = keyframes[0].zoom
        ideal_w, ideal_h = compute_ideal_crop_size(
            width, height, zoom, output_width=out_w, output_height=out_h
        )
        crop_w = min(width, round_even(ideal_w))
        crop_h = min(height, round_even(ideal_h))
        max_x = max(0.0, float(width - crop_w))
        max_y = max(0.0, float(height - crop_h))

        times = [kf.t for kf in keyframes]
        transitions = [kf.transition for kf in keyframes]
        xs = [min(max(kf.x * float(width), 0.0), max_x) for kf in keyframes]
        ys = [min(max(kf.y * float(height), 0.0), max_y) for kf in keyframes]

        if len(keyframes) == 1:
            crop_part = f"crop=w={crop_w}:h={crop_h}:x={xs[0]:.4f}:y={ys[0]:.4f}"
        else:
            x_expr = f"clip({_piecewise_linear_expr(times, xs, 't', transitions)},0,{max_x:.4f})"
            y_expr = f"clip({_piecewise_linear_expr(times, ys, 't', transitions)},0,{max_y:.4f})"
            crop_part = f"crop=w={crop_w}:h={crop_h}:x='{x_expr}':y='{y_expr}'"

        return f"{crop_part},scale={out_w}:{out_h}:flags=lanczos,setsar=1,fps={fps}"

    # Animated zoom: pad to target aspect canvas so zoompan preserves output aspect with zero distortion
    # while allowing full horizontal and vertical panning across the (W, H) source frame.
    target_aspect = float(out_w) / float(out_h)
    source_aspect = float(width) / float(height)
    if source_aspect >= target_aspect:
        pad_w = round_even(float(width))
        pad_h = max(height, round_even(float(width) / target_aspect))
        base_crop_w = float(height) * target_aspect
    else:
        pad_h = round_even(float(height))
        pad_w = max(width, round_even(float(height) * target_aspect))
        base_crop_w = float(width)

    zoom_scale = float(pad_w) / base_crop_w

    times = [kf.t for kf in keyframes]
    transitions = [kf.transition for kf in keyframes]
    zooms = [kf.zoom for kf in keyframes]
    xs = [kf.x * float(width) for kf in keyframes]
    ys = [kf.y * float(height) for kf in keyframes]

    z_expr = f"({zoom_scale:.8f})*({_piecewise_linear_expr(times, zooms, 'it', transitions)})"
    x_raw = _piecewise_linear_expr(times, xs, "it", transitions)
    y_raw = _piecewise_linear_expr(times, ys, "it", transitions)
    x_expr = f"clip({x_raw},0,max(0,{width}-iw/zoom))"
    y_expr = f"clip({y_raw},0,max(0,{height}-ih/zoom))"

    return (
        f"fps={fps},"
        f"pad=w={pad_w}:h={pad_h}:x=0:y=0:color=black,"
        f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':d=1:fps={fps}:s={out_w}x{out_h},"
        f"setsar=1"
    )
