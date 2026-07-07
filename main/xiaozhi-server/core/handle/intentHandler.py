import json
import uuid
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.utils.dialogue import Message
from core.providers.tts.dto.dto import ContentType
from core.handle.helloHandle import checkWakeupWords
from plugins_func.register import Action, ActionResponse
from core.handle.sendAudioHandle import send_stt_message
from core.handle.reportHandle import enqueue_tool_report
from core.utils.util import remove_punctuation_and_length
from core.providers.tts.dto.dto import TTSMessageDTO, SentenceType

TAG = __name__

# 机械臂控制关键词（用于绕过 LLM 函数调用，直接路由到 move_arm 插件）
# 注意：不含"抓取/抓住/拿起"等需要视觉的指令，这些留给 LLM → vla_grasp
ARM_KEYWORDS = [
    "机械臂", "手臂", "夹爪",
    "抬起", "抬高", "举起来", "往上", "上升", "升高",
    "放下", "降下", "降低", "往下", "下降", "落下来",
    "左转", "向左", "右转", "向右", "转腕",
    "伸出", "伸出去", "往前", "展开",
    "收回", "缩回", "收回来", "往后", "折叠",
    "打开", "张开", "闭合", "夹紧", "合上", "松开", "释放",
    "归位", "回正", "复位", "初始位置", "回零",
]


def _has_arm_command(text: str) -> bool:
    """检测文本是否包含机械臂控制指令"""
    return any(kw in text for kw in ARM_KEYWORDS)


async def _handle_debug_command(conn: "ConnectionHandler", text: str) -> bool:
    """处理调试直控指令（绕过 LLM 和 move_arm 插件，直接调 MCP 工具）。

    格式：
      !J:[90,45,90,90,90,90]  — 直接控制 6 个关节角度
      !J:[90,45,90,90,90,90],speed=60  — 带速度
      !G:open  或  !G:close   — 夹爪控制
      !G:open,50              — 夹爪带速度

    Returns: True 如果处理了调试指令
    """
    if not text.startswith("!"):
        return False

    from core.providers.tools.device_mcp.mcp_handler import call_mcp_tool

    # 检查 MCP 是否就绪
    if not hasattr(conn, "mcp_client") or not conn.mcp_client:
        conn.logger.bind(tag=TAG).warning("调试指令失败: MCP 客户端未初始化")
        return True

    try:
        if text.startswith("!J:"):
            # 关节角度: !J:[90,45,90,90,90,90] 或 !J:[90,45,90,90,90,90],speed=50
            cmd = text[3:].strip()
            speed = 40
            if ",speed=" in cmd:
                parts = cmd.split(",speed=")
                cmd = parts[0]
                speed = int(parts[1])
            angles = json.loads(cmd)
            safe = [max(0, min(180, int(a))) for a in angles[:6]]
            while len(safe) < 6:
                safe.append(90)
            args = json.dumps({"angles": json.dumps(safe), "speed": speed})
            conn.logger.bind(tag=TAG).info(f"🎮 直控关节: {safe}, 速度={speed}")
            result = await call_mcp_tool(conn, conn.mcp_client, "robot.arm.move_joints", args, timeout=10)
            conn.logger.bind(tag=TAG).info(f"🎮 关节结果: {result}")
            await send_stt_message(conn, f"关节→{safe}")
            # 简短 TTS 反馈
            conn.tts.tts_one_sentence(conn, "ok", content_detail=f"关节已调至{safe[1]}度")
            return True

        elif text.startswith("!G:"):
            # 夹爪: !G:open 或 !G:close 或 !G:open,50
            cmd = text[3:].strip()
            speed = 50
            if "," in cmd:
                parts = cmd.split(",")
                cmd = parts[0]
                speed = int(parts[1])
            is_open = cmd.lower() in ("open", "true", "1", "开")
            args = json.dumps({"open": is_open, "speed": speed})
            conn.logger.bind(tag=TAG).info(f"🎮 直控夹爪: {'张开' if is_open else '闭合'}, 速度={speed}")
            result = await call_mcp_tool(conn, conn.mcp_client, "robot.arm.gripper", args, timeout=10)
            conn.logger.bind(tag=TAG).info(f"🎮 夹爪结果: {result}")
            await send_stt_message(conn, f"夹爪→{'张开' if is_open else '闭合'}")
            conn.tts.tts_one_sentence(conn, "ok", content_detail="夹爪已操作")
            return True

    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"调试指令失败: {e}")
        return True

    return False


