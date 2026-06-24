"""
飞书 Bot 消息处理模块
桥接飞书消息 → OpenClaw LLM → 飞书回复（文字）
支持 URL 验证、消息接收、异步 LLM 调用、双路回复（飞书 + ESP32 TTS）
"""

import json
import time
import asyncio
import logging
from typing import Optional, Dict, Any

import aiohttp

TAG = __name__
logger = logging.getLogger(TAG)


class FeishuBot:
    """飞书 Bot 消息处理 — 桥接到小智 AI 的 LLM 流水线"""

    # 飞书 API 地址
    TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    REPLY_URL = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"

    def __init__(self, config: dict, llm, system_prompt: str = ""):
        """
        初始化飞书 Bot
        :param config: 服务器配置字典（需含 feishu 节）
        :param llm: 共享的 LLM 实例（来自 WebSocketServer._llm）
        :param system_prompt: 对话系统提示词
        """
        feishu_config = config.get("feishu", {})
        self.app_id = feishu_config.get("app_id", "")
        self.app_secret = feishu_config.get("app_secret", "")
        self.verify_token = feishu_config.get("verify_token", "")
        self.enabled = feishu_config.get("enabled", False)

        self.llm = llm
        self.system_prompt = system_prompt or config.get("prompt", "")

        # Token 缓存
        self._tenant_access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._session: Optional[aiohttp.ClientSession] = None

        if self.enabled and self.app_id and self.app_secret:
            logger.info(f"[飞书] 飞书 Bot 回调已就绪 (App ID: {self.app_id[:8]}...)")
        elif self.enabled:
            logger.warning("[飞书] 飞书已启用但 App ID/Secret 未配置，回调将不可用")

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建 aiohttp session"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # ========== Token 管理 ==========

    async def get_tenant_access_token(self) -> str:
        """
        获取 tenant_access_token（带缓存，有效期约 2 小时）
        提前 5 分钟刷新，避免边界过期
        """
        now = time.time()
        if self._tenant_access_token and now < self._token_expires_at - 300:
            return self._tenant_access_token

        session = await self._get_session()
        try:
            async with session.post(self.TOKEN_URL, json={
                "app_id": self.app_id,
                "app_secret": self.app_secret
            }) as resp:
                result = await resp.json()
                if result.get("code") != 0:
                    logger.error(f"[飞书] 获取 tenant_access_token 失败: {result}")
                    raise RuntimeError(f"飞书 token 获取失败: {result.get('msg', '未知错误')}")

                self._tenant_access_token = result["tenant_access_token"]
                # 有效期通常 7200 秒（2小时）
                expire_seconds = result.get("expire", 7200)
                self._token_expires_at = now + expire_seconds
                logger.info(f"[飞书] tenant_access_token 已刷新，有效期 {expire_seconds}s")
                return self._tenant_access_token
        except Exception as e:
            logger.error(f"[飞书] Token 请求异常: {e}")
            raise

    # ========== URL 验证（飞书首次配置回调地址） ==========

    def verify_url(self, challenge: str, token: str = "") -> dict:
        """
        URL 验证 — 飞书首次配置回调地址时调用
        必须返回 {"challenge": challenge_value}
        """
        if self.verify_token and token != self.verify_token:
            logger.warning(f"[飞书] URL 验证失败: token 不匹配")
            return {"code": 1, "msg": "invalid token"}
        logger.info(f"[飞书] URL 验证成功")
        return {"challenge": challenge}

    # ========== 事件处理入口 ==========

    async def handle_callback(self, body: dict) -> Optional[dict]:
        """
        处理飞书事件回调（入口方法）
        由 HTTP 服务器的 /feishu/callback 端点调用
        """
        if not self.enabled:
            logger.warning("[飞书] Bot 未启用，忽略回调")
            return {"code": 0, "msg": "feishu bot disabled"}

        # 处理 URL 验证
        if "challenge" in body:
            token = body.get("token", "")
            return self.verify_url(body["challenge"], token)

        # 处理消息事件
        event_type = body.get("header", {}).get("event_type", "")
        if event_type == "im.message.receive_v1":
            await self._handle_message_event(body.get("event", {}))
        else:
            logger.debug(f"[飞书] 忽略事件类型: {event_type}")

        return {"code": 0}

    # ========== 消息处理 ==========

    async def _handle_message_event(self, event: dict):
        """处理飞书 im.message.receive_v1 事件"""
        message = event.get("message", {})
        if not message:
            return

        msg_type = message.get("message_type", "")
        if msg_type != "text":
            logger.debug(f"[飞书] 忽略非文本消息: {msg_type}")
            return

        # 解析消息文本
        content_str = message.get("content", "{}")
        try:
            content = json.loads(content_str)
        except json.JSONDecodeError:
            logger.warning(f"[飞书] 无法解析消息内容: {content_str}")
            return

        user_text = content.get("text", "").strip()

        # 过滤 @bot / @_all 前缀
        if user_text.startswith("@_bot"):
            user_text = user_text[len("@_bot"):].strip()
        if user_text.startswith("@_all"):
            user_text = user_text[len("@_all"):].strip()

        if not user_text:
            return

        message_id = message.get("message_id", "")
        logger.info(f"[飞书] 收到消息: {user_text}")

        # 异步处理（先返回 200，避免飞书 3 秒超时重试）
        asyncio.create_task(self._process_message(user_text, message_id))

    async def _process_message(self, user_text: str, message_id: str):
        """
        处理用户文字：调用 LLM → 回复飞书
        在后台 asyncio.Task 中运行
        """
        try:
            # 1. 调用 LLM（非流式，获取完整回复）
            llm_response = await self._call_llm(user_text)

            if llm_response:
                # 2. 回复飞书（文字）
                await self._reply_feishu_text(message_id, llm_response)
                logger.info(f"[飞书] 已回复消息 {message_id}")
            else:
                logger.warning(f"[飞书] LLM 返回空响应")
                await self._reply_feishu_text(
                    message_id, "抱歉，我现在有点忙，请稍后再试～"
                )
        except Exception as e:
            logger.error(f"[飞书] 处理消息失败: {e}", exc_info=True)

    # ========== LLM 调用 ==========

    async def _call_llm(self, user_text: str) -> str:
        """
        调用共享的 LLM 实例获取回复
        使用 response_no_stream 获取完整文本
        """
        if self.llm is None:
            logger.error("[飞书] LLM 实例未设置，无法调用")
            return ""

        # 在 executor 中运行同步的 LLM 调用（asyncio 兼容）
        loop = asyncio.get_running_loop()

        def _sync_call():
            try:
                return self.llm.response_no_stream(
                    system_prompt=self.system_prompt,
                    user_prompt=user_text,
                )
            except Exception as e:
                logger.error(f"[飞书] LLM 调用失败: {e}", exc_info=True)
                return ""

        result = await loop.run_in_executor(None, _sync_call)
        return result.strip() if result else ""

    # ========== 飞书回复 ==========

    async def _reply_feishu_text(self, message_id: str, text: str):
        """
        通过飞书 API 回复文字消息（作为话题回复）
        """
        if not message_id:
            logger.warning("[飞书] message_id 为空，无法回复")
            return

        url = self.REPLY_URL.format(message_id=message_id)
        payload = {
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }

        try:
            token = await self.get_tenant_access_token()
        except Exception:
            logger.error("[飞书] 无法获取 token，跳过回复")
            return

        session = await self._get_session()
        try:
            async with session.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as resp:
                result = await resp.json()
                if result.get("code") != 0:
                    logger.error(f"[飞书] 发送回复失败: {result}")
        except Exception as e:
            logger.error(f"[飞书] 回复请求异常: {e}")

    # ========== 清理 ==========

    async def close(self):
        """关闭 aiohttp session"""
        if self._session and not self._session.closed:
            await self._session.close()
            logger.info("[飞书] Session 已关闭")
