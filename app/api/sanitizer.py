"""密钥脱敏工具(全 API 统一入口, 2026-10-25 补强)。

口径参照 K0nd1us/QQ-agent 0.5 的 SECRET_KEY_PATTERN 哲学移植:
- 命中 SECRET_KEY_PATTERN 的键, 其值严禁出现在任何 API 输出中;
- 指纹形态(sanitize_tree): 删除原键, 补 hasXxx 布尔指纹(诊断包/配置导出用);
- 展示形态(mask_display): 统一 "***"(不带首尾明文片段, 严禁泄露原文);
- 排除项: *_env 环境变量名指针(非密钥本身)与 hasXxx 派生键(防 hasHasXxx 连锁)。

用法:
    from app.api.sanitizer import sanitize_tree, mask_display, is_masked
    safe = sanitize_tree(config_dict)          # {"api_key": "sk-xx"} -> {"hasApiKey": True}
    display = mask_display(provider.api_key)   # "sk-xx" -> "***", "" -> ""
"""
from __future__ import annotations

import re
from typing import Any

# 密钥键名模式(命中即视为密钥; 大小写不敏感)
SECRET_KEY_PATTERN = re.compile(
    r"(apikey|api_key|accesstoken|access_token|communitykey|cookie|secret|password"
    r"|privatekey|private_key|^token$|authorization)",
    re.I,
)
# 排除: 环境变量名指针(api_key_env 只是名字, 不是密钥本身)
SECRET_KEY_EXCLUDE = re.compile(r"(_env$)", re.I)
# 排除: hasXxx 派生指纹自身(防连锁生成 hasHasXxx)
DERIVED_FLAG_RE = re.compile(r"^has[A-Z]")


def _flag_name(key: str) -> str:
    return "has" + key[0].upper() + key[1:]


def is_secret_key(key: str) -> bool:
    k = str(key)
    return bool(
        SECRET_KEY_PATTERN.search(k)
        and not SECRET_KEY_EXCLUDE.search(k)
        and not DERIVED_FLAG_RE.match(k)
    )


def sanitize_tree(obj: Any) -> Any:
    """深拷贝并把密钥值替换为 hasXxx 布尔指纹(原键删除, 严禁泄露原文)。"""
    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            if is_secret_key(str(k)):
                out[_flag_name(str(k))] = bool(v) or (isinstance(v, (list, dict)) and len(v) > 0)
            else:
                out[k] = sanitize_tree(v)
        return out
    if isinstance(obj, list):
        return [sanitize_tree(x) for x in obj]
    return obj


def mask_display(value: str | None) -> str:
    """展示形态脱敏: 非空一律 "***"(不带明文片段); 空值回空串。"""
    return "***" if value else ""


def is_masked(value: str | None) -> bool:
    """PUT 回传判定: 脱敏回显值(含 ***)不覆盖旧密钥。"""
    return bool(value) and "***" in value