async def handle_user_intent(conn: "ConnectionHandler", text):
    # 预处理输入文本，处理可能的JSON格式
    try:
        if text.strip().startswith("{") and text.strip().endswith("}"):
            parsed_data = json.loads(text)
            if isinstance(parsed_data, dict) and "content" in parsed_data:
                text = parsed_data["content"]  # 提取content用于意图分析
                conn.current_speaker = parsed_data.get("speaker")  # 保留说话人信息
    except (json.JSONDecodeError, TypeError):
        pass

    # 检查是否有明确的退出命令
    _, filtered_text = remove_punctuation_and_length(text)
    if await check_direct_exit(conn, filtered_text):
        return True

    # 明确再见不被打断
    if conn.is_exiting:
        return True

    # 检查是否是唤醒词
    if await checkWakeupWords(conn, filtered_text):
        return True

    # 🆕 诊断日志：跟踪意图处理路径
    conn.logger.bind(tag=TAG).info(f"🔍 handle_user_intent: text='{text}', intent_type='{conn.intent_type}', has_arm_cmd={_has_arm_command(text)}")

    if conn.intent_type == "function_call":
        # 🆕 调试直控指令（!J: / !G: 前缀，直接调 MCP 工具，不经过 LLM）
        if await _handle_debug_command(conn, text):
            return True

        # 全部指令交 LLM 推理——LLM 自行选择调用 move_arm 或直调 MCP 工具

        # 使用支持function calling的聊天方法,不再进行意图分析
        return False
    # 使用LLM进行意图分析
    intent_result = await analyze_intent_with_llm(conn, text)
    if not intent_result:
        return False
    # 会话开始时生成sentence_id
    conn.sentence_id = str(uuid.uuid4().hex)
    # 处理各种意图
    return await process_intent_result(conn, intent_result, text)


async def check_direct_exit(conn: "ConnectionHandler", text):
    """检查是否有明确的退出命令"""
    _, text = remove_punctuation_and_length(text)
    cmd_exit = conn.cmd_exit
    for cmd in cmd_exit:
        if text == cmd:
            conn.logger.bind(tag=TAG).info(f"识别到明确的退出命令: {text}")
            await send_stt_message(conn, text)
            conn.is_exiting = True
            await conn.close()
            return True
    return False


async def analyze_intent_with_llm(conn: "ConnectionHandler", text):
    """使用LLM分析用户意图"""
    if not hasattr(conn, "intent") or not conn.intent:
        conn.logger.bind(tag=TAG).warning("意图识别服务未初始化")
        return None

    # 对话历史记录
    dialogue = conn.dialogue
    try:
        intent_result = await conn.intent.detect_intent(conn, dialogue.dialogue, text)
        return intent_result
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"意图识别失败: {str(e)}")

    return None


