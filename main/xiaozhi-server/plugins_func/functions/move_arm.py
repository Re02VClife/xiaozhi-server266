"""
机械臂自然语言控制插件（增量式）
==============================
使 LLM 通过自然语言指令控制机械臂，无需理解关节角度。

核心改进（v2）：
  - 增量式控制：先获取当前位置 → 只改用户指定的关节 → 其他关节保持不动
  - 相对指令支持：「再高一点」→ 当前角度+10°
  - 支持 home/stop 直接路由

插件内部负责：
  - 关键词解析（"抬起"/"放下"/"左转"等）
  - get_status 获取当前姿态
  - 调用 robot.arm.* MCP 工具
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
            "用自然语言控制机械臂移动。当用户要求机械臂做任何动作时调用此函数。\n"
            "支持的动作：抬起/放下手臂、左转/右转、伸出去/收回来、张开/闭合夹爪、"
            "归位/回到初始位置、急停。\n"
            "增量式控制：只移动用户指定的关节，其他关节保持当前姿态不变。\n"
            "instruction参数直接传用户的原话即可，例如'抬起来'、'向左转45度'、'再高一点'。\n"
            "⚠️ 用户说任何关于机械臂移动的话都必须调用此函数，不要只回复文字！"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "用户的自然语言指令，直接传原话",
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


async def _get_current_angles(conn: "ConnectionHandler") -> list[int] | None:
    """获取机械臂6关节当前角度（0-180 归一化）。

    调用 robot.arm.get_status MCP 工具，解析 joints[].angle 字段。
    失败时返回 None，调用方降级为默认值。
    """
    if not hasattr(conn, "mcp_client") or not conn.mcp_client:
        return None
    if not await conn.mcp_client.is_ready():
        return None

    try:
        result = await call_mcp_tool(
            conn, conn.mcp_client, "robot.arm.get_status", "{}", timeout=10
        )
        # 解析返回的 JSON
        if isinstance(result, str):
            data = json.loads(result)
        elif isinstance(result, dict):
            data = result
        else:
            return None

        joints = data.get("joints", [])
        if len(joints) < 6:
            return None

        angles = []
        for j in joints:
            angle = j.get("angle", -1)
            if angle < 0:
                return None
            angles.append(int(round(angle)))
        return angles
    except Exception as e:
        logger.bind(tag=TAG).warning(f"获取当前位置失败: {e}")
        return None


async def _parse_single(
    text: str, current: list[int]
) -> dict | None:
    """解析单步自然语言指令。不获取当前位置（由调用方提供）。

    Returns:
        dict: {"tool": "robot.arm.xxx", "args": {...}, "delay": float}
        None: 无法解析
    """
    text = text.strip()
    if not text:
        return None

    speed = 40
    speed_match = re.search(r'速度\s*(\d+)', text)
    if speed_match:
        speed = max(1, min(100, int(speed_match.group(1))))

    delay = 0.0  # 本步之前的等待时间（秒）

    # ========== 系统指令 ==========
    if any(kw in text for kw in ["回家", "归位", "复位", "初始", "回正", "归零", "回到初始", "回到原位"]):
        return {"tool": "robot.arm.home", "args": {}, "delay": delay}

    if any(kw in text for kw in ["急停", "紧急停止", "快停", "停住", "停下"]):
        return {"tool": "robot.arm.stop", "args": {}, "delay": delay}

    # ========== 夹爪控制 ==========
    if any(kw in text for kw in ["张开", "打开", "松开", "释放"]):
        return {"tool": "robot.arm.gripper", "args": {"open": True, "speed": speed}, "delay": delay}
    if any(kw in text for kw in ["闭合", "夹紧", "抓住", "夹住", "合上"]):
        return {"tool": "robot.arm.gripper", "args": {"open": False, "speed": speed}, "delay": delay}

    grip_match = re.search(r'夹爪\s*(\d+)\s*度', text)
    if grip_match:
        pos = max(0, min(180, int(grip_match.group(1))))
        return {"tool": "robot.arm.gripper", "args": {"position": pos}, "delay": delay}

    # ========== 关节移动 ==========
    angles = list(current)
    modified = False

    # 关节2：抬起/放下/点头
    if any(kw in text for kw in ["抬起", "抬高", "举起来", "往上", "上升", "升高", "点头"]):
        angles[1] = min(180, current[1] + _parse_angle(text, 30))
        modified = True
    elif any(kw in text for kw in ["放下", "降下", "降低", "往下", "下降", "落下来"]):
        angles[1] = max(0, current[1] - _parse_angle(text, 30))
        modified = True
    elif any(kw in text for kw in ["再高一点", "再高点", "再高些"]):
        angles[1] = min(180, current[1] + 10)
        modified = True
    elif any(kw in text for kw in ["再低一点", "再低点", "再低些"]):
        angles[1] = max(0, current[1] - 10)
        modified = True

    # 关节1：左转/右转/摇头
    if any(kw in text for kw in ["左转", "向左", "左旋"]):
        angles[0] = max(0, current[0] - _parse_angle(text, 30))
        modified = True
    elif any(kw in text for kw in ["右转", "向右", "右旋"]):
        angles[0] = min(180, current[0] + _parse_angle(text, 30))
        modified = True
    elif any(kw in text for kw in ["摇头"]):
        angles[0] = max(0, current[0] - _parse_angle(text, 20))
        modified = True

    # 关节3：伸出/收回
    if any(kw in text for kw in ["伸出", "伸出去", "往前", "展开", "伸长"]):
        angles[2] = min(180, current[2] + _parse_angle(text, 30))
        modified = True
    elif any(kw in text for kw in ["收回", "缩回", "收回来", "往后", "折叠"]):
        angles[2] = max(0, current[2] - _parse_angle(text, 30))
        modified = True

    # 关节4：腕转
    if any(kw in text for kw in ["手腕左转", "手腕右转", "转腕"]):
        delta = _parse_angle(text, 30)
        if "左" in text:
            angles[3] = max(0, current[3] - delta)
        else:
            angles[3] = min(180, current[3] + delta)
        modified = True

    # 关节5：腕俯仰
    if any(kw in text for kw in ["手腕抬起", "手腕放下", "腕抬", "腕降"]):
        delta = _parse_angle(text, 20)
        if "抬" in text:
            angles[4] = min(180, current[4] + delta)
        else:
            angles[4] = max(0, current[4] - delta)
        modified = True

    if not modified:
        return None

    return {"tool": "robot.arm.move_joints", "args": {"angles": json.dumps(angles), "speed": speed}, "delay": delay}


async def _parse_instruction(
    instruction: str, conn: "ConnectionHandler"
) -> list[dict] | None:
    """解析多步自然语言指令，返回动作序列。

    支持：
      - 单步: "左转45度"
      - 多步: "左转45度然后归位"
      - 延时: "左转45度两秒后归位" / "抬起等3秒放下"

    Returns:
        list: [{"tool": ..., "args": ..., "delay": float}, ...]
        None: 完全无法解析
    """
    text = instruction.strip()

    # 获取当前姿态（多步指令共享一次读取）
    current = await _get_current_angles(conn)
    if current is None:
        current = [92, 0, 179, 169, 16, 32]

    # 按连接词拆分为多步
    # 支持: "然后" "再" "之后" "2秒后" "两秒后" "三秒后" "等X秒" "过X秒"
    # 中文数字映射
    _CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
               "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "半": 0.5}
    parts = []
    delays = [0.0]

    connectors = list(re.finditer(r'((?:\d+|[一两二三四五六七八九十半])\s*秒后|然后再|之后再|再之后|然后|再|等\s*(?:\d+|[一两二三四五六七八九十半])\s*秒|过\s*(?:\d+|[一两二三四五六七八九十半])\s*秒)', text))
    if connectors:
        last_end = 0
        for i, m in enumerate(connectors):
            part = text[last_end:m.start()].strip()
            if part:
                parts.append(part)
            last_end = m.end()
            # 解析延迟（支持中文和阿拉伯数字）
            delay_match = re.search(r'(\d+|[一两二三四五六七八九十半])\s*秒', m.group())
            if delay_match:
                num_str = delay_match.group(1)
                if num_str.isdigit():
                    delays.append(float(num_str))
                else:
                    delays.append(float(_CN_NUM.get(num_str, 0.5)))
            else:
                delays.append(0.5)
        part = text[last_end:].strip()
        if part:
            parts.append(part)
    else:
        parts = [text]

    # 如果只有一步，直接解析
    if len(parts) == 1:
        action = await _parse_single(parts[0], current)
        return [action] if action else None

    # 多步：逐步解析，每步都基于上一轮更新后的 current
    actions = []
    cur = list(current)
    for i, part in enumerate(parts):
        action = await _parse_single(part, cur)
        if action is None:
            logger.bind(tag=TAG).warning(f"多步指令第{i+1}步无法解析: '{part}'，跳过")
            continue
        action["delay"] = delays[i] if i < len(delays) else 0.0
        actions.append(action)
        # 如果是 move_joints，更新 cur 为新的目标角度（供下一步参考）
        if action["tool"] == "robot.arm.move_joints":
            new_angles = json.loads(action["args"]["angles"])
            cur = new_angles
        # home 之后 cur 设为 home angles
        elif action["tool"] == "robot.arm.home":
            cur = [92, 0, 179, 169, 16, 32]

    return actions if actions else None


@register_function("move_arm", MOVE_ARM_DESC, ToolType.SYSTEM_CTL)
async def move_arm(conn: "ConnectionHandler", instruction: str) -> ActionResponse:
    """根据自然语言指令控制机械臂（增量式）。

    Args:
        conn: ConnectionHandler 实例
        instruction: 用户的自然语言指令

    Returns:
        ActionResponse: 执行结果
    """
    logger.bind(tag=TAG).info(f"MOVE_ARM: {instruction}")

    # 检查 MCP 客户端
    if not hasattr(conn, "mcp_client") or not conn.mcp_client:
        logger.bind(tag=TAG).error("MCP客户端不存在!")
        return ActionResponse(
            Action.REQLLM,
            "机械臂设备当前不在线，请确认ESP32已连接。",
            None,
        )

    # 等待 MCP 就绪（MCP初始化可能还没完成，轮询最多等5秒）
    import asyncio as _asyncio
    is_ready = False
    for _attempt in range(10):
        is_ready = await conn.mcp_client.is_ready()
        if is_ready:
            break
        logger.bind(tag=TAG).info(f"MCP未就绪，等待... ({_attempt+1}/10)")
        await _asyncio.sleep(0.5)
    if not is_ready:
        return ActionResponse(
            Action.REQLLM,
            "机械臂设备正在初始化中，请等几秒再试。",
            None,
        )

    # 解析指令 → 返回动作列表（支持多步: "左转45度两秒后归位"）
    actions = await _parse_instruction(instruction, conn)
    if not actions:
        logger.bind(tag=TAG).warning(f"无法解析指令: {instruction}")
        return ActionResponse(
            Action.REQLLM,
            f"抱歉，我没理解「{instruction}」。试试：抬起/放下/左转/右转/归位/张开夹爪/左转45度然后归位",
            None,
        )

    import asyncio as _asyncio

    # 逐步执行
    executed = 0
    last_error = None
    for i, action in enumerate(actions):
        tool_name = action["tool"]
        args = action["args"]
        delay = action.get("delay", 0.0)

        if delay > 0:
            logger.bind(tag=TAG).info(f"⏳ 等待 {delay}秒...")
            await _asyncio.sleep(delay)

        args_json = json.dumps(args, ensure_ascii=False)
        logger.bind(tag=TAG).info(f"📡 第{i+1}/{len(actions)}步: {tool_name} {args_json}")

        try:
            result = await call_mcp_tool(conn, conn.mcp_client, tool_name, args_json, timeout=15)
            logger.bind(tag=TAG).info(f"✅ 第{i+1}步完成: {tool_name}")
            executed += 1
        except Exception as e:
            logger.bind(tag=TAG).error(f"❌ 第{i+1}步失败({tool_name}): {e}")
            last_error = str(e)

    if executed == 0:
        return ActionResponse(Action.RESPONSE, f"失败: {last_error}")

    # 静默模式：关键词指令不走LLM、不生成TTS，秒级完成
    # 需要语音时改成: Action.REQLLM, desc, None
    desc = f"已完成: {instruction} ({executed}/{len(actions)}步)"
    return ActionResponse(Action.RESPONSE, desc, "silent")
