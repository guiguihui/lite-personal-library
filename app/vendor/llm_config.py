"""Shared LLM + pipeline config for the translation pipeline.

REFACTOR: get_tier now delegates to app.llm.config.resolve_for_tier, which
reads the app's canonical llm.yaml + keyring (not config.yaml/.env). The
legacy config.yaml/.env path is preserved as a fallback when no active app
config has been injected.

Injection contract: the orchestrator (e.g. translate_chapters.py main, or
the FastAPI app bootstrap) MUST call set_active_config(cfg, config_dir)
before invoking get_tier, so the vendor script can reach the app's config
store without breaking its sibling-import ergonomics.

Usage:
    from llm_config import set_active_config, get_tier, get_pipeline_config
    set_active_config(llm_cfg, config_dir)   # inject once at startup
    api_key, base_url, model, max_tokens = get_tier("strong")
"""
from __future__ import annotations

import os
from typing import Optional

# ── Legacy path setup (kept for fallback + pipeline/segment toggles) ────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.join(SCRIPT_DIR, "..", "..", "..", "..")
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.yaml")
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH)
except ImportError:
    pass  # rely on env vars being set in the shell

# ── Legacy config.yaml (optional; pipeline/segment toggles still live here) ─
_legacy_yaml = None
try:
    import yaml
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            _legacy_yaml = yaml.safe_load(f) or {}
except ImportError:
    pass  # PyYAML not installed — fall back to legacy single-model mode

# ── Defaults (legacy single-model behavior) ───────────────────────────────
_DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
_DEFAULT_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
_DEFAULT_API_KEY = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
_DEFAULT_PIPELINE = {
    "review": True,
    "consistency_qa": True,
    "backtranslate": False,
    "autofix_severe": True,
}
_DEFAULT_SEGMENT = {
    "max_chars_per_batch": 4500,
    "max_chars_per_segment": 2000,
}
_LEGACY_MAX_TOKENS = {
    "strong": 8192,
    "cheap": 4096,
    "fast": 4096,
}

# ── Injected app config (the new path) ────────────────────────────────────
# Set once at startup by the orchestrator. None => legacy fallback.
_active_cfg = None  # type: Optional["LlmConfig"]  # noqa: F821
_active_config_dir: Optional[str] = None


def set_active_config(cfg, config_dir: str) -> None:
    """Inject the app's canonical LlmConfig + config_dir.

    Must be called before get_tier() if the vendor script should resolve
    through app.llm.config.resolve_for_tier (llm.yaml + keyring). If never
    called, get_tier degrades to the legacy config.yaml/.env behavior.
    """
    global _active_cfg, _active_config_dir
    _active_cfg = cfg
    _active_config_dir = config_dir


def _api_key_for(base_url: str) -> str:
    """Legacy env-var key resolution (fallback path only)."""
    if "deepseek" in base_url:
        return os.environ.get("DEEPSEEK_API_KEY") or _DEFAULT_API_KEY
    if "openai" in base_url:
        return os.environ.get("OPENAI_API_KEY") or _DEFAULT_API_KEY
    if "mimo" in base_url or "xiaomimimo" in base_url:
        return os.environ.get("MIMO_API_KEY", "")
    if "glm" in base_url or "zhipu" in base_url:
        return os.environ.get("GLM_API_KEY") or os.environ.get("ZHIPUAI_API_KEY", "")
    return _DEFAULT_API_KEY


def get_tier(name: str):
    """Return (api_key, base_url, model, max_tokens) for a tier.

    Signature UNCHANGED. Resolution order:
      1. If set_active_config() was called -> app.llm.config.resolve_for_tier
         (reads llm.yaml + keyring via the app config store).
         NOTE: resolve_for_tier returns (provider, model, base_url, api_key);
         we reorder to the legacy (api_key, base_url, model, max_tokens) tuple
         and synthesize max_tokens from _LEGACY_MAX_TOKENS (app schema has no
         per-tier max_tokens field in MVP).
      2. Else -> legacy config.yaml["llm"]["tiers"][name] + .env keys.
      3. Else -> legacy single-model defaults.
    """
    # ── New path: app config injected ────────────────────────────────────
    if _active_cfg is not None and _active_config_dir is not None:
        try:
            from app.llm.config import resolve_for_tier  # local import keeps vendor dir decoupled
            provider, model, base_url, api_key = resolve_for_tier(name, _active_cfg, _active_config_dir)
            # App schema has no max_tokens; keep legacy per-tier mapping.
            max_tokens = _LEGACY_MAX_TOKENS.get(name, 8192)
            # Guard empty (no active provider configured) -> fall through to legacy.
            if api_key or model:
                return (api_key, base_url, model, max_tokens)
        except Exception:
            pass  # app config unavailable -> degrade to legacy

    # ── Legacy path: config.yaml + .env ──────────────────────────────────
    tiers = (_legacy_yaml or {}).get("llm", {}).get("tiers", {}) if _legacy_yaml else {}
    tier = tiers.get(name, {})
    model = tier.get("model", _DEFAULT_MODEL)
    base_url = tier.get("base_url", _DEFAULT_BASE_URL)
    max_tokens = tier.get("max_tokens", _LEGACY_MAX_TOKENS.get(name, 8192))
    api_key = _api_key_for(base_url)
    return (api_key, base_url, model, max_tokens)


def get_pipeline_config() -> dict:
    """Return pipeline toggles. Merges config.yaml over defaults.

    NOTE: pipeline toggles are NOT in the app llm.yaml schema; they remain
    in the legacy config.yaml. Unchanged from original.
    """
    if not _legacy_yaml:
        return dict(_DEFAULT_PIPELINE)
    cfg = (_legacy_yaml.get("pipeline") or {})
    merged = dict(_DEFAULT_PIPELINE)
    merged.update({k: v for k, v in cfg.items() if k in _DEFAULT_PIPELINE})
    return merged


def get_segment_config() -> dict:
    """Return segment thresholds. Merges config.yaml over defaults. Unchanged."""
    if not _legacy_yaml:
        return dict(_DEFAULT_SEGMENT)
    cfg = (_legacy_yaml.get("segment") or {})
    merged = dict(_DEFAULT_SEGMENT)
    merged.update({k: v for k, v in cfg.items() if k in _DEFAULT_SEGMENT})
    return merged


def has_config() -> bool:
    """True if EITHER app config was injected OR legacy config.yaml loaded."""
    return _active_cfg is not None or _legacy_yaml is not None
