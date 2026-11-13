from typing import Any

# 值类型异构（str / int / bool），且各键被单独取用为**不同**的函数默认值
# （见 cua.py 的 CuaBooter.__init__）：不显式宽注解时 ty 会把每个键推成全部值类型的并集
# （`str | int` 之类），导致「默认值与形参注解不符」的误判。此处宽注解即表达本意。
CUA_DEFAULT_CONFIG: dict[str, Any] = {
    "image": "linux",
    "os_type": "linux",
    "ttl": 3600,
    "idle_timeout": 0,
    "telemetry_enabled": False,
    "local": True,
    "api_key": "",
}

CUA_CONFIG_KEYS = {
    "image": "cua_image",
    "os_type": "cua_os_type",
    "ttl": "cua_ttl",
    "telemetry_enabled": "cua_telemetry_enabled",
    "local": "cua_local",
    "api_key": "cua_api_key",
}
