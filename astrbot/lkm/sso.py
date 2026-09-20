"""面板 SSO 换取端点：用社区后台签发的一次性票据建立面板会话。

背景：LKMBot 面板并入 LKM 社区站后台后，管理员已登录社区后台（``admin_session`` cookie）再打开
内嵌面板时不应二次登录。链路：

    Astro SSR 页（持 admin 会话）→ 社区 backend ``/api/v1/admin/bot/sso-ticket``
      → auth 域 RS256 签票（60s、一次性）
    → iframe 载入本端点 ``/api/v1/auth/sso?ticket=...&next=/welcome``
      → 本端点用**公钥**验签 → 建面板会话 → 302 到 ``<base>/#/welcome``（URL 不留票据）

为什么是独立端点而不是「直接信任社区 cookie」：两边会话密钥与受众（audience）不同，面板不认社区
cookie；票据是显式的、短期的、一次性的换取凭据，且面板侧**只持公钥**（无法伪造社区票据）。

安全面：
- 算法白名单**写死 RS256**（防 alg confusion：拿公钥当 HMAC 密钥的经典混淆）；
- 校验 ``iss`` + ``aud=lkm:bot`` + ``type=bot_sso`` + ``account_level=admin``，别的 token
  一律不收（``iss`` 校验由 PyJWT 的 ``issuer=`` 完成；缺失/不符都归入 ``InvalidTokenError``
  分支 → 302 回登录页，**不**改成 5xx）；
- ``jti`` 一次性消费（内存 TTL 表），重放即拒；
- 票据经 iframe URL query 传递 → 响应带 ``Cache-Control: no-store`` 与 ``Referrer-Policy:
  no-referrer``，且 302 后的 Location 不含票据（不进历史/日志/Referer）；
- ``next`` 走白名单（仅同前缀内的绝对路径），防开放重定向；
- **fail-safe**：未配公钥 / 票据无效 / 重放 → 302 到面板登录页。SSO 不可用只退化为「手动登录
  一次」，绝不退化为越权。

已知限制：``jti`` 表在进程内存里，故本端点假设面板**单副本**（compose 与 k8s 清单均如此）。
扩多副本前必须换成共享存储（面板 DB 表），否则重放窗口会在多副本间打开。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import jwt
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from astrbot.lkm.base_path import dashboard_base_path


def _log() -> Any:
    """面板日志器：必须经 ``LogManager.GetLogger``（它会注入 loguru 格式化所需的
    ``plugin_tag`` 等 extra 字段）——用标准库的模块级 logger 记日志会因缺字段在格式化阶段
    抛错，而异常发生在请求处理路径上，表现为 400（真机验收踩到过）。惰性取用，避免模块导入
    期的日志系统副作用。
    """
    from astrbot.core.log import LogManager

    return LogManager.GetLogger("astrbot.dashboard")


router = APIRouter(tags=["Auth"])

#: 内联 PEM（多行可用 ``\n`` 转义）。
SSO_PUBLIC_KEY_ENV = "LKM_BOT_SSO_PUBLIC_KEY"
#: PEM 文件路径（k8s Secret / compose 只读挂载首选，免去 env 转义问题）。
SSO_PUBLIC_KEY_FILE_ENV = "LKM_BOT_SSO_PUBLIC_KEY_FILE"


def _protocol_value(env_name: str, default: str) -> str:
    """协议值：同名环境变量可覆盖，未配置/空白 → 代码默认值（进程启动时定值）。

    默认值必须与签发侧 ``auth.bot_sso``（LKM-service）逐字相同，否则票据被对面拒收。
    跨仓无法共享 import，故真正的单一来源在部署层：``LKM_BOT_SSO_AUDIENCE`` /
    ``LKM_BOT_SSO_ISSUER`` 由 ``.env`` 只写一次、经 compose 注入两侧（见 ``.env.example``
    的「LKM Bot」段与 ``x-bot-sso-env`` 锚点）。
    """
    return os.environ.get(env_name, "").strip() or default


#: 与社区 auth 域 ``auth.bot_sso`` 同源（单一事实源在部署层，这里是消费侧默认值）。
BOT_SSO_AUDIENCE = _protocol_value("LKM_BOT_SSO_AUDIENCE", "lkm:bot")
BOT_SSO_TYPE = _protocol_value("LKM_BOT_SSO_TYPE", "bot_sso")
#: 签发方标识：**本次新增校验**（此前只验 aud/type，iss 签发时写入却从不校验，等于允许
#: 任何持有同一 audience 的签发方冒充社区 auth）。可经 LKM_BOT_SSO_ISSUER 覆盖（两侧同值）。
BOT_SSO_ISSUER = _protocol_value("LKM_BOT_SSO_ISSUER", "lkm-auth")
#: 与签发侧同值（见 LKM-service ``auth.bot_sso.BOT_SSO_ACCOUNT_LEVEL``）。
BOT_SSO_ACCOUNT_LEVEL = _protocol_value("LKM_BOT_SSO_ACCOUNT_LEVEL", "admin")

#: 已消费票据的保留时长：票据本身 60s 过期，多留一分钟以防「过期边界上的重放」。
_JTI_RETENTION_S = 120

#: 已消费的 jti → 可清理时刻。单副本进程内存（见模块 docstring 已知限制）。
_consumed_jti: dict[str, float] = {}


def _load_sso_public_key() -> str | None:
    """取验签公钥：内联 env 优先，其次文件。都没有/读不到 → ``None``（SSO 关闭，fail-safe）。

    与 auth 侧 ``jwt_keys._pem`` 的差别：那里读不到文件直接抛（部署配置错误不该静默降级成
    「无密钥」）；此处是**消费方**，宁可告警并回落面板自带登录页，也不能让面板起不来。
    """
    inline = os.environ.get(SSO_PUBLIC_KEY_ENV, "").strip()
    if inline:
        return inline.replace("\\n", "\n")
    path = os.environ.get(SSO_PUBLIC_KEY_FILE_ENV, "").strip()
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        _log().warning("Cannot read bot SSO public key file %s: %s", path, exc)
        return None
    return text or None


def _consume_jti(jti: str, expires_at: float) -> bool:
    """一次性消费 ``jti``：首次返回 True，重复返回 False。

    同步函数内无 ``await``，在单事件循环下即是原子操作；顺带清理过期条目避免无界增长。
    """
    now = time.time()
    for stale in [key for key, deadline in _consumed_jti.items() if deadline < now]:
        _consumed_jti.pop(stale, None)
    if jti in _consumed_jti:
        return False
    _consumed_jti[jti] = max(expires_at, now) + _JTI_RETENTION_S
    return True


def _safe_next(raw: str) -> str:
    """``next`` 白名单：只接受面板前缀内的绝对路径（拒 ``//host``、``#``、反斜杠）。"""
    value = (raw or "").strip()
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "#" in value
        or "\\" in value
    ):
        return "/welcome"
    return value


