"""
config_helper.py — 加载配置，敏感字段从 .env 读取

用法:
    from config_helper import load_config
    cfg = load_config()  # 返回 dict，敏感字段已覆盖
"""
import json
import os
from pathlib import Path
from dotenv import load_dotenv

_ENV_MAP = {
    "POLY_PRIVATE_KEY": "poly_private_key",
    "POLY_FUNDER":      "poly_funder",
    "POLY_CHAIN_ID":    "poly_chain_id",
    "POLY_SIGNATURE_TYPE": "poly_signature_type",
    "SMTP_USER":        "smtp_user",
    "SMTP_PASS":        "smtp_pass",
    "NOTIFY_EMAIL":     "notify_email",
    "VC_KEY":           "vc_key",
}

_PROJECT_ROOT = Path(__file__).resolve().parent
_ENV_FILE     = _PROJECT_ROOT / ".env"
_CFG_FILE     = _PROJECT_ROOT / "config.json"


def load_config(config_path: str | Path | None = None) -> dict:
    load_dotenv(_ENV_FILE, override=False)

    cfg_path = Path(config_path) if config_path else _CFG_FILE
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    for env_var, cfg_key in _ENV_MAP.items():
        val = os.environ.get(env_var)
        if val is not None:
            if cfg_key in ("poly_chain_id",):
                cfg[cfg_key] = int(val)
            elif cfg_key in ("poly_signature_type",):
                cfg[cfg_key] = int(val)
            else:
                cfg[cfg_key] = val

    return cfg
