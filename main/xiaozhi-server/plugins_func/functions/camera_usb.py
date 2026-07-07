"""
USB 摄像头抓图插件（自检 + 优雅降级）
====================================
提供 USB 摄像头拍照功能供 LLM 视觉理解。
未接入摄像头时自动降级，不影响系统正常运行。

依赖: opencv-python (pip install opencv-python)
用法: LLM 调用 camera_usb 函数 → 抓图 → base64 → 返回给 LLM 看图
"""
import json
import base64
import asyncio
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

# ====== 自检：探测 USB 摄像头 ======
_camera_available = False
_camera_info = "未检测到USB摄像头"
_cap = None

try:
    import cv2
    _HAS_CV2 = True
except ImportError:
    _HAS_CV2 = False
    _camera_info = "opencv-python 未安装，请执行: pip install opencv-python"

if _HAS_CV2:
    # 尝试打开摄像头（索引 0-3）
    for idx in range(4):
        try:
            test_cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
            if test_cap.isOpened():
                ret, frame = test_cap.read()
                if ret and frame is not None:
                    h, w = frame.shape[:2]
                    _camera_available = True
                    _camera_info = f"USB摄像头已就绪 (设备{idx}, {w}x{h})"
                    _cap = test_cap  # 保持打开，后续复用
                    logger.bind(tag=TAG).info(f"USB摄像头已就绪 (设备{idx}, {w}x{h})")
                    break
                test_cap.release()
        except Exception:
            pass

if not _camera_available:
    logger.bind(tag=TAG).warning(f"摄像头不可用: {_camera_info}，视觉功能将降级运行")


def _capture_frame() -> str | None:
    """抓取一帧，返回 base64 JPEG 字符串。失败返回 None。"""
    global _cap
    if not _camera_available or _cap is None:
        if _HAS_CV2:
            try:
                _cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
                if not _cap.isOpened():
                    _cap.release()
                    _cap = None
                    return None
            except Exception:
                return None
        else:
            return None

    try:
        ret, frame = _cap.read()
        if not ret or frame is None:
            return None
        _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        return base64.b64encode(jpeg.tobytes()).decode("utf-8")
    except Exception as e:
        logger.bind(tag=TAG).error(f"抓图失败: {e}")
        return None


# ====== 函数注册 ======

CAMERA_USB_DESC = {
    "type": "function",
    "function": {
        "name": "camera_usb",
        "description": (
            "使用USB摄像头拍摄当前桌面画面，返回base64编码的JPEG图片。\n"
            "当你需要「看」桌面上的物体、确认机械臂位置、识别目标时调用此函数。\n"
            "question参数描述你想了解什么，例如「桌面上有什么」「红色方块在哪」。\n"
            f"当前状态: {_camera_info}"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "拍照目的，如「看看桌上有什么」「找红色物体」",
                },
            },
            "required": ["instruction"],
        },
    },
}


@register_function("camera_usb", CAMERA_USB_DESC, ToolType.SYSTEM_CTL)
async def camera_usb(conn: "ConnectionHandler", instruction: str = "描述画面") -> ActionResponse:
    """USB 摄像头拍照。

    Args:
        conn: ConnectionHandler 实例
        instruction: 拍照目的

    Returns:
        ActionResponse: 包含图片描述或错误信息
    """
    logger.bind(tag=TAG).info(f"拍照请求: {instruction}")

    if not _camera_available:
        return ActionResponse(
            Action.REQLLM,
            f"摄像头不可用：{_camera_info}。请根据用户描述继续操作。",
            None,
        )

    loop = asyncio.get_event_loop()
    try:
        image_b64 = await loop.run_in_executor(None, _capture_frame)
    except Exception as e:
        logger.bind(tag=TAG).error(f"抓图异常: {e}")
        return ActionResponse(Action.REQLLM, "拍照异常，请稍后重试。", None)

    if not image_b64:
        return ActionResponse(Action.REQLLM, "摄像头抓图失败——可能被占用或已断开。", None)

    logger.bind(tag=TAG).info(f"拍照成功, base64大小: {len(image_b64)} 字符")
    # 返回图片 + 提问，让 LLM 看图回答
    return ActionResponse(
        Action.REQLLM,
        f"已拍摄桌面照片。用户问：{instruction}。请根据照片内容用一两句话回答。",
        image_b64,  # 传给 VLLM 做视觉理解
    )