def _redirect(location: str) -> RedirectResponse:
    """统一 302：面板内跳转，带 no-store / no-referrer（票据与来源不外泄）。"""
    response = RedirectResponse(location, status_code=302)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _login_redirect() -> RedirectResponse:
    """任何失败的统一出口：回面板自带登录页（SSO 不可用时的降级路径）。"""
    return _redirect(f"{dashboard_base_path()}/#/auth/login")


@router.get("/auth/sso", include_in_schema=False)
async def sso_login(request: Request) -> RedirectResponse:
    """用社区票据换面板会话：验签 → 一次性消费 → 签发面板 JWT → 302 进面板。"""
    public_key = _load_sso_public_key()
    if public_key is None:
        _log().warning(
            "SSO ticket presented but no public key configured (%s/%s); "
            "falling back to the login page",
            SSO_PUBLIC_KEY_ENV,
            SSO_PUBLIC_KEY_FILE_ENV,
        )
        return _login_redirect()

    ticket = request.query_params.get("ticket", "").strip()
    if not ticket:
        return _login_redirect()

    try:
        payload = jwt.decode(
            ticket,
            public_key,
            algorithms=["RS256"],
            audience=BOT_SSO_AUDIENCE,
            # iss 校验（本次补齐）：无 issuer= 时 pyjwt 完全不看 iss，任何签得出同 aud 的
            # 签发方都能换到面板会话。缺失/不符都抛 InvalidTokenError 的子类，落入下面的
            # fail-safe 分支（302 登录页），不会变成 5xx。
            issuer=BOT_SSO_ISSUER,
        )
    except jwt.InvalidTokenError as exc:
        _log().warning("Rejected bot SSO ticket: %s", exc)
        return _login_redirect()

    if (
        payload.get("type") != BOT_SSO_TYPE
        or payload.get("account_level") != BOT_SSO_ACCOUNT_LEVEL
    ):
        _log().warning("Rejected bot SSO ticket: wrong type/account_level")
        return _login_redirect()

    jti = payload.get("jti")
    if not isinstance(jti, str) or not _consume_jti(jti, float(payload.get("exp", 0))):
        _log().warning("Rejected bot SSO ticket: missing or already consumed jti")
        return _login_redirect()

    # 惰性 import：依赖方向是 dashboard → lkm（``astrbot.dashboard.api.router`` 在模块级
    # import 本模块），顶层反向 import dashboard 会构成循环。
    from astrbot.dashboard.api.auth import _set_dashboard_jwt_cookie, get_auth_service

    service = get_auth_service(request)
    username = service.config["dashboard"]["username"]
    token = service.generate_jwt(username, auth_source="sso")

    response = _redirect(
        f"{dashboard_base_path()}/#{_safe_next(request.query_params.get('next', ''))}"
    )
    _set_dashboard_jwt_cookie(request, response, token)
    _log().info("Bot dashboard session established via community SSO for %s", username)
    return response