async def process_intent_result(
    conn: "ConnectionHandler", intent_result, original_text
):
    """处理意图识别结果"""
    try:
        # 尝试将结果解析为JSON
        intent_data = json.loads(intent_result)

        # 检查是否有function_call
        if "function_call" in intent_data:
            # 直接从意图识别获取了function_call
            conn.logger.bind(tag=TAG).debug(
                f"检测到function_call格式的意图结果: {intent_data['function_call']['name']}"
            )
            function_name = intent_data["function_call"]["name"]
            if function_name == "continue_chat":
                return False

            if function_name == "result_for_context":
                await send_stt_message(conn, original_text)
                conn.client_abort = False

                def process_context_result():
                    conn.dialogue.put(Message(role="user", content=original_text))

                    from core.utils.current_time import get_current_time_info

                    current_time, today_date, today_weekday, lunar_date = (
                        get_current_time_info()
                    )

                    # 构建带上下文的基础提示
                    context_prompt = f"""当前时间：{current_time}
                                        今天日期：{today_date} ({today_weekday})
                                        今天农历：{lunar_date}

                                        请根据以上信息回答用户的问题：{original_text}"""

                    response = conn.intent.replyResult(context_prompt, original_text)
                    speak_txt(conn, response)

                conn.executor.submit(process_context_result)
                return True

            function_args = {}
            if "arguments" in intent_data["function_call"]:
                function_args = intent_data["function_call"]["arguments"]
                if function_args is None:
                    function_args = {}
            # 确保参数是字符串格式的JSON
            if isinstance(function_args, dict):
                function_args = json.dumps(function_args)

            function_call_data = {
                "name": function_name,
                "id": str(uuid.uuid4().hex),
                "arguments": function_args,
            }

            await send_stt_message(conn, original_text)
            conn.client_abort = False

            # 准备工具调用参数
            tool_input = {}
            if function_args:
                if isinstance(function_args, str):
                    tool_input = json.loads(function_args) if function_args else {}
                elif isinstance(function_args, dict):
                    tool_input = function_args

            # 上报工具调用
            enqueue_tool_report(conn, function_name, tool_input)

            # 使用executor执行函数调用和结果处理
            def process_function_call():
                conn.dialogue.put(Message(role="user", content=original_text))
                
                # 工具调用超时时间
                tool_call_timeout = int(conn.config.get("tool_call_timeout", 30))
                # 使用统一工具处理器处理所有工具调用
                try:
                    result = asyncio.run_coroutine_threadsafe(
                        conn.func_handler.handle_llm_function_call(
                            conn, function_call_data
                        ),
                        conn.loop,
                    ).result(timeout=tool_call_timeout)
                except Exception as e:
                    conn.logger.bind(tag=TAG).error(f"工具调用失败: {e}")
                    result = ActionResponse(
                        action=Action.ERROR, result="工具调用超时，请一会再试下哈", response="工具调用超时，请一会再试下哈"
                    )

                # 上报工具调用结果
                if result:
                    enqueue_tool_report(conn, function_name, tool_input, str(result.result) if result.result else None, report_tool_call=False)

                    # 检查是否静默模式（response="silent"时跳过TTS语音）
                    silent = (hasattr(result, 'response') and result.response == "silent")

                    if result.action == Action.RESPONSE:  # 直接回复（静默模式：不生成语音）
                        text = result.result or result.response or ""
                        if text and not silent:
                            speak_txt(conn, text)
                    elif result.action == Action.REQLLM:  # 调用函数后再请求llm生成回复
                        text = result.result
                        conn.dialogue.put(Message(role="tool", content=text))
                        llm_result = conn.intent.replyResult(text, original_text)
                        if llm_result is None:
                            llm_result = text
                        if not silent:
                            speak_txt(conn, llm_result)
                    elif (
                        result.action == Action.NOTFOUND
                        or result.action == Action.ERROR
                    ):
                        text = result.response if result.response else result.result
                        if text is not None and not silent:
                            speak_txt(conn, text)
                    elif function_name != "play_music":
                        # For backward compatibility with original code
                        # 获取当前最新的文本索引
                        text = result.response
                        if text is None:
                            text = result.result
                        if text is not None:
                            speak_txt(conn, text)

            # 将函数执行放在线程池中
            conn.executor.submit(process_function_call)
            return True
        return False
    except json.JSONDecodeError as e:
        conn.logger.bind(tag=TAG).error(f"处理意图结果时出错: {e}")
        return False


# 🔇 全局TTS开关：改为False即可关闭所有语音
_TTS_ENABLED = False

def speak_txt(conn: "ConnectionHandler", text):
    if not _TTS_ENABLED:
        return
    # 记录文本到 sentence_id 映射
    conn.tts.store_tts_text(conn.sentence_id, text)

    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.FIRST,
            content_type=ContentType.ACTION,
        )
    )
    conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=text)
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.LAST,
            content_type=ContentType.ACTION,
        )
    )
    conn.dialogue.put(Message(role="assistant", content=text))
