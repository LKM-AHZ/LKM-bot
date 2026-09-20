"""面板 SSO 换取端点（社区后台免登）与 base path 行为验收。

被测链路：社区后台签发的 RS256 一次性票据 → ``/api/v1/auth/sso`` 验签 + 消费 jti → 写面板
cookie（Path 收窄到面板前缀）→ 302 进面板；随后 ``/api/v1/auth/session`` 把 cookie 水合成
token（面板 WS/SSE 需要）。

覆盖重点：算法白名单（防 alg confusion）、aud/type/account_level 校验、jti 一次性（重放即拒）、
**fail-safe**（未配公钥一律回落登录页，绝不退化为越权）、``next`` 白名单、base path 对 cookie
Path 与 302 Location 的作用。
"""

from __future__ import annotations

import datetime
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from astrbot.dashboard.api import auth_sso
from astrbot.dashboard.api.router import build_api_router
from astrbot.dashboard.base_path import (
    dashboard_base_path,
    dashboard_cookie_path,
    with_base,
)
from astrbot.dashboard.responses import ApiError, error
from astrbot.dashboard.services.auth_service import (
    DASHBOARD_JWT_COOKIE_NAME,
    AuthService,
)

JWT_SECRET = "bot-sso-test-secret-with-32-bytes!!"
ADMIN_USERNAME = "lkm-admin"


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[str, str]:
    """一次性生成 RS256 密钥对（私钥签票、公钥验签，与批 5 的生产形态一致）。"""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def _ticket(private_pem: str, **overrides) -> str:
    """签一张与社区 auth 域同形的票据（默认全部合法，overrides 用于构造反例）。"""
    now = datetime.datetime.now(datetime.UTC)
    payload = {
        "iss": "lkm-auth",
        "aud": "lkm:bot",
        "sub": str(uuid.uuid4()),
        "account_level": "admin",
        "type": "bot_sso",
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + datetime.timedelta(seconds=60)).timestamp()),
    }
    payload.update(overrides)
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, private_pem, algorithm="RS256")


@pytest.fixture(autouse=True)
def _reset_sso_state(monkeypatch: pytest.MonkeyPatch):
    """每个用例隔离：清 jti 一次性表、清 SSO/base-path 相关环境变量。"""
    auth_sso._consumed_jti.clear()
    for name in (
        auth_sso.SSO_PUBLIC_KEY_ENV,
        auth_sso.SSO_PUBLIC_KEY_FILE_ENV,
        "ASTRBOT_DASHBOARD_BASE_PATH",
        "DASHBOARD_BASE_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    auth_sso._consumed_jti.clear()


@pytest_asyncio.fixture
async def client() -> httpx.AsyncClient:
    """最小 dashboard app：只装 API router + SSO 端点所需的 state（services.auth、jwt_secret）。"""
    app = FastAPI()

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        """与 ``create_dashboard_asgi_app`` 同形：把面板 API 错误映射成 JSON 信封。"""
        return JSONResponse(error(exc.message, exc.data), status_code=exc.status_code)

    app.state.jwt_secret = JWT_SECRET
    app.state.services = SimpleNamespace(
        auth=AuthService(
            SimpleNamespace(),
            {"dashboard": {"username": ADMIN_USERNAME, "jwt_secret": JWT_SECRET}},
        )
    )
    app.include_router(build_api_router())
    transport = httpx.ASGITransport(app=app)
    # https 而非 http：面板 cookie 默认带 Secure（非 debug/testing 运行时），httpx 不会在
    # 明文 http 上回送 Secure cookie，用 https 才能覆盖「SSO 写 cookie → 水合读取」的真实链路。
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver"
    ) as ac:
        yield ac


# ─────────────────────── base path 工具 ───────────────────────


def test_base_path_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    """前缀规范化：空 / ``/`` / 带尾斜杠 / 缺前导斜杠 都收敛成 ``""`` 或 ``"/bot"``。"""
    assert dashboard_base_path() == ""
    for raw in ("", "/", "  ", "/bot", "bot", "/bot/", "//bot//"):
        monkeypatch.setenv("ASTRBOT_DASHBOARD_BASE_PATH", raw)
        expected = "" if raw.strip() in ("", "/") else "/bot"
        assert dashboard_base_path() == expected, raw


def test_with_base_and_cookie_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """根路径部署保持上游语义；子路径部署下 cookie 作用域收窄到面板前缀。"""
    assert with_base("/") == "/"
    assert dashboard_cookie_path() == "/"
    assert dashboard_cookie_path("/api/auth") == "/api/auth"

    monkeypatch.setenv("ASTRBOT_DASHBOARD_BASE_PATH", "/bot")
    assert with_base("/") == "/bot/"
    assert with_base("/api/v1/auth/sso") == "/bot/api/v1/auth/sso"
    assert dashboard_cookie_path() == "/bot/"
    assert dashboard_cookie_path("/api/auth") == "/bot/api/auth"


