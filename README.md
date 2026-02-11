# AstrBot Webhook Push 🚀

通用型 Webhook 推送插件。支持媒体服务器（Emby/Jellyfin/Plex）、自动化脚本（BAAS 等）及各类服务的通知。

## ⚡ 快速开始

1. **配置端口与群组**：在插件配置中设置 `webhook_port` (默认 `60071`) 和 `group_id`。
2. **设置推送地址**：在您的服务中填写以下 URL：

| 场景 | Webhook URL 示例 |
| :--- | :--- |
| **媒体服务器** (Emby/Jellyfin/Plex) | `http://<IP>:60071/media-webhook` |
| **自动化脚本** (BAAS 等游戏脚本) | `http://<IP>:60071/game-webhook` |
| **各类服务** (通用推送) | `http://<IP>:60071/webhook` |

---

## 📸 指令说明

| 指令 | 说明 |
| :--- | :--- |
| `/webhook status` | 查看当前 Webhook 监听状态与统计数据 |

---

## 🧩 配置项详解

- **`media_template`**: 可选 `media_movie_modern.html` (精美海报墙) 或 `media_movie_daily.html` (白底卡片)。
- **`game_ai_analyze`**: 默认开启。使用 AI 对原始推送内容进行智能摘要。
- **`batch_interval_seconds`**: 默认 300 秒。在此时间内若有多条推送将触发**合并转发**，防止刷屏。

---

## 许可证
MIT License
