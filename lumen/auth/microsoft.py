"""微软正版登录（设备码授权流程）—— 接口预留实现。

完整链路为：
    1. POST https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode
    2. 轮询 POST .../token 拿 Microsoft access_token
    3. 换 Xbox Live token（user.auth.xboxlive.com）
    4. XSTS 授权（xsts.auth.xboxlive.com）
    5. 换 Minecraft token（api.minecraftservices.com/authentication/login_with_xbox）
    6. 查档案（api.minecraftservices.com/minecraft/profile）

本模块实现了 1、2 两步的真实请求（设备码可以在 UI 上真正展示），
3–6 步留出明确的扩展点：接入方实现 ``complete_sign_in`` 即可，不影响离线账号使用。
``client_id`` 需要使用者自行在 Azure 门户注册「移动和桌面应用」，默认留空会抛出可读错误。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..utils.errors import NetworkError
from ..utils.log import get as get_logger
from ..utils.net import get_json, session

log = get_logger("auth.microsoft")

DEVICE_CODE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
SCOPE = "XboxLive.signin offline_access"


@dataclass
class DeviceCodeFlow:
    user_code: str
    device_code: str
    verification_uri: str
    expires_in: int
    interval: int
    message: str = ""

    @property
    def expires_text(self) -> str:
        minutes = max(1, self.expires_in // 60)
        return f"{minutes} 分钟内有效"


@dataclass
class MicrosoftAuth:
    """设备码授权客户端。未配置 client_id 时所有方法都会给出明确提示。"""

    client_id: str = ""
    timeout: float = 20.0

    def _require_client_id(self) -> str:
        client_id = (self.client_id or "").strip()
        if not client_id:
            raise NetworkError(
                "尚未配置微软应用 ID",
                hint=(
                    "请在 Azure 门户注册应用并填入 client_id；"
                    "该功能为预留接口，当前请使用离线账号进入游戏"
                ),
            )
        return client_id

    def request_device_code(self) -> DeviceCodeFlow:
        client_id = self._require_client_id()
        response = session().post(
            DEVICE_CODE_URL,
            data={"client_id": client_id, "scope": SCOPE},
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise NetworkError(f"设备码请求失败（HTTP {response.status_code}）：{response.text[:200]}")
        payload = response.json()
        flow = DeviceCodeFlow(
            user_code=str(payload.get("user_code", "")),
            device_code=str(payload.get("device_code", "")),
            verification_uri=str(payload.get("verification_uri", "https://www.microsoft.com/link")),
            expires_in=int(payload.get("expires_in", 900)),
            interval=int(payload.get("interval", 5)),
            message=str(payload.get("message", "")),
        )
        log.info("已获取设备码：%s（%s）", flow.user_code, flow.verification_uri)
        return flow

    def poll_token(self, flow: DeviceCodeFlow, *, should_cancel=None) -> dict:
        """轮询 token；返回原始 token 响应（含 access_token / refresh_token）。"""
        client_id = self._require_client_id()
        deadline = time.monotonic() + flow.expires_in
        interval = max(1, flow.interval)
        while time.monotonic() < deadline:
            if should_cancel is not None and should_cancel():
                raise NetworkError("已取消登录")
            response = session().post(
                TOKEN_URL,
                data={
                    "client_id": client_id,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": flow.device_code,
                },
                timeout=self.timeout,
            )
            payload = response.json()
            error = str(payload.get("error", ""))
            if response.status_code == 200 and payload.get("access_token"):
                log.info("微软账号授权成功")
                return payload
            if error in ("authorization_pending", "slow_down"):
                if error == "slow_down":
                    interval += 2
                time.sleep(interval)
                continue
            if error == "expired_token":
                raise NetworkError("设备码已过期，请重新获取")
            if error == "authorization_declined":
                raise NetworkError("用户拒绝了授权")
            raise NetworkError(f"授权失败：{payload.get('error_description') or error or response.status_code}")
        raise NetworkError("等待授权超时，请重试")

    def complete_sign_in(self, microsoft_tokens: dict):  # noqa: ANN201 - 预留接口
        """Xbox Live → XSTS → Minecraft 的换票过程（扩展点）。"""
        raise NotImplementedError(
            "微软账号的 Xbox Live / Minecraft 换票流程尚未接入，"
            "当前版本请使用离线账号；该接口保留给后续扩展。"
        )
