"""WeFlow API message-type mapping for canonical raw v1."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MessageType:
    canonical: str
    placeholder: str


BASE_TYPES: dict[int, MessageType] = {
    1: MessageType("文本消息", ""),
    3: MessageType("图片消息", "[图片]"),
    34: MessageType("语音消息", "[语音消息 - 转文字失败: ASR未启用]"),
    42: MessageType("其他消息", "[名片]"),
    43: MessageType("其他消息", "[视频]"),
    47: MessageType("动画表情", "[动画表情]"),
    48: MessageType("其他消息", "[位置]"),
    50: MessageType("其他消息", "[通话消息]"),
    10000: MessageType("其他消息", "[系统消息]"),
}

APP_TYPES: dict[int, MessageType] = {
    1: MessageType("其他消息", "[链接]"),
    3: MessageType("其他消息", "[音乐]"),
    4: MessageType("其他消息", "[视频分享]"),
    5: MessageType("其他消息", "[链接]"),
    6: MessageType("其他消息", "[文件]"),
    8: MessageType("其他消息", "[其他消息]"),
    19: MessageType("其他消息", "[合并转发]"),
    24: MessageType("其他消息", "[收藏]"),
    33: MessageType("其他消息", "[小程序]"),
    36: MessageType("其他消息", "[分享]"),
    47: MessageType("其他消息", "[其他消息]"),
    50: MessageType("其他消息", "[视频号]"),
    51: MessageType("其他消息", "[动态]"),
    53: MessageType("其他消息", "[接龙]"),
    57: MessageType("引用消息", "[引用消息]"),
    62: MessageType("其他消息", "[视频号]"),
    63: MessageType("其他消息", "[视频号]"),
    74: MessageType("其他消息", "[文件]"),
    87: MessageType("其他消息", "[群公告]"),
    2000: MessageType("其他消息", "[转账]"),
    2001: MessageType("其他消息", "[红包]"),
}

# Only these chatlab values have been verified against the same default-format
# messages on a real WeFlow 5.x dataset.  Default JSON remains the sole mapping
# input because it uniquely exposes isSend/serverId/quote; this table is an
# explicit drift detector, not a second parsing path.
CHATLAB_TYPE_ALIGNMENT: dict[int, tuple[int, str]] = {
    0: (1, "文本消息"),
    5: (47, "动画表情"),
    25: ((57 << 32) | 49, "引用消息"),
}


def unpack_local_type(local_type: int) -> tuple[int, int]:
    value = int(local_type)
    return value & 0xFFFFFFFF, value >> 32


def resolve_message_type(local_type: int) -> MessageType:
    base, app_type = unpack_local_type(local_type)
    if base == 49:
        return APP_TYPES.get(app_type, MessageType("其他消息", "[其他消息]"))
    return BASE_TYPES.get(base, MessageType("其他消息", "[其他消息]"))


__all__ = [
    "APP_TYPES",
    "BASE_TYPES",
    "CHATLAB_TYPE_ALIGNMENT",
    "MessageType",
    "resolve_message_type",
    "unpack_local_type",
]
