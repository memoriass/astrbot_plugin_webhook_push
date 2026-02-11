"""
BGM.tv 媒体数据提供者
提供 BGM.tv (Bangumi.tv) 的数据丰富和图片获取功能
"""

from typing import Any

from .base_provider import BaseProvider, MediaEnrichmentProvider, MediaImageProvider


class BGMProvider(MediaEnrichmentProvider, MediaImageProvider, BaseProvider):
    """BGM.tv 数据和图片提供者"""

    def __init__(self, config: dict[str, Any]):
        BaseProvider.__init__(self, request_interval=0.5)
        self.config = config
        self.base_url = "https://api.bgm.tv"

    @property
    def name(self) -> str:
        return "Bangumi"

    @property
    def priority(self) -> int:
        return 3  # 优先级低于 TMDB

    async def enrich_media_data(self, media_data: dict) -> dict:
        """只针对可能的动漫进行丰富"""
        # 如果已经通过 TMDB 丰富过了，且不是剧集，跳过 (或者根据需要保留)
        if media_data.get("tmdb_enriched") and media_data.get("overview") and media_data.get("item_type") == "Movie":
            return media_data

        name = media_data.get("series_name") or media_data.get("item_name")
        if not name:
            return media_data

        # 1. 搜索作品
        subject = await self._search_subject(name)
        if subject:
            media_data.update(
                {
                    "bgm_id": subject.get("id"),
                    "bgm_enriched": True,
                    "overview": subject.get("summary") or media_data.get("overview"),
                }
            )
            # 如果没有图片，尝试获取 BGM 的图片
            if not media_data.get("image_url"):
                images = subject.get("images", {})
                media_data["image_url"] = images.get("large") or images.get("common")

        return media_data

    async def get_media_image(self, media_data: dict) -> str:
        return await self.get_image(media_data)

    async def get_image(self, media_data: dict) -> str:


        name = media_data.get("series_name") or media_data.get("item_name")
        if not name:
            return ""

        subject = await self._search_subject(name)
        if subject:
            images = subject.get("images", {})
            return images.get("large") or images.get("common") or ""

        return ""

    async def _search_subject(self, name: str) -> dict | None:
        cache_key = f"bgm_search_{name}"
        cached = self._get_from_cache(cache_key)
        if cached:
            return cached

        # BGM V0 Search API (推荐使用)
        url = f"{self.base_url}/search/subject/{name}"
        # 限制类型为 2 (动漫)
        data = await self._http_get(url, params={"type": 2, "max_results": 1})
        if data and data.get("list"):
            subject = data["list"][0]
            self._set_cache(cache_key, subject)
            return subject
        return None