# ─────────────────────── SSO 端点 ───────────────────────


@pytest.mark.asyncio
async def test_sso_without_public_key_falls_back_to_login(
    client: httpx.AsyncClient, rsa_keys: tuple[str, str]
) -> None:
    """未配公钥 → 一律 302 到登录页（SSO 关闭的 fail-safe，绝不越权放行）。"""
    private_pem, _ = rsa_keys
    resp = await client.get(
        "/api/v1/auth/sso",
        params={"ticket": _ticket(private_pem)},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/#/auth/login"
    assert DASHBOARD_JWT_COOKIE_NAME not in resp.cookies


@pytest.mark.asyncio
async def test_sso_rejects_missing_and_invalid_ticket(
    client: httpx.AsyncClient,
    rsa_keys: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺 ticket / 伪造字符串 → 302 登录页。"""
    _, public_pem = rsa_keys
    monkeypatch.setenv(auth_sso.SSO_PUBLIC_KEY_ENV, public_pem)
    for params in ({}, {"ticket": "not-a-jwt"}):
        resp = await client.get(
            "/api/v1/auth/sso", params=params, follow_redirects=False
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/#/auth/login"


@pytest.mark.asyncio
async def test_sso_rejects_wrong_audience_type_and_level(
    client: httpx.AsyncClient,
    rsa_keys: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """aud/type/account_level 任一不符即拒——别的 token 不能换面板会话。"""
    private_pem, public_pem = rsa_keys
    monkeypatch.setenv(auth_sso.SSO_PUBLIC_KEY_ENV, public_pem)
    for overrides in (
        {"aud": "lkm:admin"},
        {"type": "admin"},
        {"account_level": "normal"},
    ):
        resp = await client.get(
            "/api/v1/auth/sso",
            params={"ticket": _ticket(private_pem, **overrides)},
            follow_redirects=False,
        )
        assert resp.status_code == 302, overrides
        assert resp.headers["location"] == "/#/auth/login", overrides


@pytest.mark.asyncio
async def test_sso_rejects_hs256_forgery(
    client: httpx.AsyncClient,
    rsa_keys: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """算法白名单：非 RS256 的票一律拒（对应 alg confusion 防线）。

    注：PyJWT 自身已拒绝把 PEM 当 HMAC 密钥（``InvalidKeyError``），故经典「公钥当 HMAC 密钥」
    无法在此构造；本用例改用任意 HMAC 密钥签 HS256，验证面板只认 RS256 白名单——即便签名
    「自洽」也不放行。
    """
    _, public_pem = rsa_keys
    monkeypatch.setenv(auth_sso.SSO_PUBLIC_KEY_ENV, public_pem)
    forged = jwt.encode(
        {
            "aud": "lkm:bot",
            "type": "bot_sso",
            "account_level": "admin",
            "jti": uuid.uuid4().hex,
        },
        "attacker-hmac-secret-with-enough-length",
        algorithm="HS256",
    )
    resp = await client.get(
        "/api/v1/auth/sso", params={"ticket": forged}, follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/#/auth/login"


@pytest.mark.asyncio
async def test_sso_rejects_expired_ticket(
    client: httpx.AsyncClient,
    rsa_keys: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_pem, public_pem = rsa_keys
    monkeypatch.setenv(auth_sso.SSO_PUBLIC_KEY_ENV, public_pem)
    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)
    ticket = _ticket(
        private_pem,
        iat=int(past.timestamp()),
        exp=int((past + datetime.timedelta(seconds=60)).timestamp()),
    )
    resp = await client.get(
        "/api/v1/auth/sso", params={"ticket": ticket}, follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/#/auth/login"


@pytest.mark.asyncio
async def test_sso_establishes_session_and_is_single_use(
    client: httpx.AsyncClient,
    rsa_keys: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有效票 → 进面板：302 到 ``<base>/#/next``、URL 不带票、cookie Path 收窄、之后可水合。"""
    private_pem, public_pem = rsa_keys
    monkeypatch.setenv(auth_sso.SSO_PUBLIC_KEY_ENV, public_pem)
    monkeypatch.setenv("ASTRBOT_DASHBOARD_BASE_PATH", "/bot")
    ticket = _ticket(private_pem)

    resp = await client.get(
        "/api/v1/auth/sso",
        params={"ticket": ticket, "next": "/platforms"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/bot/#/platforms"
    assert ticket not in resp.headers["location"]
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["referrer-policy"] == "no-referrer"

    set_cookie = resp.headers["set-cookie"]
    assert DASHBOARD_JWT_COOKIE_NAME in set_cookie
    assert "Path=/bot/;" in set_cookie or set_cookie.endswith("Path=/bot/")

    # 重放同一张票（同 jti）→ 拒绝（一次性消费）
    replay = await client.get(
        "/api/v1/auth/sso", params={"ticket": ticket}, follow_redirects=False
    )
    assert replay.status_code == 302
    assert replay.headers["location"] == "/bot/#/auth/login"


@pytest.mark.asyncio
async def test_sso_next_is_whitelisted(
    client: httpx.AsyncClient,
    rsa_keys: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``next`` 只认面板内绝对路径：协议相对 URL / 带 # 的注入回落到 /welcome。"""
    private_pem, public_pem = rsa_keys
    monkeypatch.setenv(auth_sso.SSO_PUBLIC_KEY_ENV, public_pem)
    for evil in ("//evil.example.com", "https://evil.example.com", "/a#b", "/a\\b"):
        resp = await client.get(
            "/api/v1/auth/sso",
            params={"ticket": _ticket(private_pem), "next": evil},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/#/welcome", evil


@pytest.mark.asyncio
async def test_sso_public_key_from_file(
    client: httpx.AsyncClient,
    rsa_keys: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """公钥可经文件提供（k8s Secret / compose 只读挂载形态）。"""
    private_pem, public_pem = rsa_keys
    key_file = tmp_path / "jwt_public.pem"
    key_file.write_text(public_pem, encoding="utf-8")
    monkeypatch.setenv(auth_sso.SSO_PUBLIC_KEY_FILE_ENV, str(key_file))

    resp = await client.get(
        "/api/v1/auth/sso",
        params={"ticket": _ticket(private_pem)},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/#/welcome"


@pytest.mark.asyncio
async def test_sso_missing_key_file_falls_back(
    client: httpx.AsyncClient,
    rsa_keys: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """公钥文件读不到 → 告警并回落登录页（不让面板起不来）。"""
    private_pem, _ = rsa_keys
    monkeypatch.setenv(auth_sso.SSO_PUBLIC_KEY_FILE_ENV, str(tmp_path / "missing.pem"))
    resp = await client.get(
        "/api/v1/auth/sso",
        params={"ticket": _ticket(private_pem)},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/#/auth/login"


# ─────────────────────── 会话水合端点 ───────────────────────


@pytest.mark.asyncio
async def test_session_requires_credentials(client: httpx.AsyncClient) -> None:
    """无水合凭据 → 401（不泄露任何信息）。"""
    resp = await client.get("/api/v1/auth/session")
    assert resp.status_code == 401


def test_dashboard_modules_use_log_manager() -> None:
    """面板日志必须走 ``LogManager.GetLogger``（注入 loguru 格式化所需的 extra 字段）。

    用标准库的模块级 logger 记日志会在格式化阶段抛
    ``Formatting field not found in record: 'plugin_tag'``，而该异常发生在请求处理路径上，
    表现为 400（真机验收踩到过，单测因不经 loguru 格式化而漏掉）。
    """
    dashboard_dir = Path(__file__).resolve().parents[1] / "astrbot" / "dashboard"
    offenders = [
        f"{path.relative_to(dashboard_dir)}:{lineno}"
        for path in dashboard_dir.rglob("*.py")
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "logging.getLogger" in line and not line.lstrip().startswith("#")
    ]
    assert offenders == []


@pytest.mark.asyncio
async def test_session_exchanges_cookie_for_token(
    client: httpx.AsyncClient,
    rsa_keys: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2E：SSO 写下的 httpOnly cookie → 水合出 token（面板 WS/SSE 依赖它）。"""
    private_pem, public_pem = rsa_keys
    monkeypatch.setenv(auth_sso.SSO_PUBLIC_KEY_ENV, public_pem)

    sso = await client.get(
        "/api/v1/auth/sso",
        params={"ticket": _ticket(private_pem)},
        follow_redirects=False,
    )
    assert sso.status_code == 302
    client.cookies.update(sso.cookies)

    resp = await client.get("/api/v1/auth/session")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["username"] == ADMIN_USERNAME
    assert body["data"]["token"]
    assert body["data"]["password_upgrade_required"] is False
    assert resp.headers["cache-control"] == "no-store"

    # 水合出的 token 与 login 同源，可被面板 JWT 校验接受
    payload = jwt.decode(body["data"]["token"], JWT_SECRET, algorithms=["HS256"])
    assert payload["username"] == ADMIN_USERNAME
    assert payload["auth_source"] == "session"
