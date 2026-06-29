"""
VLA 视觉抓取函数插件
====================
Phase 3.4: 使 LLM 在 function_call 模式下能调用 VLA 视觉抓取流程。

执行流程：
  ① 调用 camera.take_photo → 获取 base64 图片
  ② HTTP POST VLA 推理服务 /vla/infer → 获取关节动作序列
  ③ 逐帧调用 robot.arm.move_joints + robot.arm.gripper → 执行动作

依赖：
  - VLA 推理服务运行中（services/vla_server.py，默认 http://localhost:8080）
  - ESP32 设备在线（提供 camera.take_photo / robot.arm.* MCP 工具）
"""
import json
import asyncio
import httpx
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from core.providers.tools.device_mcp.mcp_handler import call_mcp_tool
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

VLA_GRASP_DESC = {
    "type": "function",
    "function": {
        "name": "vla_grasp",
        "description": (
            "通过摄像头拍照，调用VLA视觉模型识别物体并控制机械臂完成抓取。"
            "当用户要求抓取、拿起、拾取某个物体时调用此函数。"
            "instruction参数应描述目标物体，例如'抓取红色方块'、'拿起左边的杯子'。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "抓取指令，描述要抓取什么物体，例如'抓取红色方块'",
                },
            },
            "required": ["instruction"],
        },
    },
}


