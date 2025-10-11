# astrbot-plugin-CloudImg

> 从 CloudFlare-ImgBed 图床获取随机图片，为 astrbot 增添随机图片功能。

## ✨ 简介

`astrbot-plugin-CloudImg` 是一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计的插件，它专门用于对接基于 [CloudFlare-ImgBed](https://github.com/MarSeventh/CloudFlare-ImgBed) 项目搭建的图床 API。用户可以通过简单的命令，快速获取图床中的一张随机图片。

## 🚀 安装

由于 astrbot 通常通过 Git 或直接复制文件来管理插件，请按照以下步骤进行安装：

1. **克隆或下载** 本项目代码到你 astrbot 的插件目录（通常是 `plugins/` 文件夹下）。
   
   ```bash
   cd path/to/your/astrbot/data/plugins
   git clone https://github.com/Foolllll/astrbot_plugin_CloudImg
   ```
2. **重启** 你的 astrbot 实例，以加载新的插件。

## 📋 使用方法

插件加载并配置完成后，用户可以在聊天中使用以下命令获取随机图片：

| 命令 | 描述 |
| :--- | :--- |
| `/img` | 获取一张随机图片。 |



## 注意事项

本插件目前仅支持对接 [CloudFlare-ImgBed](https://github.com/MarSeventh/CloudFlare-ImgBed) 项目的 API 格式，即 `[base_url]/random?form=text` 返回相对路径的模式。

* 确保你配置的 `base_url` 可以正常访问。
* 如果请求失败，请检查 `base_url` 的设置以及图床的运行状态。

