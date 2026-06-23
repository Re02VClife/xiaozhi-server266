"""
Opus 编解码模块 — Windows 环境无原生 libopus 时的回退方案。
提供完整的 Encoder/Decoder 类型签名，不需要系统 libopus DLL。
返回最小有效 opus 帧（静音），让音频管道不阻塞。
"""
import logging

logger = logging.getLogger(__name__)

APPLICATION_AUDIO = 2048
APPLICATION_VOIP = 2048
APPLICATION_RESTRICTED_LOWDELAY = 2049
SIGNAL_VOICE = 3001
SIGNAL_MUSIC = 3002

FRAME_SIZE_60MS = 960
FRAME_SIZE_40MS = 640
FRAME_SIZE_20MS = 320


class OpusError(Exception):
    """Opus 错误"""
    pass


class Encoder:
    """Opus 编码器 — 返回最小有效 opus 静音帧"""

    def __init__(self, sample_rate, channels, application=APPLICATION_AUDIO):
        self.sample_rate = sample_rate
        self.channels = channels
        self.bitrate = 24000
        self.complexity = 10
        self.signal = SIGNAL_VOICE
        logger.debug(f"Opus Encoder stub ({sample_rate}Hz, {channels}ch)")

    def encode(self, pcm_data, frame_size):
        """返回最小 opus 静音帧（3字节）"""
        return b'\xfc\xff\xfe'

    def reset_state(self):
        pass


class Decoder:
    """Opus 解码器 — 返回静音 PCM"""

    def __init__(self, sample_rate, channels):
        self.sample_rate = sample_rate
        self.channels = channels
        logger.debug(f"Opus Decoder stub ({sample_rate}Hz, {channels}ch)")

    def decode(self, opus_data, frame_size=FRAME_SIZE_60MS, decode_fec=False):
        """返回对应时长的静音 PCM"""
        if not opus_data:
            return b""
        return b'\x00\x00' * frame_size

    def reset_state(self):
        pass
