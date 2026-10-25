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
        # ME_STATE_DIR 环境覆盖: 测试/多实例(实例锁按 state 目录隔离)
        override = os.environ.get("ME_STATE_DIR", "").strip()
        d = (Path(override) if override else
             self.root / (self.console.get("state_dir", "state")))
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

    # ---- 通道双模(2026-10-25 补强): reverse=被动监听 / forward=正向外连 ----

    def onebot_mode(self) -> str:
        m = os.environ.get("ME_ONEBOT_MODE", "").strip().lower()
        if m not in ("reverse", "forward"):
            m = str(self.channels.get("onebot", {}).get("mode", "reverse")).strip().lower()
        return m if m in ("reverse", "forward") else "reverse"

    def onebot_forward_url(self) -> str:
        u = os.environ.get("ME_ONEBOT_WS_URL", "").strip()
        return u or str(self.channels.get("onebot", {}).get("ws_url", "")).strip()

    def onebot_reconnect_interval(self) -> float:
        raw = os.environ.get("ME_ONEBOT_RECONNECT_S", "").strip()
        if not raw:
            raw = str(self.channels.get("onebot", {}).get("reconnect_interval_s", 5)).strip()
        try:
            return max(1.0, float(raw))
        except ValueError:
            return 5.0

    def internal_token(self) -> str:
        return self.env(self.console.get("internal_token_env", "ME_INTERNAL_TOKEN"))

    def llm_api_key(self) -> str:
        return self.env(self.llm_cfg.get("api_key_env", "DEEPSEEK_API_KEY"))

    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key()) and bool(self.llm_cfg.get("base_url"))
