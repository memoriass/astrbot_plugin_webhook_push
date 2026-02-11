"""
TMDB 媒体数据提供者
提供 TMDB API 的媒体数据丰富和图片获取功能
"""

import re
import aiohttp
from typing import Any

from astrbot.api import logger

from .base_provider import BaseProvider, MediaEnrichmentProvider, MediaImageProvider


class TMDBProvider(MediaEnrichmentProvider, MediaImageProvider, BaseProvider):
    """TMDB 媒体数据和图片提供者"""

    def __init__(self, api_key: str, fanart_api_key: str = ""):
        BaseProvider.__init__(self, request_interval=0.2)
        self.tmdb_api_key = api_key
        self.fanart_api_key = fanart_api_key
        self.tmdb_base_url = "https://api.themoviedb.org/3"
        self.fanart_base_url = "https://webservice.fanart.tv/v3"

    @property
    def name(self) -> str:
        return "TMDB"

    @property
    def priority(self) -> int:
        return 1

    async def enrich_media_data(self, media_data: dict) -> dict:
        """使用 TMDB API 丰富媒体数据"""
        try:
            if not self.tmdb_api_key:
                return media_data

            raw_type = media_data.get("item_type", "")
            item_type = str(raw_type).title() if raw_type else ""

            if item_type not in ["Movie", "Episode", "Series", "Season"]:
                logger.debug(f"TMDB 跳过不支持的类型: {item_type}")
                return media_data

            p_ids = media_data.get("provider_ids", {})
            tmdb_id = p_ids.get("TMDB") or p_ids.get("Tmdb")
            imdb_id = p_ids.get("IMDB") or p_ids.get("Imdb")

            # 1. 如果已知 ID，直接获取详情
            if tmdb_id:
                if item_type == "Movie":
                    await self._enrich_movie_by_id(media_data, tmdb_id)
                else:
                    await self._enrich_tv_by_id(media_data, tmdb_id)
                
                if media_data.get("tmdb_enriched") or media_data.get("poster_path"):
                    return media_data
                else:
                    logger.warning(f"TMDB ID {tmdb_id} 匹配失败，将尝试通过搜索获取...")

            # 2. 如果只有 IMDB ID
            if imdb_id and not media_data.get("tmdb_enriched"):
                tmdb_id_from_imdb = await self._find_tmdb_id_by_external(imdb_id, "imdb_id")
                if tmdb_id_from_imdb:
                    if item_type == "Movie":
                        await self._enrich_movie_by_id(media_data, tmdb_id_from_imdb)
                    else:
                        await self._enrich_tv_by_id(media_data, tmdb_id_from_imdb)
                    
                    if media_data.get("tmdb_enriched") or media_data.get("poster_path"):
                        return media_data

            # 3. 如果没有 ID 或 ID 匹配失败，按照标题搜索
            if item_type == "Movie":
                return await self._enrich_movie_by_search(media_data)
            else:
                return await self._enrich_tv_by_search(media_data)

        except Exception as e:
            logger.error(f"TMDB 数据丰富出错: {e}")
            return media_data

    async def get_media_image(self, media_data: dict) -> str:
        return await self.get_image(media_data)

    async def get_image(self, media_data: dict) -> str:
        """获取媒体图片"""
        try:
            item_type = media_data.get("item_type", "")
            season_number = media_data.get("season_number")
            episode_number = media_data.get("episode_number")

            if item_type == "Episode" and season_number and episode_number:
                tmdb_id = media_data.get("tmdb_tv_id") or media_data.get("tmdb_id")
                if tmdb_id:
                    details = await self._get_tmdb_episode_details(
                        tmdb_id, season_number, episode_number
                    )
                    if details and details.get("still_path"):
                        return f"https://image.tmdb.org/t/p/w500{details['still_path']}"

            if self.fanart_api_key and item_type != "Movie":
                fanart_image = await self._get_fanart_image(media_data)
                if fanart_image:
                    return fanart_image

            poster_path = media_data.get("poster_path")
            if poster_path:
                return f"https://image.tmdb.org/t/p/w500{poster_path}"

            tmdb_id = media_data.get("tmdb_tv_id") or media_data.get("tmdb_id")
            if tmdb_id and not poster_path:
                try:
                    endpoint = "tv" if media_data.get("tmdb_tv_id") or item_type in ["Series", "Season", "Episode"] else "movie"
                    if endpoint == "movie":
                        await self._enrich_movie_by_id(media_data, tmdb_id)
                    else:
                        await self._enrich_tv_by_id(media_data, tmdb_id)
                    
                    if media_data.get("poster_path"):
                        return f"https://image.tmdb.org/t/p/w500{media_data['poster_path']}"
                except Exception as e:
                    logger.warning(f"补全 TMDB 海报详情失败: {e}")

            if not tmdb_id and not media_data.get("poster_path"):
                try:
                    search_name = media_data.get('series_name') if item_type == 'Episode' else (media_data.get('item_name') or media_data.get('series_name'))
                    if not search_name:
                        search_name = media_data.get('item_name') or media_data.get('series_name')
                    
                    if search_name:
                        self.cache.clear() 
                        logger.warning(f"TMDB ID 缺失，尝试即时搜索: {search_name}")
                        await self.enrich_media_data(media_data)
                        if media_data.get("poster_path"):
                            return f"https://image.tmdb.org/t/p/w500{media_data['poster_path']}"
                except Exception as e:
                    logger.warning(f"即时搜索 TMDB 异常: {e}")

            return ""
        except Exception as e:
            logger.error(f"TMDB 图片获取出错: {e}")
            return ""

    async def _http_get(
        self, url: str, params: dict | None = None, headers: dict | None = None
    ) -> dict | None:
        """封装 aiohttp GET 请求"""
        await self._rate_limit()
        if not headers:
            headers = {}
        headers["User-Agent"] = "AstrBot/1.0 (MediaWebhookPlugin)"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params, headers=headers, timeout=12
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    elif response.status == 401:
                        logger.error("TMDB API Key 无效 (401)")
                        return None
                    else:
                        return None
        except Exception as e:
            logger.error(f"TMDB HTTP 请求异常 ({url}): {e}")
            return None

    # --- 私有方法：详情获取 ---

    async def _enrich_movie_by_id(self, media_data: dict, movie_id: str) -> dict:
        url = f"{self.tmdb_base_url}/movie/{movie_id}"
        data = await self._http_get(
            url, params={"api_key": self.tmdb_api_key, "language": "zh-CN"}
        )
        if data:
            overview = data.get("overview")
            # 如果中文简介为空，尝试获取英文简介
            if not overview:
                eng_data = await self._http_get(url, params={"api_key": self.tmdb_api_key})
                if eng_data:
                    overview = eng_data.get("overview")

            media_data.update(
                {
                    "tmdb_id": data.get("id"),
                    "overview": overview or media_data.get("overview"),
                    "year": (data.get("release_date") or "")[:4],
                    "poster_path": data.get("poster_path") or media_data.get("poster_path"),
                    "tmdb_enriched": True,
                }
            )
        return media_data

    async def _enrich_tv_by_id(self, media_data: dict, tv_id: str) -> dict:
        url = f"{self.tmdb_base_url}/tv/{tv_id}"
        data = await self._http_get(
            url, params={"api_key": self.tmdb_api_key, "language": "zh-CN"}
        )
        
        if not data:
            data = await self._http_get(
                url, params={"api_key": self.tmdb_api_key}
            )

        if data:
            poster = data.get("poster_path") or media_data.get("poster_path")
            media_data.update(
                {
                    "tmdb_tv_id": data.get("id"),
                    "poster_path": poster,
                    "year": (data.get("first_air_date") or "")[:4],
                }
            )
            season = media_data.get("season_number")
            episode = media_data.get("episode_number")
            if season and episode:
                ep_data = await self._get_tmdb_episode_details(
                    data.get("id"), season, episode
                )
                if ep_data:
                    overview = ep_data.get("overview")
                    # 剧集同样增加英文回退
                    if not overview:
                        eng_ep_data = await self._get_tmdb_episode_details(data.get("id"), season, episode, language=None)
                        if eng_ep_data:
                            overview = eng_ep_data.get("overview")

                    media_data.update(
                        {
                            "item_name": ep_data.get("name")
                            or media_data.get("item_name"),
                            "overview": overview or media_data.get("overview"),
                            "tmdb_enriched": True,
                        }
                    )
        return media_data

    # --- 私有方法：搜索逻辑 ---

    async def _enrich_movie_by_search(self, media_data: dict) -> dict:
        name = media_data.get("item_name")
        year = media_data.get("year")
        if not name:
            return media_data

        search_url = f"{self.tmdb_base_url}/search/movie"
        params = {"api_key": self.tmdb_api_key, "query": name}
        if year:
            params["year"] = year

        results = await self._http_get(search_url, params=params)
        if results and results.get("results"):
            best_match = self._find_best_match(name, results["results"], "title")
            if best_match:
                return await self._enrich_movie_by_id(media_data, best_match["id"])
        return media_data

    async def _enrich_tv_by_search(self, media_data: dict) -> dict:
        name = media_data.get("series_name") or media_data.get("item_name")
        if not name:
            return media_data

        search_url = f"{self.tmdb_base_url}/search/tv"
        params = {"api_key": self.tmdb_api_key, "query": name}
        
        results = await self._http_get(search_url, params=params)
        
        if not (results and results.get("results")):
            cleaned_name = re.sub(r'\d{4}$', '', name).strip()
            if cleaned_name and cleaned_name != name:
                 params["query"] = cleaned_name
                 results = await self._http_get(search_url, params=params)

        if results and results.get("results"):
            best_match = self._find_best_match(name, results["results"], "name")
            if best_match:
                return await self._enrich_tv_by_id(media_data, best_match["id"])
            
        return media_data

    def _find_best_match(self, query: str, results: list, key: str) -> dict | None:
        """寻找最佳匹配"""
        if not results:
            return None
            
        query_clean = self._clean_title(query)
        for res in results:
            res_title = res.get(key, "")
            res_clean = self._clean_title(res_title)
            if query_clean == res_clean or query_clean in res_clean or res_clean in query_clean:
                return res
            
            orig_key = f"original_{key}"
            orig_title = res.get(orig_key, "")
            orig_clean = self._clean_title(orig_title)
            if orig_clean and (query_clean == orig_clean or query_clean in orig_clean or orig_clean in query_clean):
                return res

        return results[0]

    def _clean_title(self, title: str) -> str:
        """清理标题"""
        if not title:
            return ""
        title = re.sub(r"\(.*?\)", "", title)
        title = re.sub(r"[^\w\s\u4e00-\u9fa5]", "", title)
        return title.lower().strip()

    async def _find_tmdb_id_by_external(
        self, external_id: str, source: str
    ) -> str | None:
        url = f"{self.tmdb_base_url}/find/{external_id}"
        params = {"api_key": self.tmdb_api_key, "external_source": source}
        data = await self._http_get(url, params=params)
        if data:
            for key in ["movie_results", "tv_results"]:
                if data.get(key):
                    return data[key][0].get("id")
            
            if data.get("tv_episode_results"):
                ep = data["tv_episode_results"][0]
                show_id = ep.get("show_id")
                return show_id or ep.get("id")
        return None

    async def _get_tmdb_episode_details(
        self, tv_id: Any, season: Any, episode: Any, language: str | None = "zh-CN"
    ) -> dict | None:
        url = f"{self.tmdb_base_url}/tv/{tv_id}/season/{season}/episode/{episode}"
        params = {"api_key": self.tmdb_api_key}
        if language:
            params["language"] = language
        return await self._http_get(url, params=params)

    async def _get_fanart_image(self, media_data: dict) -> str:
        tmdb_id = media_data.get("tmdb_tv_id") or media_data.get("tmdb_id")
        if not tmdb_id:
            return ""

        url = f"{self.fanart_base_url}/tv/{tmdb_id}"
        data = await self._http_get(url, params={"api_key": self.fanart_api_key})
        if data:
            for key in ["tvposter", "tvbanner"]:
                if data.get(key):
                    return data[key][0].get("url")
        return ""
