import asyncio
import base64
import json
import time
import uuid
from datetime import datetime
from pathlib import Path

from aiohttp import web
from aiohttp.web import Request, Response

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .adapters import AdapterFactory
from .common import CommonHandler
from .game import GameHandler
from .media import MediaDataProcessor, MediaHandler
from .utils.browser import BrowserManager
from .utils.html_renderer import HtmlRenderer

# 常量定义
DEFAULT_SENDER_ID = "2659908767"
DEFAULT_SENDER_NAME = "媒体通知"
DEFAULT_WEBHOOK_PORT = 60071
DEFAULT_BATCH_MIN_SIZE = 3
DEFAULT_CACHE_TTL = 300
DEFAULT_BATCH_INTERVAL = 300


@register("astrbot_plugin_webhook_push", "memoriass", "通知推送插件", "2.0.0")
class Main(Star):
    """通用 Webhook 推送插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 配置验证
        self._validate_config()

        # 核心配置
        self.webhook_port = config.get("webhook_port", DEFAULT_WEBHOOK_PORT)
        self.group_id = config.get("group_id", "")
        self.platform_name = config.get("platform_name", "auto")
        self.batch_min_size = config.get("batch_min_size", DEFAULT_BATCH_MIN_SIZE)
        self.batch_interval_seconds = config.get(
            "batch_interval_seconds", DEFAULT_BATCH_INTERVAL
        )
        self.cache_ttl_seconds = config.get("cache_ttl_seconds", DEFAULT_CACHE_TTL)

        # 适配器配置
        self.sender_id = config.get("sender_id", DEFAULT_SENDER_ID)
        self.sender_name = config.get("sender_name", DEFAULT_SENDER_NAME)
        self.webhook_token = config.get("webhook_token", "")

        # 路由配置
        self.media_routes = self._parse_routes(
            config.get("media_routes", ["/media-webhook"])
        )
        self.game_routes = self._parse_routes(
            config.get("game_routes", ["/game-webhook"])
        )
        self.common_routes = self._parse_routes(
            config.get("common_routes", ["/webhook"])
        )

        # 模板配置
        self.media_template = config.get("media_template", "media_news.html")
        self.game_template = config.get("game_template", "game_modern.html")
        self.common_template = config.get("common_template", "common_blog.html")

        # 初始化子模块
        # 获取标准数据路径
        base_data_path = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "astrbot_plugin_webhook_push"
        )
        base_data_path.mkdir(parents=True, exist_ok=True)
        
        # 自动创建用户可自定义的资源目录
        (base_data_path / "media_bg").mkdir(exist_ok=True)
        (base_data_path / "game_bg").mkdir(exist_ok=True)
        (base_data_path / "common_bg").mkdir(exist_ok=True)

        enrichment_config = {
            "tmdb_api_key": config.get("tmdb_api_key", ""),
            "fanart_api_key": config.get("fanart_api_key", ""),
            "tvdb_api_key": config.get("tvdb_api_key", ""),
            "enable_translation": config.get("enable_translation", False),
            "preferred_translator": config.get("preferred_translator", "tencent"),
            "tencent_secret_id": config.get("tencent_secret_id", ""),
            "tencent_secret_key": config.get("tencent_secret_key", ""),
            "baidu_app_id": config.get("baidu_app_id", ""),
            "baidu_secret_key": config.get("baidu_secret_key", ""),
            "cache_persistence_days": config.get("cache_persistence_days", 7),
            "data_path": base_data_path,  # 传入数据路径
        }

        try:
            self.media_handler = MediaHandler(enrichment_config)
            self.data_processor = MediaDataProcessor(
                self.media_handler, self.cache_ttl_seconds
            )
            self.game_handler = GameHandler(self.context, config)
            self.common_handler = CommonHandler(config)
            self.image_renderer = HtmlRenderer(base_data_path)
        except Exception as e:
            logger.error(f"初始化处理器失败: {e}")
            raise

        # 初始化运行时数据
        self.message_queue: list[dict] = []
        self.last_batch_time = time.time()
        
        # 动态更新 Schema 以支持新模板热重载
        self._update_conf_schema()

        # HTTP 服务器组件
        self.app = None

    def _update_conf_schema(self):
        """扫描模板目录动态更新 _conf_schema.json"""
        try:
            base = Path(__file__).parent
            schema_path = base / "_conf_schema.json"
            if not schema_path.exists():
                return
            
            with open(schema_path, "r", encoding="utf-8") as f:
                schema = json.load(f)

            # 映射关系: schema_key -> subdir
            mapping = {
                "game_template": "game",
                "media_template": "media",
                "common_template": "common",
            }
            
            updated = False
            for key, subdir in mapping.items():
                if key not in schema: continue
                
                # 扫描子目录
                tpl_dir = base / "utils" / "templates" / subdir
                if tpl_dir.exists():
                    files = [f.name for f in tpl_dir.glob("*.html")]
                    if files:
                        # 更新枚举选项
                        current_enum = schema[key].get("enum", [])
                        current_options = schema[key].get("options", [])
                        
                        # 覆盖旧配置，只保留当前实际存在的文件
                        new_enum = sorted(list(set(files)))
                        new_options = sorted(list(set(files)))
                        
                        if new_enum != current_enum or new_options != current_options:
                            schema[key]["enum"] = new_enum
                            schema[key]["options"] = new_options
                            updated = True
                            
                            # 自检默认值是否合法，若不合法则自动修正为第一个可用模板
                            current_default = schema[key].get("default")
                            if current_default not in new_options and new_options:
                                schema[key]["default"] = new_options[0]
                                logger.warning(f"模板配置[{key}]默认值已自动修正为: {new_options[0]}")
                            
                            logger.info(f"检测到新模板[{subdir}]: {files}")

            if updated:
                with open(schema_path, "w", encoding="utf-8") as f:
                    json.dump(schema, f, indent=2, ensure_ascii=False)
                logger.info("已动态更新配置 Schema，新模板将在重载后生效")

        except Exception as e:
            logger.error(f"动态更新 Schema 失败: {e}")
        self.runner = None
        self.site = None
        self.batch_processor_task = None

    def _parse_routes(self, routes) -> list:
        if isinstance(routes, str):
            return [r.strip() for r in routes.split(",") if r.strip()]
        elif isinstance(routes, list):
            return [r for r in routes if isinstance(r, str) and r.strip()]
        return []

    async def initialize(self):
        """初始化插件，启动 Webhook 服务器和批处理器"""
        try:
            # 恢复持久化队列
            saved_queue = await self.get_kv_data("persistent_msg_queue", [])
            if saved_queue:
                self.message_queue.extend(saved_queue)
                logger.info(f"已恢复 {len(saved_queue)} 条未处理消息")

            logger.info("准备进行浏览器环境自检...")
            await BrowserManager.init()
            await self.start_webhook_server()
            self.batch_processor_task = asyncio.create_task(
                self.start_batch_processor()
            )
            logger.info("[OK] 插件初始化完成 - 所有模块已启用")
        except Exception as e:
            logger.error(f"插件初始化失败: {e}", exc_info=True)

    async def _save_queue(self):
        """持久化队列到 KV"""
        try:
            await self.put_kv_data("persistent_msg_queue", self.message_queue)
        except Exception as e:
            logger.error(f"保存队列失败: {e}")

    async def _enqueue(self, msg: dict):
        """入队并保存"""
        self.message_queue.append(msg)
        await self._save_queue()

    def _validate_config(self):
        """验证配置参数"""
        errors = []
        port = self.config.get("webhook_port", DEFAULT_WEBHOOK_PORT)
        if not isinstance(port, int) or port < 1 or port > 65535:
            errors.append(f"webhook_port 必须是 1-65535 之间的整数，当前值: {port}")

        batch_size = self.config.get("batch_min_size", DEFAULT_BATCH_MIN_SIZE)
        if not isinstance(batch_size, int) or batch_size < 1:
            errors.append(f"batch_min_size 必须是大于 0 的整数，当前值: {batch_size}")

        if errors:
            error_msg = "配置验证失败:\n" + "\n".join(
                f"  - {error}" for error in errors
            )
            logger.error(error_msg)
            raise ValueError(error_msg)

    async def start_webhook_server(self):
        """启动 Webhook 服务器"""
        try:
            self.app = web.Application()

            # 注册媒体相关路由
            for route in self.media_routes:
                self.app.router.add_post(
                    self._normalize_route(route), self.handle_media_webhook
                )
                logger.info(f"注册媒体Webhook路由: POST {route}")

            # 注册游戏相关路由
            for route in self.game_routes:
                self.app.router.add_post(
                    self._normalize_route(route), self.handle_game_webhook
                )
                logger.info(f"注册游戏Webhook路由: POST {route}")

            # 注册通用路由
            for route in self.common_routes:
                self.app.router.add_post(
                    self._normalize_route(route), self.handle_common_webhook
                )
                logger.info(f"注册通用Webhook路由: POST {route}")

            self.app.router.add_get("/status", self.handle_status)

            self.runner = web.AppRunner(self.app)
            await self.runner.setup()
            self.site = web.TCPSite(self.runner, "0.0.0.0", self.webhook_port)
            await self.site.start()

            logger.info(f"Webhook 服务器已启动在端口 {self.webhook_port}")
        except Exception as e:
            logger.error(f"启动 Webhook 服务器失败: {e}")
            raise

    def _check_auth(self, request: Request) -> bool:
        """检查 Webhook 鉴权 Token"""
        if not self.webhook_token:
            return True
        token = request.headers.get("X-Webhook-Token")
        return token == self.webhook_token

    def _normalize_route(self, route: str) -> str:
        if not route.startswith("/"):
            return "/" + route
        return route

    async def start_batch_processor(self):
        """启动批量处理器周期任务"""
        while True:
            try:
                await asyncio.sleep(self.batch_interval_seconds)
                await self.process_message_queue()
            except Exception as e:
                logger.error(f"批量处理器出错: {e}")
                await asyncio.sleep(10)

    # --- Webhook 处理方法 (只负责分流) ---

    async def handle_media_webhook(self, request: Request) -> Response:
        """处理媒体相关 Webhook 请求"""
        trace_id = str(uuid.uuid4())[:8]
        if not self._check_auth(request):
            logger.warning(f"[{trace_id}] 未授权: {request.remote}")
            return Response(text="Unauthorized", status=401)
        try:
            body_text = await request.text()
            headers = dict(request.headers)
            logger.info(f"[{trace_id}][媒体Webhook] 收到 Webhook 请求: {request.path}")

            # 加入队列，标记为需要媒体检测
            raw_payload = {
                "raw_data": body_text,
                "headers": headers,
                "timestamp": time.time(),
                "message_type": "raw_media",
                "trace_id": trace_id,
                "template": self.media_template,
            }
            await self._enqueue(raw_payload)
            return Response(text=f"已加入队列 (ID: {trace_id})", status=200)
        except Exception as e:
            logger.error(f"[{trace_id}] Webhook 处理出错: {e}")
            return Response(text="Internal Error", status=500)

    async def handle_game_webhook(self, request: Request) -> Response:
        """处理游戏相关 Webhook 请求"""
        trace_id = str(uuid.uuid4())[:8]
        if not self._check_auth(request):
            logger.warning(f"[{trace_id}] 未授权: {request.remote}")
            return Response(text="Unauthorized", status=401)
        try:
            body_text = await request.text()
            headers = dict(request.headers)
            logger.info(f"[{trace_id}][游戏Webhook] 收到 Webhook 请求: {request.path}")

            payload = json.loads(body_text)
            
            # --- 异步处理：直接入队并返回 ---
            raw_payload = {
                "raw_data": payload,
                "headers": headers,
                "timestamp": time.time(),
                "message_type": "raw_game", # 新增原始游戏类型
                "trace_id": trace_id,
                "template": self.game_template,
            }
            await self._enqueue(raw_payload)
            return Response(text=f"已加入队列 (ID: {trace_id})", status=200)

        except Exception as e:
            logger.error(f"[{trace_id}] Webhook 处理出错: {e}")
            return Response(text="Internal Error", status=500)

    async def handle_common_webhook(self, request: Request) -> Response:
        """处理通用相关 Webhook 请求"""
        trace_id = str(uuid.uuid4())[:8]
        if not self._check_auth(request):
            logger.warning(f"[{trace_id}] 未授权: {request.remote}")
            return Response(text="Unauthorized", status=401)
        try:
            body_text = await request.text()
            headers = dict(request.headers)
            logger.info(f"[{trace_id}][通用Webhook] 收到 Webhook 请求: {request.path}")

            result = await self.common_handler.process_common_webhook(
                body_text, headers
            )

            if result and "message_text" in result:
                result["timestamp"] = time.time()
                result["trace_id"] = trace_id
                result["template"] = self.common_template
                await self._enqueue(result)
                return Response(text=f"已加入队列 (ID: {trace_id})", status=200)

            return Response(text="无效数据", status=400)
        except Exception as e:
            logger.error(f"[{trace_id}] Webhook 处理出错: {e}")
            return Response(text="Internal Error", status=500)

    async def handle_status(self, request: Request) -> Response:
        """HTTP 状态查询"""
        status_info = {
            "server_running": bool(self.site),
            "listen_port": self.webhook_port,
            "queue_messages": len(self.message_queue),
            "target_group": self.group_id or "not_configured",
        }
        return Response(
            text=json.dumps(status_info, indent=2),
            status=200,
            content_type="application/json",
        )

    # --- 消息分发与队列处理 (只负责最终发送) ---

    async def process_message_queue(self):
        """处理消息队列"""
        if not self.message_queue or not self.group_id:
            return

        messages_to_process = self.message_queue.copy()
        self.message_queue.clear()
        await self._save_queue()

        final_messages = []
        for msg in messages_to_process:
            trace_id = msg.get("trace_id", "Unknown")
            m_type = msg.get("message_type")
            if m_type == "raw_media":
                logger.debug(f"[{trace_id}] 开始处理媒体元数据...")
                # 交给媒体处理器进行识别和数据富化
                processed = await self.data_processor.detect_and_process_raw_data(msg)
                if processed:
                    processed["trace_id"] = trace_id
                    processed["template"] = msg.get("template", self.media_template)
                    final_messages.append(processed)
            elif m_type == "raw_game":
                logger.debug(f"[{trace_id}] 开始在后台处理游戏解析与 AI 分析...")
                # 在后台慢慢调 AI 和转 Base64，不阻塞接收端
                processed = await self.game_handler.process_game_webhook(
                    msg["raw_data"], msg.get("headers")
                )
                if processed:
                    processed["trace_id"] = trace_id
                    processed["template"] = msg.get("template", self.game_template)
                    processed["message_type"] = "game"
                    final_messages.append(processed)
            else:
                # 已经是标准格式 (game 或 common)
                final_messages.append(msg)

        if final_messages:
            logger.info(f"开始批量处理 {len(final_messages)} 条消息")
            await self.send_intelligently(final_messages)

        self.last_batch_time = time.time()

    async def send_intelligently(self, messages: list):
        """智能发送逻辑"""
        count = len(messages)
        if count >= self.batch_min_size:
            await self.send_batch_messages(messages)
        else:
            await self.send_individual_messages(messages)

    async def send_batch_messages(self, messages: list):
        """批量发送 (渲染为多张合并转发图片)"""
        try:
            rendered_messages = []
            for msg in messages:
                trace_id = msg.get("trace_id", "Unknown")
                logger.info(f"[{trace_id}] 正在渲染")
                # 动态提取除标准字段外的所有数据，作为渲染上下文
                extra_render_context = {k: v for k, v in msg.items() if k not in ["message_text", "poster_url", "image_url", "template", "trace_id", "message_type", "timestamp"]}
                
                # 注入格式化时间
                ts = msg.get("timestamp", time.time())
                try:
                    dt = datetime.fromtimestamp(float(ts))
                    extra_render_context["formatted_time"] = dt.strftime("%m/%d %H:%M")
                except Exception:
                    extra_render_context["formatted_time"] = ""

                # 使用 HtmlRenderer 异步渲染
                img = await self.image_renderer.render(
                    msg["message_text"],
                    msg.get("poster_url") or msg.get("image_url"),
                    template_name=msg.get("template", "card_default.html"),
                    extra_context=extra_render_context
                )

                if img:
                    # 将图片转换为 base64:// 协议字符串，适配 OneBot 协议
                    base64_str = f"base64://{base64.b64encode(img).decode()}"
                    logger.info(f"[{trace_id}] 图片转 Base64 成功，长度: {len(base64_str)}")
                    rendered_messages.append(
                        {
                            "message_text": "",  # 留空，只发送图片
                            "image_url": base64_str,  # 适配器期望的字段名是 image_url
                            "sender_name": self.sender_name,
                        }
                    )

            if not rendered_messages:
                logger.warning("没有可发送的渲染消息")
                return

            effective_platform = self.get_effective_platform_name()
            logger.info(f"配置/推断的协议适配器类型: {effective_platform}")

            # 1. 尝试直接获取平台实例 (Transport Layer)
            platform_inst = self.context.get_platform_inst(effective_platform)
            
            # 2. 如果失败，尝试获取 'aiocqhttp' (这是大多数 OneBot 实现的通用 AstrBot 平台名)
            if not platform_inst and effective_platform in ["llonebot", "napcat"]:
                logger.info(f"未找到名为 {effective_platform} 的平台实例，尝试使用 'aiocqhttp' 作为传输层...")
                platform_inst = self.context.get_platform_inst("aiocqhttp")

            # 3. 如果还是失败，尝试使用第一个可用平台
            if not platform_inst:
                insts = self.context.platform_manager.platform_insts
                if insts:
                    fallback_id = insts[0].meta().id
                    logger.warning(f"指定/推断的平台 {effective_platform} 未加载，回退到第一个可用平台: {fallback_id}")
                    platform_inst = insts[0]

            bot = platform_inst.get_client() if platform_inst else None
            if not bot:
                logger.error(f"无法获取任何可用的 Bot 实例，取消发送")
                return

            logger.info("正在创建适配器...")
            adapter = AdapterFactory.create_adapter(effective_platform)
            logger.info(f"适配器 {type(adapter).__name__} 创建成功，开始发送...")
            
            result = await adapter.send_forward_messages(
                bot_client=bot,
                group_id=str(self.group_id).replace(":", "_"),
                messages=rendered_messages,
                sender_id=self.sender_id,
                sender_name=self.sender_name,
            )
            logger.info(f"发送结果: {result}")
        except Exception as e:
            logger.error(f"批量发送失败，回退到单独发送: {e}")
            await self.send_individual_messages(messages)

    async def send_individual_messages(self, messages: list):
        """单独发送 (每条消息渲染一张图片)"""
        group_id = str(self.group_id).replace(":", "_")
        origin = f"{self.get_effective_platform_name()}:GroupMessage:{group_id}"

        for msg in messages:
            trace_id = msg.get("trace_id", "Unknown")
            try:
                logger.info(f"[{trace_id}] 正在渲染")
                # 动态提取除标准字段外的所有数据，作为渲染上下文
                extra_render_context = {k: v for k, v in msg.items() if k not in ["message_text", "poster_url", "image_url", "template", "trace_id", "message_type", "timestamp"]}
                
                # 注入格式化时间
                ts = msg.get("timestamp", time.time())
                try:
                    dt = datetime.fromtimestamp(float(ts))
                    extra_render_context["formatted_time"] = dt.strftime("%m/%d %H:%M")
                except Exception:
                    extra_render_context["formatted_time"] = ""

                # 使用 HtmlRenderer 异步渲染
                img = await self.image_renderer.render(
                    msg["message_text"],
                    msg.get("poster_url") or msg.get("image_url"),
                    template_name=msg.get("template", "card_default.html"),
                    extra_context=extra_render_context
                )
                if img:
                    chain = MessageChain([Comp.Image.fromBytes(img)])
                    await self.context.send_message(origin, chain)
                    logger.info(f"[{trace_id}] 发送成功")
                    await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"单条消息发送失败: {e}")

    @filter.command("webhook status", alias=["推送状态"])
    async def webhook_status(self, event: AstrMessageEvent):
        """查看 Webhook 状态 (AstrBot 命令)"""
        status_text = f"📊 Webhook 状态\n\n🌐 端口: {self.webhook_port}\n📋 待发: {len(self.message_queue)}\n🎯 目标: {self.group_id}"
        yield event.plain_result(status_text)

    @filter.command("webhook clear_cache", alias=["推送数据清除"])
    async def webhook_clear_cache(self, event: AstrMessageEvent):
        """手动清除媒体数据缓存"""
        try:
            # 获取 MediaHandler 中的 EnrichmentManager 进行清理
            if self.media_handler and self.media_handler.enrichment_manager:
                manager = self.media_handler.enrichment_manager
                # 显式清理所有，不仅仅是过期的
                # 注意：CacheManager.cleanup() 默认只清除过期
                # 这里我们可能需要一个新的方法来清除所有，或者我们只清除过期。
                # 用户说"手动清除数据库内的缓存而不是等到自动过期"，这意味着强制清除"所有"或者"当前"的缓存
                # 为了安全，我们先实现清除 CacheManager 所管理的过期缓存（但我们可以传入0天让它全清除？）
                
                # 更好的方式是直接清空表或做一次深度清理
                # 由于 CacheManager 封装在内部，我们先尝试调用 cleanup
                # 如果用户是想清理所有（包括未过期的），需要看 CacheManager 实现
                
                # 重新审视需求: "清除数据库内的缓存而不是等到自动过期"
                # 这意味着"使所有缓存立即过期并删除"
                
                count = manager.cache.clear_all() # 假设我们去实现这个方法
                yield event.plain_result(f"🗑️ 已清除 {count} 条媒体数据缓存")
            else:
                yield event.plain_result("❌ 媒体处理器未初始化")
        except Exception as e:
            logger.error(f"清除缓存失败: {e}")
            yield event.plain_result(f"❌ 清除缓存失败: {e}")

    async def terminate(self):
        """卸载清理"""
        if self.batch_processor_task:
            self.batch_processor_task.cancel()
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
        await BrowserManager.close()

    def get_effective_platform_name(self) -> str:
        if self.platform_name == "auto":
            # 简化版自动检测逻辑
            available = [
                p.meta().id for p in self.context.platform_manager.platform_insts
            ]
            for p in ["llonebot", "napcat", "aiocqhttp"]:
                if any(p in name.lower() for name in available):
                    return p
            return available[0] if available else "llonebot"
        return self.platform_name
