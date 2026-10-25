"""配置装载:config/*.yaml + .env 环境变量。仓库根 = app/ 的上级目录。"""
from __future__ import annotations
import os
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

def _load_env() -> None:
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v

_load_env()

def _yaml(name: str) -> dict:
    p = CONFIG_DIR / name
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text("utf-8"))
    return data or {}

class Settings:
    def __init__(self, root: Path | None = None, state_dir: Path | None = None):
        self.root = root or ROOT
        self._cfg_dir = self.root / "config"
        self._state_dir_override = state_dir
        self.reload()

    def reload(self) -> None:
        self.persona = _yaml("persona.yaml")
        self.routine = _yaml("routine.yaml")
        self.activities_cfg = _yaml("activities.yaml")
        self.locations_cfg = _yaml("locations.yaml")
        self.dynamics_cfg = _yaml("dynamics.yaml")
        self.channels = _yaml("channels.yaml")
        self.memory = _yaml("memory.yaml")
        self.shaping = _yaml("shaping.yaml")
        self.llm_cfg = _yaml("llm.yaml")
        self.tts_cfg = _yaml("tts.yaml")
        self.programs_cfg = _yaml("programs.yaml")
        self.console = _yaml("console.yaml")

    def env(self, name: str, default: str = "") -> str:
        return os.environ.get(name, default)

    @property
    def state_dir(self) -> Path:
        if self._state_dir_override is not None:
            self._state_dir_override.mkdir(parents=True, exist_ok=True)
            return self._state_dir_override
        d = self.root / (self.console.get("state_dir", "state"))
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def vault_dir(self) -> Path:
        return self.root / (self.console.get("vault_dir", "vault"))

    @property
    def tz(self) -> str:
        return self.routine.get("timezone", "Asia/Shanghai")

    def onebot_token(self) -> str:
        return self.env(self.channels.get("onebot", {}).get("access_token_env", "ONEBOT_ACCESS_TOKEN"))

    def internal_token(self) -> str:
        return self.env(self.console.get("internal_token_env", "ME_INTERNAL_TOKEN"))

    def llm_api_key(self) -> str:
        return self.env(self.llm_cfg.get("api_key_env", "DEEPSEEK_API_KEY"))

    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key()) and bool(self.llm_cfg.get("base_url"))
