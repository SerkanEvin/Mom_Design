"""Settings, read once from the environment. Secrets are read from files, never logged."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _read_secret(env_value: str, env_file: str) -> str:
    """A secret comes from VAR or from the file named in VAR_FILE (file wins)."""
    path = os.environ.get(env_file, "").strip()
    if path:
        try:
            return Path(path).read_text().strip()
        except OSError:
            return ""
    return os.environ.get(env_value, "").strip()


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# Price table used for reservations (USD). Units follow fal's get_pricing on 2026-11-04.
# "per_call": flat per request; "per_mp": per started megapixel of each output image;
# "per_second": per compute second (charged from metrics.inference_time, min_charge otherwise).
DEFAULT_PRICES: dict[str, dict] = {
    "fal-ai/imageutils/rembg": {"per_second": 0.000575, "min_charge": 0.002},
    "fal-ai/hidream-o1-image/dev/edit": {"per_mp": 0.005},
    "fal-ai/qwen-image-edit-2511": {"per_mp": 0.03},
    "fal-ai/trellis/multi": {"per_call": 0.02},
    "fal-ai/hunyuan3d/v2/multi-view/turbo": {"per_call": 0.14},
    "fal-ai/hunyuan3d/v2/mini/turbo": {"per_call": 0.08},
}


# Effort levels the user picks (Orta / Yüksek / Ekstra). "high" is the behaviour from before efforts existed.
#   edit_rounds: Blender plan → run → check rounds     qa_redos: generated views re-made after a failed check
#   top_view:    also generate the top view (MOD-01)   texture: trellis texture size
#   careful:     planner/agent prompts ask for an extra self-check pass
EFFORTS: dict[str, dict] = {
    "mid": {"label": "Orta", "edit_rounds": 2, "qa_redos": 1, "top_view": False, "texture": 1024, "careful": False},
    "high": {"label": "Yüksek", "edit_rounds": 3, "qa_redos": 2, "top_view": False, "texture": 1024, "careful": False},
    "extra": {"label": "Ekstra", "edit_rounds": 5, "qa_redos": 3, "top_view": True, "texture": 2048, "careful": True},
}
DEFAULT_EFFORT = "high"


def effort(name: str | None) -> dict:
    """Settings of an effort level; unknown names fall back to the default."""
    key = name if name in EFFORTS else DEFAULT_EFFORT
    return {"name": key, **EFFORTS[key]}


def secret(name: str) -> str:
    """Current value of a secret: env/file first, then the copy saved from the admin page (data/secrets)."""
    s = get_settings()
    env = {"fal": ("FAL_KEY", "FAL_KEY_FILE"), "syn": ("SYN_API_KEY", "SYN_API_KEY_FILE"),
           "notify": ("AICP_NOTIFY_TOKEN", "AICP_NOTIFY_TOKEN_FILE")}[name]
    v = _read_secret(*env)
    if not v:
        p = s.data_dir / "secrets" / f"{name}.key"
        if p.exists():
            v = p.read_text().strip()
    return v


def save_secret(name: str, value: str) -> None:
    d = get_settings().data_dir / "secrets"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    p = d / f"{name}.key"
    p.write_text(value.strip())
    p.chmod(0o600)


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("STUDIO_DATA", "./data")).resolve())
    public_url: str = field(default_factory=lambda: os.environ.get("STUDIO_PUBLIC_URL", "https://asli.serkan.work"))
    cookie_secure: bool = field(default_factory=lambda: os.environ.get("STUDIO_COOKIE_SECURE", "1") == "1")

    fal_key: str = field(default_factory=lambda: _read_secret("FAL_KEY", "FAL_KEY_FILE"))
    syn_key: str = field(default_factory=lambda: _read_secret("SYN_API_KEY", "SYN_API_KEY_FILE"))
    syn_base_url: str = field(default_factory=lambda: os.environ.get("SYN_BASE_URL", "https://api.synthetic.new/anthropic"))
    syn_text_model: str = field(default_factory=lambda: os.environ.get("SYN_TEXT_MODEL", "syn:large:text"))
    syn_vision_model: str = field(default_factory=lambda: os.environ.get("SYN_VISION_MODEL", "syn:large:vision"))

    notify_url: str = field(default_factory=lambda: os.environ.get("AICP_NOTIFY_URL", ""))
    notify_token: str = field(default_factory=lambda: _read_secret("AICP_NOTIFY_TOKEN", "AICP_NOTIFY_TOKEN_FILE"))

    # Money rules (USD)
    auto_topup_usd: float = field(default_factory=lambda: _f("STUDIO_AUTO_TOPUP_USD", 5.0))
    warn_ratio: float = field(default_factory=lambda: _f("STUDIO_WARN_RATIO", 0.8))
    daily_cap_usd: float = field(default_factory=lambda: _f("STUDIO_DAILY_CAP_USD", 3.0))       # all users, guards against bugs
    fal_balance_usd: float = field(default_factory=lambda: _f("STUDIO_FAL_BALANCE_USD", 9.0))   # real fal balance Serkan reported
    member_llm_per_hour: int = field(default_factory=lambda: int(_f("STUDIO_MEMBER_LLM_PER_HOUR", 60)))
    # the design agent just does a user's first N sends; from send N+1 on it shows its plan and waits for "Tamam"
    agent_confirm_after: int = field(default_factory=lambda: int(_f("STUDIO_AGENT_CONFIRM_AFTER", 2)))
    agent_max_questions: int = field(default_factory=lambda: int(_f("STUDIO_AGENT_MAX_QUESTIONS", 2)))

    # Models
    view_model: str = field(default_factory=lambda: os.environ.get("STUDIO_VIEW_MODEL", "fal-ai/hidream-o1-image/dev/edit"))
    rembg_model: str = field(default_factory=lambda: os.environ.get("STUDIO_REMBG_MODEL", "fal-ai/imageutils/rembg"))
    recon_model: str = field(default_factory=lambda: os.environ.get("STUDIO_RECON_MODEL", "fal-ai/trellis/multi"))
    view_px: int = field(default_factory=lambda: int(_f("STUDIO_VIEW_PX", 768)))

    blender_bin: str = field(default_factory=lambda: os.environ.get("BLENDER_BIN", "/opt/blender/blender"))
    workers: int = field(default_factory=lambda: int(_f("STUDIO_WORKERS", 2)))
    prices: dict = field(default_factory=lambda: dict(DEFAULT_PRICES))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "studio.sqlite"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        for d in (_settings.data_dir, _settings.files_dir, _settings.jobs_dir):
            d.mkdir(parents=True, exist_ok=True)
    return _settings


def reset_settings(s: Settings | None = None) -> Settings:
    """Tests swap settings in."""
    global _settings
    _settings = s
    return get_settings()
