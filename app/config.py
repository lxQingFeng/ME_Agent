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
        # console 先行: persona/llm 的 state/settings 覆盖层合并(_merged→state_dir)
        # 依赖 self.console(第 13 轮修复既有启动阻断: 默认 state 目录形态下
        # Settings() 在 console 赋值前触达 state_dir → AttributeError 崩溃)。
        self.console = _yaml("console.yaml")
        # 第 11 轮: persona/llm 支持 state/settings/ 覆盖层(控制台设置 API 保存处,
        # git 不追踪 → 密钥不入库), 键级覆盖 config/ 默认值
        self.persona = self._merged("persona.yaml")
        self.routine = _yaml("routine.yaml")
        self.activities_cfg = _yaml("activities.yaml")
        self.locations_cfg = _yaml("locations.yaml")
        self.dynamics_cfg = _yaml("dynamics.yaml")
        self.channels = _yaml("channels.yaml")
        self.memory = _yaml("memory.yaml")
        self.shaping = _yaml("shaping.yaml")
        self.llm_cfg = self._merged("llm.yaml")
        self.tts_cfg = _yaml("tts.yaml")
        self.programs_cfg = _yaml("programs.yaml")

    def _merged(self, name: str) -> dict:
        """config/<name>(仓库默认) 与 state/settings/<name>(控制台保存值) 浅合并。"""
        base = dict(_yaml(name))
        overlay_path = self.state_dir / "settings" / name
        if overlay_path.exists():
            try:
                overlay = yaml.safe_load(overlay_path.read_text("utf-8")) or {}
                if isinstance(overlay, dict):
                    base.update(overlay)
            except Exception:
                pass
        return base

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
        # 第 11 轮: 控制台保存的明文 key(state/settings/llm.yaml 的 llm_cfg["api_key"])
        # 优先于环境变量(用户显式保存即意图覆盖); 空串视为未设置
        direct = str(self.llm_cfg.get("api_key", "") or "").strip()
        if direct:
            return direct
        return self.env(self.llm_cfg.get("api_key_env", "DEEPSEEK_API_KEY"))

    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key()) and bool(self.llm_cfg.get("base_url"))
