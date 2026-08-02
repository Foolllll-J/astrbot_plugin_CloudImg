<div align="center">

# 🌐 CloudFlare 图床助手

<i>🖼️ 集高效上传与智能随机于一体的图床工具</i>

![License](https://img.shields.io/badge/license-AGPL--3.0-green?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10+-blue?style=flat-square&logo=python&logoColor=white)
![AstrBot](https://img.shields.io/badge/framework-AstrBot-ff6b6b?style=flat-square)

</div>

---

## ✨ 简介

一款为 [**AstrBot**](https://github.com/AstrBotDevs/AstrBot) 设计的图床插件。它能从 [CloudFlare-ImgBed 图床](https://github.com/MarSeventh/CloudFlare-ImgBed) 获取随机图片/视频，并支持上传图片/视频到图床。还提供关键词映射功能，可自定义指令获取特定文件夹内容。

---

## 🛠️ 功能

* 🎲 **随机媒体获取**: 支持获取随机图片或视频，提供 `/img` 以及自定义指令。
* ⬆️ **智能上传功能**: 支持上传图片和视频到指定文件夹，自动识别文件类型，并支持回复合并聊天记录批量上传与序号筛选。
* 🔗 **关键词映射**: 管理员可设置自定义关键词关联到特定文件夹，如 `/二次元` 获取二次元文件夹内容。
* 🧠 **去重防重复**: 支持记录关键词最近返回的媒体 ID，并在命中时按配置重试，达到上限后回退使用距离最远的历史 ID。
* 🧰 **灵活内容过滤**: 支持按内容类型筛选（图片、视频或全部）。

---

## 🚀 安装

1. **下载本仓库**。
2. 将整个 `astrbot_plugin_CloudImg` 文件夹放入 `astrbot` 的 `plugins` 目录中。
3. 重启 AstrBot。

---

## ⚙️ 配置说明

首次加载后，请在 AstrBot 后台 -> 插件 页面找到本插件进行设置。所有配置项都有详细的说明和介绍。

---

## 💡 使用方法

### 1. 随机媒体获取

* **获取随机图片/视频**: `/img`

### 2. 文件上传

* **普通上传**: `/上传 <文件夹名>` 或使用别名 `/upload <文件夹名>`
  * 回复一张图片或视频消息，将其上传到指定文件夹
* **合并记录上传**: `/上传 [文件夹名] [序号]`
  * 回复一个**合并聊天记录**，将其中包含的图片/视频上传
  * `序号` 支持多种格式：
    * 全部上传：不填序号，如 `/上传 文件夹`
    * 单个：`/上传 文件夹 1`
    * 范围：`/上传 文件夹 1-5`
    * 指定多个：`/上传 文件夹 1,3,5`
  * 插件会自动过滤合并记录中的文本，仅提取媒体文件
* **权限说明**: 需要管理员权限（如果配置了 `仅管理员可上传`）
* **并发限制**: 同时最多处理 3 个上传任务，多余任务将排队等待

### 3. 关键词映射管理

* **设置映射**: `/imglink <关键词> <文件夹名1,文件夹名2...> [内容类型]`
  * 例如：`/imglink test test` 或 `/imglink test test,test2 img`
  * 支持**一对多映射**：指定多个文件夹（用逗号分隔），触发指令时将从这些文件夹中随机选择一个。
  * 内容类型可选: `img`(图片), `vid`(视频), 未指定则为全部
* **查看映射**: `/imglink` (不带参数)
* **删除映射**: `/imgunlink <关键词> [文件夹名1,文件夹名2...]`
  * 例如：`/imgunlink test` (删除 test 的所有映射) 或 `/imgunlink test 3cy,test1` (仅从 test 中移除指定的文件夹)
* **使用映射**: 设置后直接发送 `/<关键词>` 即可获取对应文件夹的随机内容
  * 支持在关键词后追加类型参数：
    * `/<关键词> v` 或 `/<关键词> vid`：仅获取视频
    * `/<关键词> i` 或 `/<关键词> img`：仅获取图片
  * 例如：`/test v`、`/test i`

---

## 📅 更新日志

详见 [CHANGELOG](CHANGELOG.md)

---

## ❤️ 支持

* [AstrBot 帮助文档](https://astrbot.app)
* [CloudFlare ImgBed 图床文档](https://cfbed.sanyue.de/)
* 如果您在使用中遇到错误或有功能建议，欢迎提交 [Issue](https://github.com/Foolllll-J/astrbot_plugin_CloudImg/issues)。

---

<div align="center">

**如果本插件对你有帮助，欢迎点个 ⭐ Star 支持一下！**

</div>
