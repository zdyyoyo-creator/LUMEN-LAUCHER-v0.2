"""账号模型：离线账号（Java 版离线 UUID 规则）+ 为微软正版账号预留的字段。

离线 UUID 与官方启动器 / 服务端一致：``md5("OfflinePlayer:" + name)``，
并把第 7 字节低 4 位置为 3（版本 3），第 9 字节高 2 位置为 10（RFC4122 variant）。
这样离线玩家在局域网联机和部分服务端校验里能被稳定识别。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Account:
    type: str = "offline"  # offline | msa
    username: str = "Steve"
    uuid: str = ""
    access_token: str = ""
    user_type: str = "legacy"  # legacy | msa
    client_id: str = ""
    xuid: str = ""
    refresh_token: str = ""
    properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.username = (self.username or "Steve").strip() or "Steve"
        if not self.uuid:
            self.uuid = offline_uuid(self.username)
        if not self.access_token:
            self.access_token = random_token()

    # ------------------------------------------------------------------ #
    @property
    def properties_json(self) -> str:
        return json.dumps(self.properties or {}, separators=(",", ":"))

    @property
    def is_offline(self) -> bool:
        return self.type == "offline"

    @property
    def label(self) -> str:
        return f"{self.username}（离线）" if self.is_offline else f"{self.username}（正版）"

    @property
    def uuid_dashed(self) -> str:
        return self.uuid

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "username": self.username,
            "uuid": self.uuid,
            "access_token": self.access_token,
            "user_type": self.user_type,
            "client_id": self.client_id,
            "xuid": self.xuid,
            "refresh_token": self.refresh_token,
            "properties": self.properties,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Account":
        known = {key: data.get(key, "") for key in ("type", "username", "uuid", "access_token")}
        return cls(
            type=str(known.get("type") or "offline"),
            username=str(known.get("username") or "Steve"),
            uuid=str(known.get("uuid") or ""),
            access_token=str(known.get("access_token") or ""),
            user_type=str(data.get("user_type") or "legacy"),
            client_id=str(data.get("client_id") or ""),
            xuid=str(data.get("xuid") or ""),
            refresh_token=str(data.get("refresh_token") or ""),
            properties=dict(data.get("properties") or {}),
        )

    def copy_with_name(self, username: str) -> "Account":
        return Account(
            type=self.type,
            username=username.strip() or self.username,
            uuid=offline_uuid(username.strip()) if self.is_offline else self.uuid,
            access_token=self.access_token,
            user_type=self.user_type,
            client_id=self.client_id,
            xuid=self.xuid,
            refresh_token=self.refresh_token,
            properties=dict(self.properties),
        )


def offline_uuid(username: str) -> str:
    """生成 Java 版离线 UUID（带连字符，version=3）。"""
    digest = hashlib.md5(f"OfflinePlayer:{username}".encode("utf-8")).digest()
    raw = bytearray(digest)
    raw[6] = (raw[6] & 0x0F) | 0x30
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def random_token() -> str:
    """离线模式下的假 accessToken（游戏客户端只校验长度与格式）。"""
    return secrets.token_hex(16)


def offline_account(username: str = "Steve") -> Account:
    name = (username or "Steve").strip() or "Steve"
    return Account(
        type="offline",
        username=name,
        uuid=offline_uuid(name),
        access_token=random_token(),
        user_type="legacy",
    )


def load_account(raw: dict[str, Any] | None) -> Account:
    if not raw:
        return offline_account()
    try:
        return Account.from_dict(raw)
    except Exception:  # noqa: BLE001 - 配置损坏时回落默认账号
        return offline_account()


def save_account(account: Account) -> dict[str, Any]:
    return account.to_dict()


def validate_username(username: str) -> str:
    """Java 版用户名限制：3–16 位，仅 ASCII 字母数字下划线。返回错误信息（空 = 通过）。"""
    name = (username or "").strip()
    if not name:
        return "用户名不能为空"
    if len(name) < 3:
        return "用户名至少 3 个字符"
    if len(name) > 16:
        return "用户名最多 16 个字符"
    if not all((ch.isascii() and ch.isalnum()) or ch == "_" for ch in name):
        return "用户名只能包含字母、数字和下划线（不支持中文/空格）"
    return ""
