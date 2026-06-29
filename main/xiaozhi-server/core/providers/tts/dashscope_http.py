"""
阿里百炼 DashScope CosyVoice HTTP TTS
使用 HTTP API（而非 WebSocket），避免 EdgeTTS 的 TLS 延迟问题
与 qwen-turbo LLM 共用同一个 API Key
"""
import os
import time
import uuid
import httpx
from config.logger import setup_logging
from core.providers.tts.base import TTSProviderBase

TAG = __name__
logger = setup_logging()


class TTSProvider(TTSProviderBase):
    """DashScope CosyVoice HTTP TTS — 简单 HTTP，不走 WebSocket"""

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)

        self.api_key = config.get("api_key")
        if not self.api_key:
            raise ValueError("api_key is required")

        self.model = config.get("model", "cosyvoice-v2")
        self.voice = config.get("voice", "longcheng_v2")
        self.output_dir = config.get("output_dir", "tmp/")
        self.format = config.get("format", "mp3")

        self.submit_url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-to-speech/generation"

    def to_tts(self, text: str) -> list:
        """非流式生成音频（供 TTS 线程调用）"""
        if not text or not text.strip():
            return []

        try:
            # 1. 提交合成任务
            resp = httpx.post(
                self.submit_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "X-DashScope-Async": "enable",
                },
                json={
                    "model": self.model,
                    "input": {"text": text.strip()},
                    "parameters": {
                        "voice": self.voice,
                        "format": self.format,
                    },
                },
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()

            if "output" not in data or "task_id" not in data.get("output", {}):
                logger.bind(tag=TAG).error(f"TTS 提交失败: {data}")
                return []

            task_id = data["output"]["task_id"]

            # 2. 轮询等待结果
            for _ in range(60):  # 最多等 30 秒
                time.sleep(0.3)
                r2 = httpx.get(
                    f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=10.0,
                )
                r2.raise_for_status()
                d2 = r2.json()
                status = d2.get("output", {}).get("task_status", "")

                if status == "SUCCEEDED":
                    audio_url = d2["output"]["results"]["audio_url"]
                    # 3. 下载音频文件
                    audio_resp = httpx.get(audio_url, timeout=10.0)
                    audio_resp.raise_for_status()
                    audio_data = audio_resp.content

                    # 4. 保存到文件
                    os.makedirs(self.output_dir, exist_ok=True)
                    ext = self.format if self.format else "mp3"
                    filepath = os.path.join(self.output_dir, f"tts_{uuid.uuid4().hex[:8]}.{ext}")
                    with open(filepath, "wb") as f:
                        f.write(audio_data)

                    logger.bind(tag=TAG).info(
                        f"TTS 合成成功: {len(audio_data)} bytes, "
                        f"耗时约 {_ * 0.3:.1f}s"
                    )

                    # 5. 编码为 OPUS 返回
                    return self.audio_to_opus_data(filepath)

                elif status == "FAILED":
                    err = d2.get("output", {}).get("message", "unknown")
                    logger.bind(tag=TAG).error(f"TTS 任务失败: {err}")
                    return []

            logger.bind(tag=TAG).error("TTS 轮询超时")
            return []

        except Exception as e:
            logger.bind(tag=TAG).error(f"TTS 请求异常: {e}")
            return []
