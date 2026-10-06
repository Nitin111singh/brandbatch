#!/usr/bin/env python3
"""Reproduce a render failure inside the container it fails in, and print the real cause.

  python scripts/diagnose_render.py              # 9x16, balanced, 10s synthetic clip
  python scripts/diagnose_render.py --fmt original --quality high --seconds 20
  python scripts/diagnose_render.py --chroma     # also test the green-screen path

Builds its own test video and logo with ffmpeg and Pillow - it touches none of your
customers' files - then runs the real engine and reports the environment limits that
the usual suspects (memory, disk, ffmpeg build) depend on.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from app import engine as eng  # noqa: E402


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def read_first(*paths: str) -> str | None:
    for p in paths:
        try:
            return Path(p).read_text().strip()
        except OSError:
            continue
    return None


def report_environment():
    print("=" * 72)
    print("ENVIRONMENT")
    print("=" * 72)
    print(f"ffmpeg binary : {eng.FFMPEG}")
    try:
        v = subprocess.run([eng.FFMPEG, "-version"], capture_output=True, text=True, timeout=20)
        print(f"ffmpeg version: {v.stdout.splitlines()[0] if v.stdout else '(no output)'}")
    except Exception as exc:
        print(f"ffmpeg version: COULD NOT RUN - {exc}")

    # container memory limit (cgroup v2, then v1)
    limit = read_first("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes")
    current = read_first("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes")
    if limit and limit.isdigit():
        print(f"memory limit  : {human(int(limit))}")
    elif limit:
        print(f"memory limit  : {limit} (no container limit set)")
    if current and current.isdigit():
        print(f"memory in use : {human(int(current))}")

    # has anything in this container been OOM-killed before?
    events = read_first("/sys/fs/cgroup/memory.events")
    if events:
        oom = [ln for ln in events.splitlines() if ln.startswith(("oom ", "oom_kill "))]
        if oom:
            print(f"cgroup OOM    : {', '.join(oom)}   <-- non-zero oom_kill means a process "
                  f"here has already been killed for memory")

    storage = eng_storage_root()
    try:
        du = shutil.disk_usage(storage)
        print(f"storage dir   : {storage}")
        print(f"disk free     : {human(du.free)} free of {human(du.total)}")
        if du.free < 500 * 1024 * 1024:
            print("              ^^ under 500 MB free - a render can fail just from this")
    except OSError as exc:
        print(f"storage dir   : {storage} - CANNOT STAT: {exc}")
    print(f"WEB_CONCURRENCY={os.environ.get('WEB_CONCURRENCY', '(unset)')}  "
          f"EMBEDDED_WORKER={os.environ.get('EMBEDDED_WORKER', '(unset)')}")
    print()


def eng_storage_root() -> str:
    d = os.environ.get("STORAGE_DIR") or "/tmp"
    Path(d).mkdir(parents=True, exist_ok=True)
    return d


def make_input(path: Path, seconds: int, w: int, h: int):
    """A synthetic clip with video and audio, so the engine's audio path is exercised too."""
    cmd = [eng.FFMPEG, "-y", "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=30",
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
           "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not path.exists():
        print("Could not even build the test input. ffmpeg said:\n" + r.stderr[-1500:])
        raise SystemExit(2)


def make_logo(path: Path, chroma: bool):
    from PIL import Image, ImageDraw

    size = (400, 160)
    bg = (0, 255, 0, 255) if chroma else (0, 0, 0, 0)
    img = Image.new("RGBA", size, bg)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([10, 10, size[0] - 10, size[1] - 10], radius=20,
                        fill=(91, 61, 245, 230), outline=(255, 255, 255, 255), width=4)
    d.text((40, 60), "TEST LOGO", fill=(255, 255, 255, 255))
    img.save(path)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Reproduce and explain a render failure")
    p.add_argument("--fmt", default="9x16", help="9x16 | 1x1 | 16x9 | original (default 9x16)")
    p.add_argument("--quality", default="balanced", choices=sorted(eng.QUALITIES))
    p.add_argument("--seconds", type=int, default=10)
    p.add_argument("--width", type=int, default=1080)
    p.add_argument("--height", type=int, default=1920)
    p.add_argument("--chroma", action="store_true", help="test the green-screen logo path")
    p.add_argument("--keep", action="store_true", help="keep the generated files")
    a = p.parse_args(argv)

    report_environment()

    _, crf, preset = eng.QUALITIES[a.quality]
    work = Path(tempfile.mkdtemp(prefix="bb-diag-", dir=eng_storage_root()))
    src, logo = work / "input.mp4", work / "logo.png"
    out = work / "out.mp4"

    print("=" * 72)
    print(f"BUILDING TEST INPUT  {a.width}x{a.height}, {a.seconds}s, with audio")
    print("=" * 72)
    make_input(src, a.seconds, a.width, a.height)
    make_logo(logo, a.chroma)
    print(f"input: {human(src.stat().st_size)}   logo: {human(logo.stat().st_size)}\n")

    kit = eng.KitSpec(logo_path=str(logo), chroma=a.chroma)
    spec = eng.RenderSpec(input_path=str(src), output_path=str(out), kit=kit,
                          fmt=a.fmt, crf=crf, preset=preset)

    print("=" * 72)
    print(f"RENDERING  fmt={a.fmt} quality={a.quality} crf={crf} preset={preset} chroma={a.chroma}")
    print("=" * 72)
    cmd, total = eng.build_command(spec)
    print("ffmpeg command:\n  " + " ".join(cmd) + f"\n\nexpected duration: {total:.2f}s\n")

    rc = 0
    try:
        seconds = eng.render(spec, on_progress=lambda pct: None)
        print(f"SUCCESS: rendered {seconds:.2f}s -> {human(out.stat().st_size)}")
        print("\nThe engine works in this container with these settings. If real jobs still fail,")
        print("the difference is the input file or concurrency - try --seconds 60, the real")
        print("dimensions, and running two of these at once.")
    except eng.EngineError as exc:
        rc = 1
        print("FAILED. The engine reported:\n")
        print(exc)
    except Exception:
        rc = 1
        print("CRASHED before ffmpeg finished:\n")
        traceback.print_exc()
    finally:
        if a.keep:
            print(f"\nfiles kept in {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
