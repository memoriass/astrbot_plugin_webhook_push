import base64
import json
import random
from pathlib import Path

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


class GameHandler:
    """游戏Webhook处理器"""

    def __init__(self, context, config: dict = None):
        """初始化游戏处理器"""
        self.context = context
        self.config = config or {}
        # 通过标准 API 获取数据路径，确保 Windows 下路径引用绝对准确
        base_data = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_webhook_push"
        self.bg_resource_path = base_data / "game_bg"
        # 自动创建目录
        self.bg_resource_path.mkdir(parents=True, exist_ok=True)

    async def process_game_webhook(self, payload: dict, headers: dict = None) -> dict:
        """
        处理游戏相关的Webhook推送，采用 AI 前置识别 + 规则兜底机制
        """
        # 结果初始化
        ai_success = False
        parsed_data = {}

        # 1. 尝试 AI 前置分析 (如果配置开启)
        enabled = self.config.get("game_ai_analyze", False)
        logger.info(f"[Webhook] 游戏推送 AI 分析开关状态: {enabled}")
        
        if enabled:
            try:
                logger.info("[AI] 正在发起智能解析请求...")
                ai_result = await self._ai_smart_parse(payload)
                if ai_result and ai_result.get("success"):
                    parsed_data = ai_result
                    ai_success = True
                    logger.info(f"[AI] 已智能识别推送来源: {parsed_data.get('source')}")
                else:
                    logger.warning(f"[AI] 智能解析返回失败结果: {ai_result}")
            except Exception as e:
                logger.error(f"AI 前置分析失败，切换至规则兜底: {e}", exc_info=True)

        # 2. 兜底逻辑 (AI 失败、未开启或识别不全时触发)
        if not ai_success:
            source = self.detect_game_source(payload, headers)
            is_alas = source == "alas"
            is_baas = source == "baas"
            
            # 手动提取字段
            def clean_placeholder(val):
                if not val or (isinstance(val, str) and "{" in val and "}" in val):
                    return None
                return val

            game_name = clean_placeholder(payload.get("game_name") or payload.get("game"))
            if not game_name:
                game_name = "碧蓝航线 (Alas)" if is_alas else "蔚蓝档案 (BAAS)" if is_baas else "未知游戏"

            event_type = clean_placeholder(payload.get("title") or payload.get("event") or payload.get("action")) or "通知"
            detail = clean_placeholder(payload.get("desp") or payload.get("content") or payload.get("message")) or str(payload)
            
            # --- 时间逻辑增强：强制使用当前年月日 ---
            import datetime
            push_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # ------------------------------------
            
            # 针对 Alas 的 content 进行二次开发提取
            if is_alas and "content" in payload:
                content_val = str(payload.get("content", ""))
                # 如果标题包含 "crashed" 而 content 有具体的 Task，则尝试提取
                if "Task `" in content_val and "` failed" in content_val:
                    try:
                        task_name = content_val.split("Task `")[1].split("` failed")[0]
                        event_type = f"任务失败: {task_name}"
                    except:
                        pass
            
            level = str(payload.get("level", "info")).lower()
            level_map = {"error": "严重", "critical": "崩溃", "warning": "警告", "success": "成功", "info": "信息"}
            
            parsed_data = {
                "game_name": game_name,
                "event": event_type,
                "content": detail,
                "level": level_map.get(level, "通知"),
                "source": source,
                "time": push_time
            }

        # 3. 组装消息文本 (供 HtmlRenderer 解析)
        # 规则：第一行是 Title，其余行如果是 "Key: Value" 则为 KV 项，否则为普通文本
        game_name = parsed_data.get("game_name", "未知游戏")
        level_str = parsed_data.get("level", "通知")
        event_str = parsed_data.get("event", "常规通知")
        content_str = parsed_data.get("content", "")
        time_str = parsed_data.get("time", "未知时间")

        # 构造多行文本：第一行固定为游戏名
        message_lines = [
            f"{game_name}",
            f"类型: {level_str}",
            f"事件: {event_str}",
            f"时间: {time_str}"
        ]
        
        # 详情内容作为独立行块添加
        if content_str:
            # 增加一个空行或特殊标记，确保它作为 text 类型被解析，而不是带冒号的行
            message_lines.append("") 
            message_lines.append(content_str)

        if ai_success:
             message_lines.append(f"备注: 由 AI 智能解析完成")

        message_text = "\n".join(message_lines)

        return {
            "status": "success",
            "message_text": message_text,
            "source": parsed_data.get("source", "generic"),
            "game_data": payload,
            "poster_url": self._get_random_bg_for_source(parsed_data.get("source", "generic")),
        }

    async def _ai_smart_parse(self, payload: dict) -> dict:
        """调用 AI 识别来源与分析内容"""
        prompt = (
            "分析以下 Webhook JSON 数据。识别该推送来源于哪个自动化工具或游戏（如 Alas/碧蓝航线、BAAS/蔚蓝档案等），"
            "分析事件类型、严重程度，并提炼核心内容。**请务必使用中文（简体）回答所有文本字段（game_name, event, content）**。不要使用 Emoji。\n"
            "请直接返回 JSON 格式结果，不要包含 Markdown 代码块标签，字段如下：\n"
            "{\"success\": true, \"source\": \"工具标识(\\\"alas\\\"/\\\"baas\\\"/\\\"others\\\")\", \"game_name\": \"游戏名\", "
            "\"event\": \"事件标题\", \"level\": \"严重程度\", \"content\": \"摘要\"}\n\n"
            f"数据：{json.dumps(payload, ensure_ascii=False)}"
        )

        try:
            # 1. 尝试获取当前正在使用的聊天模型实例
            provider = self.context.get_using_provider()
            if not provider:
                logger.warning("[AI] 未检测到任何正在使用的文本对话提供商，请在管理面板配置并“正在使用”一个模型。")
                return {"success": False}
            
            logger.info(f"[AI] 正在通过模型实例 [{provider.meta().id}] 发起智能解析...")
            
            # 2. 直接调用提供商实例的 text_chat 方法，避免 ID 查找失败
            response = await provider.text_chat(
                prompt=prompt,
                system_prompt="你是一个 Webhook 数据分析助手。请分析数据并仅返回一个合法的 JSON 对象。字段包含: success(bool), source(alas/baas/others), game_name(中文), event(中文), level(中文), content(中文)。不要输出 Markdown 代码块标签。"
            )
            
            # v4.x 的 LLMResponse 纯文本结果在 completion_text 属性中
            content = response.completion_text.strip()
            logger.debug(f"[AI] 得到模型原始响应: {content}")
            
            # 清洗内容：提取第一个 { 和最后一个 } 之间的内容
            start_idx = content.find("{")
            end_idx = content.rfind("}")
            if start_idx != -1 and end_idx != -1:
                content = content[start_idx:end_idx+1]
            
            data = json.loads(content)
            data["success"] = True
            return data
        except Exception as e:
            logger.error(f"[AI] 智能解析过程中出现异常: {e}", exc_info=True)
            return {"success": False}

    def _get_random_bg_for_source(self, source: str) -> str:
        """根据来源获取本地随机背景图，返回 base64 data url"""
        if not self.bg_resource_path.exists():
            return ""

        # 搜寻逻辑：
        # 直接使用来源名称作为前缀，例如 source='alas' 匹配 alas001.jpg, alas002.png 等
        # 如果未识别到任何匹配项，则搜索以 'default' 开头的图片
        search_prefix = source.lower() if source else "default"

        # 获取目录下所有匹配的文件
        matches = []
        try:
            for file in self.bg_resource_path.iterdir():
                if file.suffix.lower() in [".jpg", ".jpeg", ".png", ".webp"]:
                    # 匹配逻辑：文件名以来源名开头
                    if file.name.lower().startswith(search_prefix):
                        matches.append(file)

            # 如果来源没有匹配到，或者来源原本就是 default，则尝试寻找 default 开头的图
            if not matches and search_prefix != "default":
                for file in self.bg_resource_path.iterdir():
                    if file.suffix.lower() in [".jpg", ".jpeg", ".png", ".webp"]:
                        if file.name.lower().startswith("default"):
                            matches.append(file)

            logger.info(f"[Webhook] 源 [{source}] 匹配背景图数量: {len(matches)}")
            if not matches:
                return ""

            # 随机选择一张
            selected_file = random.choice(matches)
            
            # 不再转 base64，直接返回文件 URI 以提高渲染速度
            return selected_file.absolute().as_uri()

        except Exception as e:
            logger.error(f"加载本地游戏背景图失败: {e}")
            return ""

    async def _analyze_with_ai(self, payload: dict) -> str:
        """使用 AstrBot LLM 分析推送内容中的错误信息"""
        max_tokens = self.config.get("game_ai_max_tokens", 150)

        prompt = (
            f"你是一个资深的游戏运维专家。请分析以下 Webhook 推送的 JSON 数据，"
            f"特别是检查其中是否包含任何错误、警告或运行异常。如果发现错误，请简要说明原因及可能的解决办法。"
            f"如果没有发现明显错误，请总结该条推送的核心内容。\n"
            f"要求：回答尽量简练，字数严格控制在 {max_tokens} 字以内。\n\n"
            f"数据内容：\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )

        try:
            # 根据 AstrBot AI 逻辑调用 LLM
            llm = self.context.get_llm_chain()
            if not llm:
                return "未配置 AI 模型，无法分析。"

            # 使用 LLM 进行推理
            response = await llm.generate_response(prompt)
            result = response.completion

            # 截断处理 (虽然 prompt 要求了，但还是做一层兜底)
            if len(result) > max_tokens:
                result = result[:max_tokens] + "..."

            return result
        except Exception as e:
            logger.error(f"LLM 请求出错: {e}")
            return f"分析过程出错: {str(e)}"

    def detect_game_source(self, payload: dict, headers: dict = None) -> str:
        """
        检测游戏推送来源
        """
        # 1. 优先通过显式字段识别
        source_field = payload.get("source", "").lower()
        if source_field:
            if "alas" in source_field: return "alas"
            if "baas" in source_field: return "baas"
            return source_field

        # 2. 通过 Payload 内容特征识别 (针对无法自定义 JSON 的 BAAS 等)
        payload_str = str(payload).lower()
        if "baas" in payload_str or "bluearchive" in payload_str or "蔚蓝档案" in payload_str:
            return "baas"
        if "alas" in payload_str or "azurlane" in payload_str or "碧蓝航线" in payload_str:
            return "alas"

        # 3. 通过 HTTP Header 识别
        if headers and "user-agent" in headers:
            ua = headers["user-agent"].lower()
            if "steam" in ua:
                return "steam"
            if "discord" in ua:
                return "discord"
            if "python-requests" in ua:
                # 如果是 python 请求且带有 title/message 字段，极大概率是这类脚本
                if "title" in payload and ("message" in payload or "content" in payload):
                    # 再次尝试从 title 判断
                    title = str(payload.get("title", "")).lower()
                    if "baas" in title: return "baas"
                    if "alas" in title: return "alas"

        return "generic_game"
