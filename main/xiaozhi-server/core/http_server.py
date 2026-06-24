import asyncio
from aiohttp import web
from config.logger import setup_logging
from core.api.ota_handler import OTAHandler
from core.api.vision_handler import VisionHandler
from core.handle.feishu_handler import FeishuBot

TAG = __name__


class SimpleHttpServer:
    def __init__(self, config: dict, shared_llm=None):
        self.config = config
        self.logger = setup_logging()
        self.ota_handler = OTAHandler(config)
        self.vision_handler = VisionHandler(config)

        # 飞书 Bot（如果 LLM 可用且配置了飞书）
        self.feishu: FeishuBot = None
        if shared_llm is not None:
            self.feishu = FeishuBot(config, shared_llm)
            if self.feishu.enabled:
                self.logger.bind(tag=TAG).info("[飞书] 飞书 Bot 回调已就绪")

    def _get_websocket_url(self, local_ip: str, port: int) -> str:
        """获取websocket地址

        Args:
            local_ip: 本地IP地址
            port: 端口号

        Returns:
            str: websocket地址
        """
        server_config = self.config["server"]
        websocket_config = server_config.get("websocket")

        if websocket_config and "你" not in websocket_config:
            return websocket_config
        else:
            return f"ws://{local_ip}:{port}/xiaozhi/v1/"

    async def feishu_callback_handler(self, request: web.Request) -> web.Response:
        """飞书事件回调处理"""
        try:
            body = await request.json()

            # 处理 URL 验证
            if "challenge" in body:
                result = self.feishu.verify_url(
                    body.get("challenge", ""),
                    body.get("token", ""),
                )
                return web.json_response(result)

            # 异步处理消息事件（先返回 200，避免飞书超时重试）
            event_type = body.get("header", {}).get("event_type", "")
            if event_type == "im.message.receive_v1":
                asyncio.create_task(
                    self.feishu.handle_callback(body)
                )

            return web.json_response({"code": 0})
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"[飞书] 回调处理异常: {e}")
            return web.json_response({"code": 1, "msg": str(e)})

    async def start(self):
        try:
            server_config = self.config["server"]
            read_config_from_api = self.config.get("read_config_from_api", False)
            host = server_config.get("ip", "0.0.0.0")
            port = int(server_config.get("http_port", 8003))

            if port:
                app = web.Application()

                if not read_config_from_api:
                    # 如果没有开启智控台，只是单模块运行，就需要再添加简单OTA接口，用于下发websocket接口
                    app.add_routes(
                        [
                            web.get("/xiaozhi/ota/", self.ota_handler.handle_get),
                            web.post("/xiaozhi/ota/", self.ota_handler.handle_post),
                            web.options(
                                "/xiaozhi/ota/", self.ota_handler.handle_options
                            ),
                            # 下载接口，仅提供 data/bin/*.bin 下载
                            web.get(
                                "/xiaozhi/ota/download/{filename}",
                                self.ota_handler.handle_download,
                            ),
                            web.options(
                                "/xiaozhi/ota/download/{filename}",
                                self.ota_handler.handle_options,
                            ),
                        ]
                    )
                # 添加路由
                app.add_routes(
                    [
                        web.get("/mcp/vision/explain", self.vision_handler.handle_get),
                        web.post(
                            "/mcp/vision/explain", self.vision_handler.handle_post
                        ),
                        web.options(
                            "/mcp/vision/explain", self.vision_handler.handle_options
                        ),
                    ]
                )

                # 飞书 Bot 回调路由（仅在飞书启用时注册）
                if self.feishu and self.feishu.enabled:
                    app.add_routes(
                        [
                            web.post("/feishu/callback", self.feishu_callback_handler),
                            web.get("/feishu/callback", self.feishu_callback_handler),
                        ]
                    )

                # 运行服务
                runner = web.AppRunner(app)
                await runner.setup()
                site = web.TCPSite(runner, host, port)
                await site.start()

                # 保持服务运行
                while True:
                    await asyncio.sleep(3600)  # 每隔 1 小时检查一次
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"HTTP服务器启动失败: {e}")
            import traceback

            self.logger.bind(tag=TAG).error(f"错误堆栈: {traceback.format_exc()}")
            raise
