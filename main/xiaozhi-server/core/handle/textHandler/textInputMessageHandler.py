import time
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

from core.handle.receiveAudioHandle import startToChat
from core.handle.reportHandle import enqueue_asr_report
from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType

TAG = __name__


class TextInputMessageHandler(TextMessageHandler):
    """文字输入消息处理器 — 直接以文字形式与 AI 对话，跳过语音识别"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.TEXT

    async def handle(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        """处理 type:text 消息，将文字直接路由到 LLM"""
        user_text = msg_json.get("text", "").strip()
        if not user_text:
            conn.logger.bind(tag=TAG).warning("收到空的文字消息")
            return

        conn.logger.bind(tag=TAG).info(f"📝 文字输入: {user_text}")
        conn.last_activity_time = time.time() * 1000

        # 上报文字内容（复用 ASR 上报，但不提供音频数据）
        enqueue_asr_report(conn, user_text, [])

        # 直接进入 LLM 对话流程（跳过 STT）
        await startToChat(conn, user_text)
