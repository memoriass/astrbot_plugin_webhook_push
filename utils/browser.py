import asyncio
from pathlib import Path

import jinja2
from playwright.async_api import Browser, Page, async_playwright

from astrbot.api import logger


class BrowserManager:
    _init_lock: asyncio.Lock | None = None
    _playwright = None
    _browser: Browser | None = None

    @classmethod
    async def get_browser(cls) -> Browser:
        if cls._browser is None:
            await cls.init()
        return cls._browser

    @classmethod
    async def init(cls):
        if cls._init_lock is None:
            cls._init_lock = asyncio.Lock()

        async with cls._init_lock:
            if cls._playwright is None:
                logger.info("正在启动 Webhook 插件的 Playwright 驱动...")
                cls._playwright = await async_playwright().start()

            if cls._browser is None:
                try:
                    logger.info("正在启动 Webhook 插件的 Chromium 浏览器...")
                    cls._browser = await cls._playwright.chromium.launch(
                        headless=True,
                        args=[
                            "--no-sandbox",
                            "--disable-setuid-sandbox",
                            "--disable-dev-shm-usage", # 处理 Docker shm 内存过小问题
                            "--disable-gpu", # 禁用 GPU 加速
                            "--disable-web-security",
                            "--allow-file-access-from-files",
                        ],
                    )
                    logger.info("Webhook 插件内嵌浏览器启动成功")
                except Exception as e:
                    logger.error(f"Webhook 插件启动浏览器失败: {e}")
                    raise

    @classmethod
    async def close(cls):
        if cls._browser:
            await cls._browser.close()
            cls._browser = None
        if cls._playwright:
            await cls._playwright.stop()
            cls._playwright = None


class PageContext:
    def __init__(self, viewport=None, device_scale_factor=1, **kwargs):
        self.viewport = viewport or {"width": 800, "height": 600}
        self.scale_factor = device_scale_factor
        self.page = None

    async def __aenter__(self) -> Page:
        browser = await BrowserManager.get_browser()
        # 加入针对 B 站等防盗链站点的 Referer 兼容
        context = await browser.new_context(
            viewport=self.viewport,
            device_scale_factor=self.scale_factor,
            extra_http_headers={"Referer": "https://www.bilibili.com/"},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        self.page = await context.new_page()
        return self.page

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.page:
            await self.page.close()
            await self.page.context.close()


async def render_template(
    template_path: Path,
    template_name: str,
    context: dict,
    viewport: dict = None,
    selector: str = "body",
    device_scale_factor: float = 1.5,  # 降低默认缩放比例以减小图片体积
) -> bytes:
    """渲染模板并截图"""
    if viewport is None:
        viewport = {"width": 800, "height": 600}

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_path)),
        enable_async=True,
        autoescape=jinja2.select_autoescape(["html", "xml"]),
    )
    template = env.get_template(template_name)
    html_content = await template.render_async(**context)

    async with PageContext(viewport=viewport, device_scale_factor=device_scale_factor) as page:
        logger.info(f"[{template_name}] 开始渲染页面内容 (HTML大小: {len(html_content)/1024:.2f} KB)...")
        # 使用 domcontentloaded 以在 Docker 下获得更快的响应，避免 load 状态死等
        try:
            await page.set_content(html_content, wait_until="domcontentloaded", timeout=30000)
            logger.info(f"[{template_name}] 页面基础内容加载完成")
        except Exception as e:
            logger.error(f"[{template_name}] 页面 set_content 失败/超时: {e}")
            raise

        if selector == "body":
            logger.info("正在对整个页面进行截图...")
            screenshot = await page.screenshot(type="png", full_page=True)
            logger.info("截图完成")
            return screenshot

        try:
            logger.debug(f"等待选择器 {selector} 可见...")
            # Wait for selector to ensure store's ready
            try:
                await page.wait_for_selector(selector, state="visible", timeout=3000)
            except Exception as e:
                logger.warning(f"选择器 {selector} 等待超时: {e}")

            logger.info(f"正在对选择器 {selector} 进行截图...")
            locator = page.locator(selector)
            img = await locator.screenshot(type="png")
            logger.info("截图完成，返回图片数据。")
            return img
        except Exception as e:
            logger.warning(f"选择器 {selector} 截图失败: {e}. 回退到全屏截图。")
            return await page.screenshot(type="png", full_page=True)
