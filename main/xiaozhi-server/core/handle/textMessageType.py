from enum import Enum


class TextMessageType(Enum):
    """消息类型枚举"""
    HELLO = "hello"
    ABORT = "abort"
    LISTEN = "listen"
    TEXT = "text"
    IOT = "iot"
    MCP = "mcp"
    SERVER = "server"
    PING = "ping"
