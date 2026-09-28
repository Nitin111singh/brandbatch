"""Video rendering engine (ffmpeg).

One render = one input video x one brand kit x one output format:
  1. reframe the main video to the target canvas (blur-fill background, or keep original)
  2. overlay the kit's logo (image or green-screen video) on the main part
  3. concatenate optional intro/outro clips (fitted to the same canvas)
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    import imageio_ffmpeg

    FFMPEG = os.environ.get("FFMPEG_BINARY") or imageio_ffmpeg.get_ffmpeg_exe()
except Exception:  # pragma: no cover
    FFMPEG = os.environ.get("FFMPEG_BINARY") or shutil.which("ffmpeg") or "ffmpeg"

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
LOGO_VIDEO_EXT = VIDEO_EXT | {".gif"}

# name -> (label, aspect w, aspect h); None = keep source aspect
FORMATS = {
    "original": ("Original", None, None),
    "9x16": ("9:16 Reels / Shorts", 9, 16),
    "1x1": ("1:1 Square", 1, 1),
    "16x9": ("16:9 YouTube", 16, 9),
}
# quality key -> (label, x264 crf, preset)
QUALITIES = {
    "balanced": ("Balanced (1080p, smaller files)", 22, "veryfast"),
    "high": ("High (best quality, ~30% larger)", 18, "medium"),
}
POSITIONS = [
    "top-left", "top-center", "top-right",
    "middle-left", "center", "middle-right",
    "bottom-left", "bottom-center", "bottom-right",
]


class EngineError(Exception):
    pass


@dataclass
class MediaInfo:
    width: int
    height: int
    duration: float | None
    has_audio: bool
    fps: float = 30.0


def probe(path: str | Path) -> MediaInfo:
    proc = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    out = proc.stderr
    video = re.search(r"Stream #\S+.*?Video:.*?, (\d{2,5})x(\d{2,5})", out)
    if not video:
        raise EngineError("No video or image stream found. Is this a valid media file?")
    w, h = int(video.group(1)), int(video.group(2))
    rot = re.search(r"rotate\s*:\s*(-?\d+)|rotation of (-?\d+(?:\.\d+)?)", out)
    if rot and abs(int(float(rot.group(1) or rot.group(2)))) % 180 == 90:
        w, h = h, w
    duration = None
    dur = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if dur:
        hh, mm, ss = dur.groups()
        duration = int(hh) * 3600 + int(mm) * 60 + float(ss)
        if duration <= 0:
            duration = None
    fps = 30.0
    rate = re.search(r"Stream #\S+.*?Video:.*?, (\d+(?:\.\d+)?) fps", out)
    if rate:
        try:
            parsed = float(rate.group(1))
            if 1 <= parsed <= 240:
                fps = parsed
        except ValueError:
            pass
    return MediaInfo(w, h, duration, bool(re.search(r"Stream #\S+.*?Audio:", out)), fps)


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def canvas_size(src: MediaInfo, fmt: str, max_long_side: int) -> tuple[int, int]:
    _, aw, ah = FORMATS[fmt]
    if aw is None:
        w, h = src.width, src.height
    else:
        # 1080 on the short side: 1080x1920, 1080x1080, 1920x1080
        if aw >= ah:
            w, h = 1080 * aw / ah, 1080
        else:
            w, h = 1080, 1080 * ah / aw
    scale = min(1.0, max_long_side / max(w, h))
    return _even(w * scale), _even(h * scale)


@dataclass
class KitSpec:
    logo_path: str | None = None
    position: str = "bottom-right"
    scale: float = 22.0
    margin: float = 3.0
    opacity: float = 100.0
    chroma: bool = False
    chroma_color: str = "00ff00"
    similarity: float = 0.30
    blend: float = 0.08
    loop_logo: bool = True
    intro_path: str | None = None
    outro_path: str | None = None


@dataclass
class RenderSpec:
    input_path: str
    output_path: str
    kit: KitSpec
    fmt: str = "original"
    max_long_side: int = 1920
    free_mark: bool = False     # small "Made with BrandBatch" text for free plan
    crf: int = 22
    preset: str = "veryfast"
    max_fps: int = 60           # source frame rate is kept, capped at this


def _free_mark_png(W: int, H: int, path: str) -> str:
    """Semi-transparent footer strip with 'Made with BrandBatch' (Pillow, no drawtext needed)."""
    from PIL import Image, ImageDraw, ImageFont

    bar_h = max(28, H // 22)
    img = Image.new("RGBA", (W, bar_h), (0, 0, 0, 90))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=int(bar_h * 0.5))
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    text = "Made with BrandBatch"
    box = draw.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    draw.text(((W - tw) / 2 - box[0], (bar_h - th) / 2 - box[1]), text, font=font, fill=(255, 255, 255, 230))
    img.save(path)
    return path


def _xy(position: str, m: int) -> tuple[str, str]:
    if position == "center":
        vert, horiz = "middle", "center"
    else:
        vert, horiz = position.split("-")
    x = {"left": f"{m}", "center": "(W-w)/2", "right": f"W-w-{m}"}[horiz]
    y = {"top": f"{m}", "middle": "(H-h)/2", "bottom": f"H-h-{m}"}[vert]
    return x, y


def build_command(spec: RenderSpec) -> tuple[list[str], float]:
    """Return (ffmpeg argv, expected output duration in seconds)."""
    if spec.fmt not in FORMATS:
        raise EngineError(f"Unknown format {spec.fmt}")
    kit = spec.kit
    if kit.position not in POSITIONS:
        raise EngineError("Invalid logo position")
    main = probe(spec.input_path)
    if not main.duration:
        raise EngineError("Could not read the video duration")
    W, H = canvas_size(main, spec.fmt, spec.max_long_side)
    Path(spec.output_path).parent.mkdir(parents=True, exist_ok=True)
    # Keep the source frame rate instead of forcing 30: a 60 fps clip stays 60, 24 fps stays 24.
    # With an intro/outro attached every segment must share one rate, so use the fastest of them.
    clip_rates = [main.fps]
    for clip in (kit.intro_path, kit.outro_path):
        if clip:
            try:
                clip_rates.append(probe(clip).fps)
            except EngineError:
                pass
    fps = min(spec.max_fps, max(1, round(max(clip_rates))))

    inputs: list[list[str]] = [["-i", spec.input_path]]
    filters: list[str] = []
    segments: list[tuple[str, str]] = []  # (video label, audio label)
    total = 0.0

    def fit(label_in: str, label_out: str, blur: bool):
        common = f"fps={fps},setsar=1,format=yuv420p"
        if blur:
            filters.append(
                f"[{label_in}]split=2[{label_out}bg0][{label_out}fg0];"
                f"[{label_out}bg0]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                f"gblur=sigma=30,eq=brightness=-0.08[{label_out}bg];"
                f"[{label_out}fg0]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos[{label_out}fg];"
                f"[{label_out}bg][{label_out}fg]overlay=(W-w)/2:(H-h)/2,{common}[{label_out}]"
            )
        else:
            filters.append(
                f"[{label_in}]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,{common}[{label_out}]"
            )

    def audio_for(idx: int, info: MediaInfo, label: str):
        if info.has_audio:
            filters.append(f"[{idx}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                           f"atrim=duration={info.duration:.3f},asetpts=PTS-STARTPTS[{label}]")
        else:
            filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={info.duration:.3f},"
                           f"aformat=sample_fmts=fltp[{label}]")

    def add_clip(path: str, name: str):
        nonlocal total
        info = probe(path)
        if not info.duration:
            raise EngineError(f"Could not read the {name} clip duration")
        idx = len(inputs)
        inputs.append(["-i", path])
        filters.append(f"[{idx}:v]trim=duration={info.duration:.3f},setpts=PTS-STARTPTS[{name}raw]")
        fit(f"{name}raw", f"{name}v", blur=False)
        audio_for(idx, info, f"{name}a")
        segments.append((f"{name}v", f"{name}a"))
        total += info.duration

    if kit.intro_path:
        add_clip(kit.intro_path, "intro")

    # --- main segment
    blur = FORMATS[spec.fmt][1] is not None
    fit("0:v", "mainfit", blur=blur)
    current = "mainfit"
    if kit.logo_path:
        logo_ext = Path(kit.logo_path).suffix.lower()
        is_image = logo_ext in IMAGE_EXT
        idx = len(inputs)
        if is_image:
            inputs.append(["-loop", "1", "-i", kit.logo_path])
        elif kit.loop_logo:
            inputs.append(["-stream_loop", "-1", "-i", kit.logo_path])
        else:
            inputs.append(["-i", kit.logo_path])
        logo_w = _even(W * kit.scale / 100)
        chain = [f"scale={logo_w}:-2:flags=lanczos", "format=rgba"]
        if kit.chroma:
            chain.append(f"colorkey=0x{kit.chroma_color}:{kit.similarity:.3f}:{kit.blend:.3f}")
        if kit.opacity < 100:
            chain.append(f"colorchannelmixer=aa={kit.opacity / 100:.3f}")
        filters.append(f"[{idx}:v]{','.join(chain)}[logo]")
        x, y = _xy(kit.position, int(round(W * kit.margin / 100)))
        eof = "pass" if (not is_image and not kit.loop_logo) else "repeat"
        filters.append(f"[{current}][logo]overlay=x={x}:y={y}:eof_action={eof}:format=auto,"
                       f"trim=duration={main.duration:.3f},format=yuv420p[mainlogo]")
        current = "mainlogo"
    if spec.free_mark:
        mark_path = _free_mark_png(W, H, str(Path(spec.output_path).with_suffix(".mark.png")))
        idx = len(inputs)
        inputs.append(["-loop", "1", "-i", mark_path])
        filters.append(f"[{current}][{idx}:v]overlay=0:H-h:eof_action=repeat:format=auto,"
                       f"trim=duration={main.duration:.3f},format=yuv420p[mainmark]")
        current = "mainmark"
    audio_for(0, main, "maina")
    segments.append((current, "maina"))
    total += main.duration

    if kit.outro_path:
        add_clip(kit.outro_path, "outro")

    if len(segments) > 1:
        pairs = "".join(f"[{v}][{a}]" for v, a in segments)
        filters.append(f"{pairs}concat=n={len(segments)}:v=1:a=1[vout][aout]")
        vout, aout = "vout", "aout"
    else:
        vout, aout = segments[0]

    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y"]
    for i in inputs:
        cmd += i
    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", f"[{vout}]", "-map", f"[{aout}]",
        "-c:v", "libx264", "-preset", spec.preset, "-crf", str(spec.crf), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
        "-t", f"{total:.3f}",
        "-progress", "pipe:1", "-nostats", spec.output_path,
    ]
    return cmd, total


def render(spec: RenderSpec, on_progress: Callable[[float], None] | None = None,
           should_cancel: Callable[[], bool] | None = None) -> float:
    """Render and return output duration (seconds). Raises EngineError on failure."""
    cmd, total = build_command(spec)
    Path(spec.output_path).parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    tail: list[str] = []

    def drain():
        for line in proc.stderr:
            tail.append(line)
            del tail[:-30]

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    for line in proc.stdout:
        if should_cancel and should_cancel():
            proc.kill()
            break
        if line.startswith("out_time_us=") and on_progress and total:
            try:
                on_progress(min(99.0, int(line.split("=", 1)[1]) / 1e6 / total * 100))
            except ValueError:
                pass
    proc.wait()
    t.join(timeout=5)
    if should_cancel and should_cancel():
        raise EngineError("Cancelled")
    out = Path(spec.output_path)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise EngineError("".join(tail[-6:]).strip()[-800:] or f"ffmpeg failed ({proc.returncode})")
    return total
