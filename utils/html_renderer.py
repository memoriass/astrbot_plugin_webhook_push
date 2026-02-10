from pathlib import Path

from astrbot.api import logger
from .browser import render_template


class HtmlRenderer:
    _font_cache = {
        "regular": None,
        "bold": None
    }

    def __init__(self, data_path: Path = None):
        # Path to templates
        self.template_path = Path(__file__).parent / "templates"
        self.data_path = data_path

    def _load_fonts(self):
        """Lazy load fonts into cache"""
        if self._font_cache["regular"] and self._font_cache["bold"]:
            return

        try:
            fonts_base64_dir = Path(__file__).parent / "resources" / "fonts_base64"
            if not self._font_cache["regular"]:
                with open(fonts_base64_dir / "SourceHanSansCN-Regular.txt", "r") as f:
                    self._font_cache["regular"] = f.read().strip()
            
            if not self._font_cache["bold"]:
                with open(fonts_base64_dir / "SourceHanSansCN-Bold.txt", "r") as f:
                    self._font_cache["bold"] = f.read().strip()
        except Exception as e:
            logger.warning(f"读取内嵌字体失败: {e}")

    async def render(
        self, text: str, image_url: str = None, template_name: str = "css_news_card.html",
        extra_context: dict = None
    ) -> bytes:
        # Prapare fonts
        self._load_fonts()
        
        # Parse text into title and items
        lines = text.strip().split("\n")
        title = lines[0] if lines else ""
        items = []

        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue

            # Simple heuristic for key-value pairs
            if "：" in line:
                parts = line.split("：", 1)
                items.append(
                    {"type": "kv", "label": parts[0] + "：", "value": parts[1].strip()}
                )
            elif ":" in line:
                parts = line.split(":", 1)
                items.append(
                    {"type": "kv", "label": parts[0] + ":", "value": parts[1].strip()}
                )
            else:
                items.append({"type": "text", "text": line})

        # 尝试在子目录查找模板
        found_template = template_name
        subdirs = ["game", "media", "common", "."]
        for subdir in subdirs:
            p = self.data_path / "utils" / "templates" / subdir / template_name
            if p.exists():
                # Jinja 加载器是基于 templates 根目录的，所以要带上子目录
                if subdir != ".":
                    found_template = f"{subdir}/{template_name}"
                break
        
        custom_uri = ""
        if self.data_path:
            custom_uri = self.data_path.resolve().as_uri()

        context = {
            "poster_url": image_url or "",
            "title": title,
            "items": items,
            "resource_path": (Path(__file__).parent / "resources").resolve().as_uri(),
            "custom_resource_path": custom_uri,
            "font_base64_regular": self._font_cache["regular"] or "",
            "font_base64_bold": self._font_cache["bold"] or "",
        }
        
        if extra_context:
            context.update(extra_context)

        return await render_template(
            template_path=self.template_path,
            template_name=found_template,
            context=context,
            viewport={"width": 800, "height": 600},  # 减小视口宽度，更像手机卡片
            selector=".card",
            device_scale_factor=1.5, # 足够清晰但不过大
        )
