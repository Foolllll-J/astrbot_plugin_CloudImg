# 🌐 CloudImg - CloudFlare图床助手

![License](https://img.shields.io/badge/license-AGPL--3.0-green?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10+-blue?style=flat-square&logo=python&logoColor=white)
![AstrBot](https://img.shields.io/badge/framework-AstrBot-ff6b6b?style=flat-square)

一款为 [AstrBot](https://astrbot.app) 设计的跨平台图床管理插件。它能从 CloudFlare-ImgBed 图床获取随机图片/视频，并支持上传图片/视频到图床。还提供关键词映射功能，可自定义指令获取特定文件夹内容。

## ✨ 功能

* **随机媒体获取**: 支持获取随机图片或视频，提供 `/img`以及自定义指令。
* **智能上传功能**: 支持上传图片和视频到指定文件夹，自动识别文件类型。
* **关键词映射**: 管理员可设置自定义关键词关联到特定文件夹，如 `/二次元` 获取二次元文件夹内容。
* **灵活内容过滤**: 支持按内容类型筛选（图片、视频或全部）。

## 🚀 安装

1. **下载本仓库**。
2. 将整个 `astrbot_plugin_CloudImg` 文件夹放入 `astrbot` 的 `plugins` 目录中。
3. 重启 AstrBot。

## ⚙️ 配置

首次加载后，请在 AstrBot 后台 -> 插件页面找到本插件进行设置。

| 配置项 | 说明 | 默认值 |
| ------------------------ | ------------------------------------------------------------------------------------------ | ------------- |
| `base_url` | 图床基础地址，用于获取随机图片和上传 | `""` |
| `auth_code` | 上传认证码（可选） | `""` |
| `show_upload_link` | 上传成功时是否显示链接 | `true` |
| `upload_admin_only` | 是否仅管理员可上传 | `true` |

## 💡 使用

### 1. 随机媒体获取

* **获取随机图片/视频**: `/img`

### 2. 文件上传

* **上传图片/视频**: `/上传 <文件夹名>` 或使用别名 `/upload <文件夹名>`
  * 回复一张图片或视频消息，将其上传到指定文件夹
  * 需要管理员权限（如果配置了 `upload_admin_only`）

### 3. 关键词映射管理

* **设置映射**: `/imglink <关键词> <文件夹名> [内容类型]`
  * 例如：`/imglink 3cy 3cy` 或 `/imglink 3cy 3cy img`
  * 内容类型可选: `img`(图片), `vid`(视频), 未指定则为全部
* **查看映射**: `/imglink` (不带参数)
* **删除映射**: `/imgunlink <关键词>`
* **使用映射**: 设置后直接发送 `/<关键词>` 即可获取对应文件夹的随机内容

## 📝 版本记录

* **v1.1**
  
  * 新增上传功能
  * 新增关键词映射功能，可自定义指令
  * 新增内容类型筛选功能
* **v1.0**
  
  * 插件首次发布
  * 支持 `/img` 指令获取随机图片

## ❤️ 支持

* [AstrBot 帮助文档](https://astrbot.app)
* 如果您在使用中遇到错误或有功能建议，欢迎提交 [Issue](https://github.com/Foolllll-J/astrbot_plugin_CloudImg/issues)。

