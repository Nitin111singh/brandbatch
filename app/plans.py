"""Plan catalogue.

Every limit here is enforced server-side (see app/services.py) and every line shown on the
pricing table is derived from these values, so the page can never drift from the real limits.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    price_inr: int
    brand_kits: int
    render_minutes: int
    max_files_per_job: int
    max_long_side: int          # 1280 = 720p class, 1920 = 1080p class
    max_fps: int                # source frame rate is kept, capped here
    qualities: tuple[str, ...]  # engine quality keys the plan may choose
    free_mark: bool
    priority: int               # higher renders first
    retention_hours: int
    blurb: str
    highlights: tuple[str, ...]

    @property
    def razorpay_plan_id(self) -> str | None:
        return os.environ.get(f"RAZORPAY_PLAN_{self.key.upper()}") or None

    @property
    def resolution_label(self) -> str:
        return "Up to 1080p" if self.max_long_side >= 1920 else "Up to 720p"

    @property
    def quality_label(self) -> str:
        return "Balanced + High" if "high" in self.qualities else "Balanced"


PLANS: dict[str, Plan] = {
    "free": Plan(
        key="free", name="Free", price_inr=0, brand_kits=1, render_minutes=10,
        max_files_per_job=5, max_long_side=1280, max_fps=30, qualities=("balanced",),
        free_mark=True, priority=0, retention_hours=48,
        blurb="Try it on a few videos.",
        highlights=("1 brand kit", "10 render minutes / month", "Up to 720p, 30 fps",
                    "Small “Made with BrandBatch” mark"),
    ),
    "creator": Plan(
        key="creator", name="Creator", price_inr=499, brand_kits=2, render_minutes=120,
        max_files_per_job=25, max_long_side=1920, max_fps=60, qualities=("balanced",),
        free_mark=False, priority=1, retention_hours=48,
        blurb="For solo creators and single brands.",
        highlights=("2 brand kits", "120 render minutes / month", "Up to 1080p, 60 fps",
                    "No BrandBatch mark"),
    ),
    "agency": Plan(
        key="agency", name="Agency", price_inr=1999, brand_kits=10, render_minutes=600,
        max_files_per_job=50, max_long_side=1920, max_fps=60, qualities=("balanced", "high"),
        free_mark=False, priority=2, retention_hours=48,
        blurb="For agencies branding videos for several clients.",
        highlights=("10 brand kits", "600 render minutes / month", "Up to 50 videos per job",
                    "High quality render option"),
    ),
    "agency_pro": Plan(
        key="agency_pro", name="Agency Pro", price_inr=4999, brand_kits=40, render_minutes=2000,
        max_files_per_job=100, max_long_side=1920, max_fps=60, qualities=("balanced", "high"),
        free_mark=False, priority=3, retention_hours=48,
        blurb="For large agencies and franchise networks.",
        highlights=("40 brand kits", "2,000 render minutes / month", "Up to 100 videos per job",
                    "Priority render queue"),
    ),
}
PAID_PLANS = [p for p in PLANS.values() if p.price_inr > 0]


@dataclass(frozen=True)
class TopupPack:
    """One-time purchase of extra render minutes for the current month."""
    key: str
    minutes: int
    price_inr: int

    @property
    def amount_paise(self) -> int:
        return self.price_inr * 100


TOPUP_PACKS: dict[str, TopupPack] = {
    "pack_100": TopupPack("pack_100", 100, 299),
    "pack_300": TopupPack("pack_300", 300, 799),
    "pack_1000": TopupPack("pack_1000", 1000, 2299),
}


def get_plan(key: str | None) -> Plan:
    return PLANS.get(key or "free", PLANS["free"])


def comparison_rows() -> list[tuple[str, str, dict[str, str]]]:
    """(label, help text, {plan key: value}) for the full comparison table."""
    def per(fn):
        return {k: fn(p) for k, p in PLANS.items()}

    yes, no = "Yes", "—"
    return [
        ("Price", "Billed monthly in INR. Cancel anytime.",
         per(lambda p: "Free" if not p.price_inr else f"₹{p.price_inr:,}/mo")),
        ("Brand kits", "One saved kit per client or brand: logo, position, size, intro, outro.",
         per(lambda p: str(p.brand_kits))),
        ("Render minutes / month", "Total length of the finished videos you create each month.",
         per(lambda p: f"{p.render_minutes:,}")),
        ("Videos per job", "How many videos you can upload in one batch.",
         per(lambda p: str(p.max_files_per_job))),
        ("Output resolution", "Longest side of the finished video.",
         per(lambda p: p.resolution_label)),
        ("Frame rate", "Your source frame rate is kept, up to this limit.",
         per(lambda p: f"Up to {p.max_fps} fps")),
        ("Render quality", "High uses a higher bitrate for sharper detail, with ~30% larger files.",
         per(lambda p: p.quality_label)),
        ("BrandBatch mark", "A small “Made with BrandBatch” strip at the bottom of the video.",
         per(lambda p: "On every video" if p.free_mark else "None")),
        ("Export formats", "9:16 Reels/Shorts, 1:1 square, 16:9 YouTube, plus the original shape.",
         per(lambda _p: "All 4")),
        ("Multi-brand jobs", "Brand one upload for several clients in a single job.",
         per(lambda p: yes if p.brand_kits > 1 else no)),
        ("Green-screen logo removal", "Use an animated logo shot on a solid colour background.",
         per(lambda _p: yes)),
        ("Intro and outro clips", "Stitched automatically before and after each video.",
         per(lambda _p: yes)),
        ("Render priority", "Which jobs the render workers pick up first when the queue is busy.",
         per(lambda p: ["Standard", "Standard", "Higher", "Highest"][p.priority])),
        ("File retention", "Uploads and outputs are deleted automatically after this long.",
         per(lambda p: f"{p.retention_hours} hours")),
        ("Support", "",
         per(lambda p: "Email" if p.price_inr < 1999 else "Priority email")),
    ]
