"""
机械臂自然语言控制插件
========================
使 LLM 通过自然语言指令控制机械臂，无需理解关节角度。

插件内部负责：
  - 关键词解析（"抬起"/"放下"/"左转"等）
  - 关节角度映射
  - 调用 robot.arm.move_joints / robot.arm.gripper MCP 工具

这样即使轻量级 LLM（如 qwen-turbo）也能可靠地控制机械臂。
"""
import json
import re
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from core.providers.tools.device_mcp.mcp_handler import call_mcp_tool
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

MOVE_ARM_DESC = {
    "type": "function",
    "function": {
        "name": "move_arm",
        "description": (
            "用自然语言控制机械臂移动。当用户要求机械臂做任何动作时调用此函数。"
            "包括：抬起/放下手臂、左转/右转、伸出去/收回来、张开/闭合夹爪、回到初始位置等。"
            "instruction参数直接传用户的原话即可，例如'抬到90度'、'向左转'、'张开夹爪'。"
            "注意：用户说任何关于机械臂移动的话都必须调用此函数，不要只回复文字！"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "用户的自然语言指令，直接传原话，例如'抬到90度'、'放下'、'向左转45度'",
                },
            },
            "required": ["instruction"],
        },
    },
}


def _parse_angle(text: str, default: int = 45) -> int:
    """从文本中提取角度数值。"""
    match = re.search(r'(\d+)\s*度', text)
    if match:
        angle = int(match.group(1))
        return max(0, min(180, angle))
    return default


def _parse_instruction(instruction: str) -> dict | None:
    """解析自然语言指令，返回 MCP 工具调用参数。

    Returns:
        dict: {"tool": "robot.arm.move_joints", "args": {...}}
        或  {"tool": "robot.arm.gripper", "args": {...}}
        或  None: 无法解析
    """
    text = instruction.strip()

    # ========== 夹爪控制 ==========
    if any(kw in text for kw in ["张开", "打开", "松开", "释放"]):
        return {"tool": "robot.arm.gripper", "args": {"open": True, "speed": 50}}

    if any(kw in text for kw in ["闭合", "夹紧", "抓住", "夹住", "合上"]):
        return {"tool": "robot.arm.gripper", "args": {"open": False, "speed": 50}}

    # ========== 关节移动 ==========
    # 默认角度：保持当前基础位姿 [90, 90, 90, 90, 90, 90]
    angles = [90, 90, 90, 90, 90, 90]
    speed = 40

    # 提取速度
    speed_match = re.search(r'速度\s*(\d+)', text)
    if speed_match:
        speed = max(1, min(100, int(speed_match.group(1))))

    # 关节2（大臂/肩部）：抬起/放下
    if any(kw in text for kw in ["抬起", "抬高", "举起来", "往上", "上升", "升高"]):
        angle = _parse_angle(text, 90)
        angles[1] = angle  # 关节2 索引1
        logger.bind(tag=TAG).info(f"解析：抬起 → 关节2={angle}°")

    elif any(kw in text for kw in ["放下", "降下", "降低", "往下", "下降", "落下来"]):
        angle = _parse_angle(text, 30)
        angles[1] = angle
        logger.bind(tag=TAG).info(f"解析：放下 → 关节2={angle}°")

    # 关节1（底座）：左转/右转
    if any(kw in text for kw in ["左转", "向左", "左旋"]):
        angle = _parse_angle(text, 45)
        angles[0] = max(0, 90 - angle)
        logger.bind(tag=TAG).info(f"解析：左转 → 关节1={angles[0]}°")

    elif any(kw in text for kw in ["右转", "向右", "右旋"]):
        angle = _parse_angle(text, 45)
        angles[0] = min(180, 90 + angle)
        logger.bind(tag=TAG).info(f"解析：右转 → 关节1={angles[0]}°")

    # 关节3（小臂/肘部）：伸出/收回
    if any(kw in text for kw in ["伸出", "伸出去", "往前", "展开"]):
        angle = _parse_angle(text, 120)
        angles[2] = angle
        logger.bind(tag=TAG).info(f"解析：伸出 → 关节3={angle}°")

    elif any(kw in text for kw in ["收回", "缩回", "收回来", "往后", "折叠"]):
        angle = _parse_angle(text, 30)
        angles[2] = angle
        logger.bind(tag=TAG).info(f"解析：收回 → 关节3={angle}°")

    # 关节4（腕部旋转）
    if any(kw in text for kw in ["手腕左转", "手腕右转", "转腕"]):
        angle = _parse_angle(text, 45)
        if "左" in text:
            angles[3] = max(0, 90 - angle)
        else:
            angles[3] = min(180, 90 + angle)
        logger.bind(tag=TAG).info(f"解析：转腕 → 关节4={angles[3]}°")

    # 回家/归位/初始位置
    if any(kw in text for kw in ["回家", "归位", "复位", "初始", "回正", "归零"]):
        angles = [90, 90, 90, 90, 90, 90]
        speed = 60
        logger.bind(tag=TAG).info("解析：归位 → 全部90°")

    angles_str = json.dumps(angles)
    args = {"angles": angles_str, "speed": speed}
    return {"tool": "robot.arm.move_joints", "args": args}