@register_function("vla_grasp", VLA_GRASP_DESC, ToolType.SYSTEM_CTL)
async def vla_grasp(conn: "ConnectionHandler", instruction: str) -> ActionResponse:
    """根据自然语言指令，执行 VLA 视觉抓取。

    Args:
        conn: ConnectionHandler 实例（用于 MCP 调用和配置访问）
        instruction: 抓取指令（如"抓取红色方块"）

    Returns:
        ActionResponse: 包含抓取结果，LLM 据此生成用户回复
    """
    # 读取 VLA 配置
    vla_config = conn.config.get("VLA", {})
    vla_url = vla_config.get("server_url", "http://localhost:8080")
    vla_timeout = vla_config.get("timeout", 30)
    vla_enabled = vla_config.get("enabled", True)

    if not vla_enabled:
        return ActionResponse(
            Action.REQLLM,
            "VLA视觉抓取功能未启用，请先在配置中开启。",
            None,
        )

    # 检查 MCP 客户端是否就绪
    if not hasattr(conn, "mcp_client") or not conn.mcp_client:
        logger.bind(tag=TAG).warning("MCP客户端未初始化，设备可能离线")
        return ActionResponse(
            Action.REQLLM,
            "机械臂设备当前不在线，无法执行抓取操作。",
            None,
        )

    if not await conn.mcp_client.is_ready():
        return ActionResponse(
            Action.REQLLM,
            "机械臂设备正在初始化中，请稍后再试。",
            None,
        )

    # ========== 第1步：拍照 ==========
    logger.bind(tag=TAG).info(f"开始VLA抓取流程，指令: {instruction}")
    try:
        photo_result = await call_mcp_tool(
            conn, conn.mcp_client, "camera.take_photo", "{}", timeout=10
        )
        logger.bind(tag=TAG).debug(f"拍照结果: {str(photo_result)[:200]}...")
    except ValueError as e:
        logger.bind(tag=TAG).error(f"拍照失败（工具不可用）: {e}")
        return ActionResponse(
            Action.REQLLM,
            f"拍照功能不可用：{e}。请检查摄像头是否连接正常。",
            None,
        )
    except Exception as e:
        logger.bind(tag=TAG).error(f"拍照失败: {e}")
        return ActionResponse(
            Action.REQLLM,
            "拍照时出现异常，请稍后重试。",
            None,
        )

    # 解析拍照结果，提取 base64 图片
    image_b64 = _extract_image_base64(photo_result)
    if not image_b64:
        logger.bind(tag=TAG).error("未能从拍照结果中提取图片数据")
        return ActionResponse(
            Action.REQLLM,
            "拍照成功但未能获取图片数据，可能是摄像头返回格式异常。",
            None,
        )

    # ========== 第2步：调用 VLA 推理服务 ==========
    try:
        async with httpx.AsyncClient(timeout=vla_timeout) as client:
            resp = await client.post(
                f"{vla_url}/vla/infer",
                json={
                    "image": image_b64,
                    "instruction": instruction,
                    "current_joints": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                },
            )
            resp.raise_for_status()
            vla_result = resp.json()
        logger.bind(tag=TAG).info(
            f"VLA推理完成，置信度: {vla_result.get('confidence', 'N/A')}"
        )
    except httpx.ConnectError:
        logger.bind(tag=TAG).error(f"VLA服务不可达: {vla_url}")
        return ActionResponse(
            Action.REQLLM,
            f"视觉推理服务({vla_url})当前不可用，请确认VLA服务已启动。",
            None,
        )
    except httpx.TimeoutException:
        logger.bind(tag=TAG).error("VLA推理超时")
        return ActionResponse(
            Action.REQLLM,
            "视觉推理超时，VLA服务响应过慢，请稍后重试。",
            None,
        )
    except Exception as e:
        logger.bind(tag=TAG).error(f"VLA推理异常: {e}")
        return ActionResponse(
            Action.REQLLM,
            f"视觉推理过程出现异常：{e}",
            None,
        )

    if not vla_result.get("success"):
        error_msg = vla_result.get("error", "未知错误")
        logger.bind(tag=TAG).error(f"VLA推理返回失败: {error_msg}")
        return ActionResponse(
            Action.REQLLM,
            f"视觉推理失败：{error_msg}",
            None,
        )

    # ========== 第3步：执行动作序列 ==========
    action_joints = vla_result.get("action_joints", [])
    gripper_actions = vla_result.get("gripper", [])
    confidence = vla_result.get("confidence", 0.0)

    if not action_joints:
        logger.bind(tag=TAG).warning("VLA返回空的动作序列")
        return ActionResponse(
            Action.REQLLM,
            "VLA模型未能生成有效的抓取动作，可能是画面中没有识别到目标物体。",
            None,
        )

    # 逐帧下发关节指令
    executed_steps = 0
    for i, joints in enumerate(action_joints):
        try:
            joints_str = json.dumps(joints)
            await call_mcp_tool(
                conn,
                conn.mcp_client,
                "robot.arm.move_joints",
                json.dumps({"angles": joints_str, "speed": 40}),
                timeout=10,
            )
            executed_steps += 1
            await asyncio.sleep(0.3)  # 等机械臂完成每步动作
        except Exception as e:
            logger.bind(tag=TAG).warning(f"第{i+1}步关节指令执行失败: {e}")
            # 继续尝试后续步骤（best-effort）

    # 控制夹爪
    gripper_executed = False
    for i, grip in enumerate(gripper_actions):
        try:
            is_open = grip < 0.5
            await call_mcp_tool(
                conn,
                conn.mcp_client,
                "robot.arm.gripper",
                json.dumps({"open": is_open, "speed": 30}),
                timeout=10,
            )
            gripper_executed = True
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"夹爪动作{i+1}执行失败: {e}")

    # 构建结果描述
    result_desc = (
        f"VLA视觉抓取完成！指令：「{instruction}」，"
        f"置信度: {confidence:.0%}，"
        f"已执行 {executed_steps}/{len(action_joints)} 步关节动作"
    )
    if gripper_executed:
        result_desc += "，夹爪已操作"

    logger.bind(tag=TAG).info(result_desc)
    return ActionResponse(Action.REQLLM, result_desc, None)


def _extract_image_base64(photo_result) -> str | None:
    """从拍照结果中提取 base64 编码的图片数据。

    兼容多种返回格式：
      - 纯 base64 字符串
      - JSON 字符串: {"image": "...", "success": true}
      - dict: {"image": "..."} 或 {"data": "..."}
    """
    if isinstance(photo_result, str):
        # 尝试作为 JSON 解析
        try:
            data = json.loads(photo_result)
        except json.JSONDecodeError:
            # 纯 base64 字符串
            return photo_result if len(photo_result) > 100 else None
        if isinstance(data, dict):
            return data.get("image") or data.get("data") or data.get("base64")
        return str(data) if len(str(data)) > 100 else None

    if isinstance(photo_result, dict):
        return (
            photo_result.get("image")
            or photo_result.get("data")
            or photo_result.get("base64")
        )

    if isinstance(photo_result, bytes):
        import base64
        return base64.b64encode(photo_result).decode("utf-8")

    return str(photo_result) if len(str(photo_result)) > 100 else None