@register_function("move_arm", MOVE_ARM_DESC, ToolType.SYSTEM_CTL)
async def move_arm(conn: "ConnectionHandler", instruction: str) -> ActionResponse:
    """根据自然语言指令控制机械臂。

    Args:
        conn: ConnectionHandler 实例
        instruction: 用户的自然语言指令

    Returns:
        ActionResponse: 执行结果
    """
    import traceback
    logger.bind(tag=TAG).info(f"======================================== MOVE_ARM 入口: {instruction}")
    print(f"[MOVE_ARM] 收到指令: {instruction}", flush=True)

    # 检查 MCP 客户端
    if not hasattr(conn, "mcp_client") or not conn.mcp_client:
        logger.bind(tag=TAG).error(f"MCP客户端不存在! hasattr={hasattr(conn, 'mcp_client')}, mcp_client={getattr(conn, 'mcp_client', 'N/A')}")
        print(f"[MOVE_ARM] MCP客户端不存在!", flush=True)
        return ActionResponse(
            Action.REQLLM,
            "机械臂设备当前不在线，请确认ESP32已连接。",
            None,
        )

    if not await conn.mcp_client.is_ready():
        return ActionResponse(
            Action.REQLLM,
            "机械臂设备正在初始化，请稍后再试。",
            None,
        )

    # 解析指令
    parsed = _parse_instruction(instruction)
    if parsed is None:
        logger.bind(tag=TAG).warning(f"无法解析指令: {instruction}")
        return ActionResponse(
            Action.REQLLM,
            f"抱歉，我没理解「{instruction}」对应什么动作。你可以试试：抬起、放下、左转、右转、伸出去、收回来、张开夹爪、闭合夹爪、归位。",
            None,
        )

    # 执行 MCP 工具调用
    tool_name = parsed["tool"]
    args_json = json.dumps(parsed["args"], ensure_ascii=False)

    logger.bind(tag=TAG).info(f"📡 准备调用 MCP: {tool_name}, args={args_json}, mcp_ready={await conn.mcp_client.is_ready()}")

    try:
        result = await call_mcp_tool(
            conn, conn.mcp_client, tool_name, args_json, timeout=10
        )
        logger.bind(tag=TAG).info(f"✅ MCP 调用成功: {tool_name} → {result}")

        # 构建友好的回复
        tool_desc = {
            "robot.arm.move_joints": f"机械臂已按「{instruction}」移动",
            "robot.arm.gripper": f"夹爪已按「{instruction}」操作",
        }.get(tool_name, f"已执行「{instruction}」")

        return ActionResponse(Action.REQLLM, tool_desc, None)

    except ValueError as e:
        logger.bind(tag=TAG).error(f"工具不可用: {e}")
        return ActionResponse(
            Action.REQLLM,
            f"无法控制机械臂：{e}",
            None,
        )
    except Exception as e:
        logger.bind(tag=TAG).error(f"执行失败: {e}")
        return ActionResponse(
            Action.REQLLM,
            f"机械臂指令执行失败：{e}",
            None,
        )
