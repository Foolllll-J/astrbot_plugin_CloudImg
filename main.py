import asyncio
import ipaddress
import json
import os
import random
import re
import socket
import string
from urllib.parse import quote, unquote, urljoin, urlparse

import aiohttp
from aiohttp.abc import AbstractResolver
from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Reply as ApiReply
from astrbot.api.message_components import Video
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.components import Image, Plain
from astrbot.core.platform.astr_message_event import (
    AstrMessageEvent as BaseAstrMessageEvent,
)
from astrbot.core.utils.media_utils import MediaResolver
from astrbot.core.utils.session_waiter import (
    SessionController,
    SessionFilter,
    session_waiter,
)

from .helpers.aiocqhttp import AiocqhttpMixin
from .helpers.telegram import TelegramMixin
from .helpers.utils import UtilsMixin


class _PinnedResolver(AbstractResolver):
    """Resolve one request's hostname to the addresses already safety-checked."""

    def __init__(self, addresses: list[str]):
        self._addresses = tuple(addresses)

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_UNSPEC,
    ) -> list[dict]:
        resolved = []
        for address in self._addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                continue

            address_family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
            if family not in (socket.AF_UNSPEC, address_family):
                continue
            resolved.append(
                {
                    "hostname": host,
                    "host": address,
                    "port": port,
                    "family": address_family,
                    "proto": 0,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        return resolved

    async def close(self) -> None:
        return None


class _SenderSessionFilter(SessionFilter):
    """Keep confirmation messages bound to the original sender."""

    def filter(self, event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}:sender:{event.get_sender_id()}"


class CloudImgPlugin(Star, UtilsMixin, TelegramMixin, AiocqhttpMixin):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config

        # ── 图床连接 ──
        server = config.get("server", {})
        # Keep reading the old top-level location for existing configurations.
        self.verify_ssl = bool(server.get("verify_ssl", config.get("verify_ssl", True)))
        self.base_url = self._normalize_base_url(server.get("base_url", ""))
        self.upload_api_url = self.base_url
        self.public_base_url = self._normalize_base_url(
            server.get("public_base_url", "")
        )
        self.auth_code = str(server.get("auth_code", "") or "").strip()
        self.api_token = str(server.get("api_token", "") or "").strip()
        self.random_path_suffix = "/random?form=text"

        # ── 上传设置 ──
        upload = config.get("upload", {})
        self.upload_admin_only = upload.get("admin_only", True)
        self.show_upload_link = upload.get("show_link", True)

        # ── 管理功能设置 ──
        manage = config.get("manage", {})
        self.list_page_size = self._clamp_config_int(
            manage.get("list_page_size", 10),
            min_value=5,
            max_value=50,
            default=10,
        )
        self.url_upload_whitelist = self._normalize_host_whitelist(
            manage.get("url_upload_whitelist", [])
        )
        self.llm_tools_enabled = bool(manage.get("llm_tools_enabled", False))

        # ── 随机获取设置 ──
        randomizer = config.get("randomizer", {})
        self.local_random_type = randomizer.get("local_random_type", False)
        self.keyword_recent_media_limit = self._clamp_config_int(
            randomizer.get("dedupe_window", 0),
            min_value=0,
            max_value=None,
            default=0,
        )
        self.keyword_dedupe_retry_limit = self._clamp_config_int(
            randomizer.get("dedupe_retry", 2),
            min_value=0,
            max_value=10,
            default=2,
        )
        self.plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_CloudImg")

        os.makedirs(self.plugin_data_dir, exist_ok=True)

        self.keyword_folder_map = {}
        self.config_keyword_map: dict[str, dict] = {}
        self.effective_keyword_map: dict[str, dict] = {}
        self.mappings_file = os.path.join(self.plugin_data_dir, "keyword_mappings.json")
        self.load_keyword_mappings()
        self.load_config_keyword_templates()
        self.refresh_effective_keyword_map()
        self.keyword_recent_media_ids: dict[str, list[str]] = {}
        self.keyword_recent_media_kv_key = "keyword_recent_media_ids"
        self.tag_random_count_cache: dict[
            tuple[str, str, tuple[str, ...]], int
        ] = {}

        if self.llm_tools_enabled:
            from .tools.manage import (
                CloudImgDeleteFolderTool,
                CloudImgDeleteTool,
                CloudImgGetFileTool,
                CloudImgListTool,
                CloudImgStatTool,
                CloudImgUploadUrlTool,
            )

            self.context.add_llm_tools(
                CloudImgListTool(plugin=self),
                CloudImgStatTool(plugin=self),
                CloudImgGetFileTool(plugin=self),
                CloudImgDeleteTool(plugin=self),
                CloudImgDeleteFolderTool(plugin=self),
                CloudImgUploadUrlTool(plugin=self),
            )

    # ==================== 配置文件管理 ====================

    def load_keyword_mappings(self):
        """从文件加载关键词媒体筛选映射。"""
        try:
            if os.path.exists(self.mappings_file):
                with open(self.mappings_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.keyword_folder_map = {}
                    for keyword, value in data.items():
                        if isinstance(value, str):
                            self.keyword_folder_map[keyword] = {
                                "folder": value,
                                "content_type": "image,video",
                            }
                        else:
                            self.keyword_folder_map[keyword] = value
            else:
                self.keyword_folder_map = {}
        except Exception as e:
            logger.error(f"加载关键词映射失败: {e}")
            self.keyword_folder_map = {}

    def save_keyword_mappings(self):
        """保存关键词媒体筛选映射到文件。"""
        try:
            with open(self.mappings_file, "w", encoding="utf-8") as f:
                json.dump(self.keyword_folder_map, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存关键词映射失败: {e}")

    def _normalize_folder_names(self, folder_value: object) -> list[str]:
        if isinstance(folder_value, list):
            candidates = folder_value
        else:
            candidates = str(folder_value or "").replace("，", ",").split(",")

        folders: list[str] = []
        for item in candidates:
            folder = str(item or "").strip()
            if folder and folder not in folders:
                folders.append(folder)
        return folders

    def _normalize_tag_names(self, tag_value: object) -> list[str]:
        if isinstance(tag_value, (list, tuple, set)):
            candidates = tag_value
        else:
            candidates = str(tag_value or "").replace("，", ",").split(",")

        tags: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            tag = str(item or "").strip()
            normalized = tag.casefold()
            if tag and normalized not in seen:
                tags.append(tag)
                seen.add(normalized)
        return tags

    def _build_tag_random_cache_key(
        self,
        folder_name: str,
        content_type: str,
        include_tags: list[str],
    ) -> tuple[str, str, tuple[str, ...]]:
        normalized_folder = str(folder_name or "").strip().strip("/")
        normalized_type = self._normalize_content_type(content_type)
        normalized_tags = tuple(tag.casefold() for tag in include_tags)
        return normalized_folder, normalized_type, normalized_tags

    def _normalize_content_type(self, content_type: object) -> str:
        normalized = str(content_type or "").strip().lower()
        if normalized in {"image", "video", "image,video"}:
            return normalized
        return "image,video"

    def _build_mapping_entry(
        self,
        folders: object,
        content_type: object = "image,video",
        tags: object = None,
        source: str | None = None,
    ) -> dict | None:
        normalized_folders = self._normalize_folder_names(folders)
        normalized_tags = self._normalize_tag_names(tags)
        if not normalized_folders and not normalized_tags:
            return None

        mapping = {
            "folder": ",".join(normalized_folders),
            "tags": normalized_tags,
            "content_type": self._normalize_content_type(content_type),
        }
        if source:
            mapping["source"] = source
        return mapping

    def load_config_keyword_templates(self):
        self.config_keyword_map = {}
        templates = self.config.get("keyword_templates", [])
        if not isinstance(templates, list):
            return

        for item in templates:
            if not isinstance(item, dict):
                continue

            keywords = item.get("keywords", [])
            folders = item.get("folders", [])
            if not isinstance(keywords, list):
                continue

            mapping_entry = self._build_mapping_entry(
                folders=folders,
                content_type=item.get("content_type", "image,video"),
                tags=item.get("tags", item.get("include_tags", [])),
                source="template",
            )
            if not mapping_entry:
                continue

            for keyword in keywords:
                normalized_keyword = str(keyword or "").strip()
                if not normalized_keyword:
                    continue
                self.config_keyword_map[normalized_keyword] = dict(mapping_entry)

    def refresh_effective_keyword_map(self):
        effective_map: dict[str, dict] = {
            key: dict(value)
            for key, value in self.config_keyword_map.items()
            if isinstance(value, dict)
        }

        for key, value in self.keyword_folder_map.items():
            if isinstance(value, str):
                mapping_entry = self._build_mapping_entry(
                    folders=value,
                    content_type="image,video",
                    source="runtime",
                )
            elif isinstance(value, dict):
                mapping_entry = self._build_mapping_entry(
                    folders=value.get("folder", ""),
                    content_type=value.get("content_type", "image,video"),
                    tags=value.get("tags", value.get("include_tags", [])),
                    source="runtime",
                )
            else:
                mapping_entry = None

            if mapping_entry:
                effective_map[key] = mapping_entry

        self.effective_keyword_map = effective_map

    def _build_connector(
        self, resolver: AbstractResolver | None = None
    ) -> aiohttp.TCPConnector:
        """Build a connector honoring the plugin SSL verification setting."""
        return aiohttp.TCPConnector(
            ssl=self.verify_ssl,
            resolver=resolver,
            use_dns_cache=resolver is None,
        )

    def _auth_headers(self) -> dict[str, str]:
        token = (self.api_token or "").strip()
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _require_api_token(self) -> str | None:
        if (self.api_token or "").strip():
            return None
        return "请先在插件配置中填写 API Token（列表/标签随机/删除必填）。详见 README。"

    def _require_base_url(self) -> str | None:
        if self.base_url:
            return None
        return "请先配置 base_url。"

    async def _api_request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        data: aiohttp.FormData | None = None,
        require_token: bool = True,
        expect_json: bool = True,
    ):
        """Call a CloudFlare ImgBed API and return JSON or a user-facing error."""
        base_err = self._require_base_url()
        if base_err:
            return base_err

        if require_token:
            token_err = self._require_api_token()
            if token_err:
                return token_err

        normalized_path = (path or "").strip()
        if not normalized_path.startswith("/"):
            normalized_path = f"/{normalized_path}"
        url = f"{self.base_url}{normalized_path}"
        headers = self._auth_headers() if require_token or self.api_token else {}

        try:
            async with aiohttp.ClientSession(
                connector=self._build_connector()
            ) as session:
                async with session.request(
                    method.upper(),
                    url,
                    params=params,
                    data=data,
                    headers=headers or None,
                ) as response:
                    response_text = await response.text()
                    if response.status != 200:
                        return self._handle_response_error(
                            response.status, response_text
                        )

                    if not expect_json:
                        return response_text

                    text = (response_text or "").strip()
                    if not text:
                        return {}
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        logger.error(f"API 响应不是有效 JSON: path={normalized_path}")
                        return "操作失败: 图床返回了非 JSON 响应"
        except Exception as e:
            logger.error(
                f"API 请求失败: method={method}, path={normalized_path}, "
                f"err={type(e).__name__}: {e}"
            )
            return "请求失败，请检查网络与 base_url。"

    def resolve_media_display_url(self, path: str) -> str:
        """Resolve a CloudFlare ImgBed path to an absolute public URL."""
        value = (path or "").strip()
        if not value:
            return ""

        parsed = urlparse(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            resolved = self._build_upload_display_url(value)
        else:
            if not (self.public_base_url or self.base_url):
                return ""
            normalized = value.lstrip("/")
            relative = (
                f"/{normalized}"
                if normalized.startswith("file/")
                else f"/file/{normalized}"
            )
            resolved = self._build_upload_display_url(relative)

        resolved_parsed = urlparse(resolved)
        if (
            resolved_parsed.scheme not in {"http", "https"}
            or not resolved_parsed.netloc
        ):
            return ""
        return resolved

    def guess_media_type(self, path_or_url: str) -> str:
        value = path_or_url or ""
        path = (
            urlparse(value).path.lower()
            if "://" in value
            else value.split("?", 1)[0].split("#", 1)[0].lower()
        )
        video_exts = (
            ".mp4",
            ".avi",
            ".mov",
            ".mkv",
            ".wmv",
            ".flv",
            ".webm",
            ".m4v",
        )
        image_exts = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")
        if any(path.endswith(ext) for ext in video_exts):
            return "video"
        if any(path.endswith(ext) for ext in image_exts):
            return "image"
        return "file"

    @staticmethod
    def _normalize_host_whitelist(value: object) -> list[str]:
        if isinstance(value, str):
            candidates = value.replace("，", ",").split(",")
        elif isinstance(value, list):
            candidates = value
        else:
            candidates = []

        hosts: list[str] = []
        for item in candidates:
            text = str(item or "").strip().lower()
            if not text:
                continue

            try:
                if "://" in text:
                    host = urlparse(text).hostname or ""
                else:
                    authority = text.split("/", 1)[0].strip()
                    ip_candidate = (
                        authority[2:] if authority.startswith("*.") else authority
                    )
                    try:
                        ipaddress.ip_address(ip_candidate)
                        host = ip_candidate
                    except ValueError:
                        host = urlparse(f"//{authority}").hostname or ""
            except ValueError:
                continue

            host = host.lower().strip().rstrip(".")
            if host.startswith("*."):
                host = host[2:]
            if host and host not in hosts:
                hosts.append(host)
        return hosts

    @staticmethod
    def _host_matches_whitelist(host: str, whitelist: list[str]) -> bool:
        host = (host or "").lower().rstrip(".")
        if not host:
            return False
        return any(
            host == entry.rstrip(".") or host.endswith(f".{entry.rstrip('.')}")
            for entry in whitelist
            if entry
        )

    def _host_from_url(self, value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if "://" not in text:
            text = f"https://{text}"
        return (urlparse(text).hostname or "").lower().rstrip(".")

    def _effective_url_upload_whitelist(self) -> list[str]:
        """Return the image-bed hosts plus configured additional hosts."""
        hosts: list[str] = []
        for base in (self.base_url, self.public_base_url):
            host = self._host_from_url(base)
            if host and host not in hosts:
                hosts.append(host)
        for host in getattr(self, "url_upload_whitelist", None) or []:
            normalized = str(host or "").lower().rstrip(".")
            if normalized and normalized not in hosts:
                hosts.append(normalized)
        return hosts

    @staticmethod
    def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return (
            not ip.is_global
            or ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
            or (ip.version == 4 and ip in ipaddress.ip_network("169.254.0.0/16"))
            or (ip.version == 6 and ip in ipaddress.ip_network("fc00::/7"))
        )

    @staticmethod
    def _parse_download_url(url: str):
        value = (url or "").strip()
        try:
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return None, "url 必须是有效的 http(s) 链接"
            if parsed.username or parsed.password:
                return None, "url 不允许包含用户名或密码"
            host = parsed.hostname
            port = parsed.port
        except ValueError:
            return None, "url 主机名或端口无效"

        if not host:
            return None, "url 主机名无效"
        if port is not None and not 1 <= port <= 65535:
            return None, "url 端口无效"
        return parsed, None

    def validate_download_url(
        self, url: str, *, resolve_dns: bool = True
    ) -> str | None:
        """Validate an URL before the LLM URL-upload tool downloads it."""
        parsed, parse_err = self._parse_download_url(url)
        if parse_err:
            return parse_err

        host = parsed.hostname or ""
        host_lower = host.lower().rstrip(".")
        allowed = self._effective_url_upload_whitelist()
        if not allowed:
            return "请先配置 base_url 或 public_base_url，或填写 URL 上传主机白名单"
        if not self._host_matches_whitelist(host_lower, allowed):
            return f"主机不在允许列表中：{host_lower}。默认仅允许图床域名，可在 URL 上传白名单中追加"

        if not self.verify_ssl:
            return None

        try:
            ip = ipaddress.ip_address(host)
            if self._is_blocked_ip(ip):
                return "目标地址是内网或保留地址，已拒绝"
            return None
        except ValueError:
            pass

        if not resolve_dns:
            return None

        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            addrinfos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            return "无法解析主机名"
        if not addrinfos:
            return "无法解析主机名"

        for info in addrinfos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except (IndexError, TypeError, ValueError):
                continue
            if self._is_blocked_ip(ip):
                return "解析到内网或保留地址，已拒绝（开启 SSL 时禁止 DNS 重绑定）"
        return None

    async def _resolve_download_addresses(
        self, parsed, *, timeout_sec: float
    ) -> tuple[list[str] | None, str | None]:
        host = parsed.hostname or ""
        try:
            ip = ipaddress.ip_address(host)
            if self._is_blocked_ip(ip):
                return None, "目标地址是内网或保留地址，已拒绝"
            return [host], None
        except ValueError:
            pass

        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        dns_timeout = min(max(float(timeout_sec), 1.0), 5.0)
        try:
            addrinfos = await asyncio.wait_for(
                asyncio.to_thread(
                    socket.getaddrinfo, host, port, type=socket.SOCK_STREAM
                ),
                timeout=dns_timeout,
            )
        except asyncio.TimeoutError:
            return None, "DNS 解析超时"
        except OSError:
            return None, "无法解析主机名"

        addresses: list[str] = []
        for info in addrinfos or []:
            try:
                address = info[4][0]
                ip = ipaddress.ip_address(address)
            except (IndexError, TypeError, ValueError):
                continue
            if self._is_blocked_ip(ip):
                return (
                    None,
                    "解析到内网或保留地址，已拒绝（开启 SSL 时禁止 DNS 重绑定）",
                )
            if address not in addresses:
                addresses.append(address)
        if not addresses:
            return None, "无法解析主机名"
        return addresses, None

    @staticmethod
    def _ext_from_content_type(content_type: str | None) -> str | None:
        if not content_type:
            return None
        mapping = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/bmp": ".bmp",
            "image/webp": ".webp",
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "video/quicktime": ".mov",
            "video/x-matroska": ".mkv",
            "video/x-msvideo": ".avi",
            "video/x-ms-wmv": ".wmv",
            "video/x-flv": ".flv",
            "video/x-m4v": ".m4v",
        }
        return mapping.get(content_type.split(";", 1)[0].strip().lower())

    async def download_url_for_upload(
        self,
        url: str,
        *,
        max_bytes: int = 20 * 1024 * 1024,
        timeout_sec: float = 30.0,
        max_redirects: int = 5,
    ) -> tuple[bytes | None, str | None, str | None, str | None]:
        """Safely download an URL for the LLM upload tool."""
        current = (url or "").strip()
        err = self.validate_download_url(current, resolve_dns=False)
        if err:
            return None, None, None, err

        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        try:
            for _ in range(max_redirects + 1):
                err = self.validate_download_url(current, resolve_dns=False)
                if err:
                    return None, None, None, err

                parsed, parse_err = self._parse_download_url(current)
                if parse_err or parsed is None:
                    return None, None, None, parse_err or "url 无效"

                resolver = None
                if self.verify_ssl:
                    addresses, resolve_err = await self._resolve_download_addresses(
                        parsed, timeout_sec=timeout_sec
                    )
                    if resolve_err:
                        return None, None, None, resolve_err
                    resolver = _PinnedResolver(addresses or [])

                async with aiohttp.ClientSession(
                    connector=self._build_connector(resolver), timeout=timeout
                ) as session:
                    async with session.get(current, allow_redirects=False) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            location = response.headers.get("Location")
                            if not location:
                                return None, None, None, "下载失败：重定向缺少 Location"
                            current = urljoin(str(response.url), location)
                            continue
                        if response.status != 200:
                            return None, None, None, f"下载失败：HTTP {response.status}"

                        content_type = (
                            response.headers.get("Content-Type") or ""
                        ).strip() or None
                        content_length = response.headers.get("Content-Length")
                        if content_length:
                            try:
                                if int(content_length) > max_bytes:
                                    return (
                                        None,
                                        None,
                                        None,
                                        f"文件过大，超过 {max_bytes} 字节限制",
                                    )
                            except ValueError:
                                pass

                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > max_bytes:
                                return (
                                    None,
                                    None,
                                    None,
                                    f"文件过大，超过 {max_bytes} 字节限制",
                                )
                            chunks.append(chunk)

                        data = b"".join(chunks)
                        if not data:
                            return None, None, None, "下载失败：响应体为空"

                        url_name = self._guess_filename_from_url(str(response.url), "")
                        url_ext = (
                            os.path.splitext(url_name)[1].lower() if url_name else ""
                        )
                        known_exts = {
                            ".jpg",
                            ".jpeg",
                            ".png",
                            ".gif",
                            ".bmp",
                            ".webp",
                            ".mp4",
                            ".webm",
                            ".mov",
                            ".mkv",
                            ".avi",
                            ".wmv",
                            ".flv",
                            ".m4v",
                        }
                        ext = url_ext if url_ext in known_exts else None
                        ext = ext or self._ext_from_content_type(content_type) or ".bin"
                        base = (
                            os.path.splitext(os.path.basename(url_name))[0]
                            if url_name
                            else "upload"
                        )
                        filename = f"{base or 'upload'}{ext}"
                        return data, content_type, filename, None

            return None, None, None, "下载失败：重定向次数过多"
        except asyncio.TimeoutError:
            return None, None, None, "下载超时"
        except Exception as e:
            logger.error(
                f"安全下载失败: url={self._redact_url_for_log(url)}, "
                f"err={type(e).__name__}"
            )
            return None, None, None, "下载失败，请检查 URL 与网络"

    async def wait_user_confirm(
        self, event: AstrMessageEvent, *, timeout: int = 60
    ) -> tuple[bool, str]:
        """Wait for a confirmation message in the current session."""
        state = {"confirmed": False, "cancelled": False}
        if not event.is_admin():
            return False, "仅管理员可用"
        sender_id = event.get_sender_id()
        if not sender_id:
            return False, "无法确认操作者身份，已取消操作。"

        @session_waiter(timeout=timeout, record_history_chains=False)
        async def _confirm_waiter(
            controller: SessionController, confirm_event: AstrMessageEvent
        ):
            if (
                not confirm_event.is_admin()
                or confirm_event.get_sender_id() != sender_id
            ):
                return

            text = (getattr(confirm_event, "message_str", None) or "").strip()
            if not text:
                for seg in confirm_event.get_messages():
                    if isinstance(seg, Plain) and getattr(seg, "text", None):
                        text = str(seg.text).strip()
                        break

            lower = text.lower()
            if text in {"确认", "是"} or lower in {"confirm", "y", "yes"}:
                state["confirmed"] = True
                controller.stop()
                return
            if text in {"取消", "否"} or lower in {"cancel", "n", "no"}:
                state["cancelled"] = True
                controller.stop()
                return
            await confirm_event.send(
                confirm_event.plain_result("请回复「确认」继续，或「取消」中止。")
            )

        try:
            await _confirm_waiter(event, session_filter=_SenderSessionFilter())
        except TimeoutError:
            return False, "确认超时，已取消操作。"
        except Exception as e:
            logger.error(f"会话确认失败: {e}")
            return False, f"确认过程出错: {e}"

        if state["confirmed"]:
            return True, ""
        if state["cancelled"]:
            return False, "已取消操作。"
        return False, "已取消操作。"

    # ==================== 核心功能方法 ====================

    async def initialize(self):
        await self._load_keyword_recent_media_ids()

    async def _load_keyword_recent_media_ids(self):
        self.keyword_recent_media_ids = {}
        if self.keyword_recent_media_limit <= 0:
            return

        try:
            stored = await self.get_kv_data(self.keyword_recent_media_kv_key, {})
        except Exception as e:
            logger.error(f"加载近期媒体去重缓存失败: {e}")
            return

        if not isinstance(stored, dict):
            return

        valid_keywords = set(self.effective_keyword_map.keys())
        valid_keywords.add("__img__")
        needs_persist = False

        for keyword, history in stored.items():
            if not isinstance(keyword, str) or not isinstance(history, list):
                needs_persist = True
                continue

            if keyword not in valid_keywords:
                needs_persist = True
                continue

            normalized_history = [
                str(item).strip()
                for item in history
                if isinstance(item, str) and str(item).strip()
            ]
            if self.keyword_recent_media_limit > 0:
                trimmed_history = normalized_history[-self.keyword_recent_media_limit :]
                if trimmed_history != normalized_history:
                    needs_persist = True
                normalized_history = trimmed_history
            if normalized_history:
                self.keyword_recent_media_ids[keyword] = normalized_history
            elif history:
                needs_persist = True

        if needs_persist:
            await self._persist_keyword_recent_media_ids()

    async def _persist_keyword_recent_media_ids(self):
        try:
            if self.keyword_recent_media_limit <= 0:
                await self.delete_kv_data(self.keyword_recent_media_kv_key)
            else:
                await self.put_kv_data(
                    self.keyword_recent_media_kv_key,
                    self.keyword_recent_media_ids,
                )
        except Exception as e:
            logger.error(f"保存近期媒体去重缓存失败: {e}")

    async def _remember_keyword_media_id(self, keyword: str, media_id: str):
        if self.keyword_recent_media_limit <= 0:
            return

        media_id = (media_id or "").strip()
        if not media_id:
            return

        history = self.keyword_recent_media_ids.get(keyword, [])
        history.append(media_id)
        if len(history) > self.keyword_recent_media_limit:
            history = history[-self.keyword_recent_media_limit :]

        self.keyword_recent_media_ids[keyword] = history
        await self._persist_keyword_recent_media_ids()

    def _build_random_media_chain(self, file_url: str) -> list:
        parsed_path = urlparse(file_url).path.lower()
        video_exts = (
            ".mp4",
            ".avi",
            ".mov",
            ".mkv",
            ".wmv",
            ".flv",
            ".webm",
            ".m4v",
        )
        if any(parsed_path.endswith(ext) for ext in video_exts):
            return [Video.fromURL(file_url)]
        return [Image.fromURL(file_url)]

    def _build_list_media_chain(self, file_url: str, file_item: object) -> list:
        metadata = file_item.get("metadata", {}) if isinstance(file_item, dict) else {}
        mime_type = str(
            metadata.get("File-Mime") or metadata.get("FileType") or ""
        ).lower()
        if mime_type.startswith("video/"):
            return [Video.fromURL(file_url)]
        if mime_type.startswith("image/"):
            return [Image.fromURL(file_url)]
        return self._build_random_media_chain(file_url)

    async def _fetch_tagged_random_media_entry(
        self,
        folder_name: str,
        content_type: str,
        include_tags: list[str],
    ):
        """Use the management list API to randomly select a tagged media file."""
        cache_key = self._build_tag_random_cache_key(
            folder_name, content_type, include_tags
        )
        cached_total = self.tag_random_count_cache.get(cache_key)

        for _ in range(2):
            if cached_total is None:
                count_result = await self.list_files(
                    dir_path=folder_name,
                    count=1,
                    recursive=True,
                    file_type=content_type,
                    include_tags=include_tags,
                    sum_only=True,
                )
                if isinstance(count_result, str):
                    return count_result

                total = count_result.get("sum", count_result.get("totalCount", 0))
                try:
                    total = int(total)
                except (TypeError, ValueError):
                    return "请求失败：图床返回的标签媒体数量无效。"

                if total <= 0:
                    tag_text = ", ".join(include_tags)
                    folder_text = f"目录「{folder_name}」" if folder_name else "图床"
                    return f"{folder_text}中未找到包含标签「{tag_text}」的媒体。"

                self.tag_random_count_cache[cache_key] = total
                cached_total = total
            else:
                logger.debug(
                    f"使用标签随机数量缓存：folder={folder_name or '<root>'}, "
                    f"tags={include_tags}, total={cached_total}"
                )

            result = await self.list_files(
                dir_path=folder_name,
                start=random.randrange(cached_total),
                count=1,
                recursive=True,
                file_type=content_type,
                include_tags=include_tags,
            )
            if isinstance(result, str):
                return result

            # The ImgBed fallback response does not reapply tag filters. Do not
            # select from it, otherwise a tagged request could return any file.
            if result.get("isIndexedResponse") is False:
                return "标签随机获取失败：图床索引未生效，请先重建图床索引。"

            response_total = result.get("totalCount")
            try:
                response_total = int(response_total)
            except (TypeError, ValueError):
                response_total = None
            if response_total is not None:
                if response_total > 0:
                    self.tag_random_count_cache[cache_key] = response_total
                    cached_total = response_total
                else:
                    self.tag_random_count_cache.pop(cache_key, None)
                    cached_total = None

            files = result.get("files") or []
            if not isinstance(files, list) or not files:
                self.tag_random_count_cache.pop(cache_key, None)
                cached_total = None
                logger.debug("标签随机结果在请求期间发生变化，将重试一次")
                continue

            file_item = files[0]
            if isinstance(file_item, dict):
                relative_file_path = str(
                    file_item.get("name") or file_item.get("src") or ""
                ).strip()
            else:
                relative_file_path = str(file_item or "").strip()
            if not relative_file_path:
                return "请求失败：图床列表未返回媒体路径。"

            file_url = self.resolve_media_display_url(relative_file_path)
            if not file_url:
                return "请求失败：无法解析图床媒体 URL，请检查 base_url 或 public_base_url。"

            return {
                "chain": self._build_list_media_chain(file_url, file_item),
                "media_id": self._extract_media_id(relative_file_path),
                "file_url": file_url,
                "relative_file_path": relative_file_path,
            }

        return "请求失败：标签随机结果在请求期间发生变化，请稍后重试。"

    async def _fetch_random_media_entry(
        self,
        folder_name: str = "",
        content_type: str = "image,video",
        include_tags: object = None,
    ):
        if not self.base_url:
            return "请先配置 base_url。"

        normalized_tags = self._normalize_tag_names(include_tags)

        final_content_type = content_type
        if self.local_random_type and "," in final_content_type:
            types = [t.strip() for t in final_content_type.split(",") if t.strip()]
            if len(types) > 1:
                final_content_type = random.choice(types)
                logger.debug(f"本地随机媒体类型：{final_content_type}")

        if normalized_tags:
            return await self._fetch_tagged_random_media_entry(
                folder_name=folder_name,
                content_type=final_content_type,
                include_tags=normalized_tags,
            )

        params = {
            "form": "text",
            "content": final_content_type,
        }
        if folder_name:
            params["dir"] = folder_name

        try:
            async with aiohttp.ClientSession(
                connector=self._build_connector()
            ) as session:
                async with session.get(
                    f"{self.base_url}/random", params=params
                ) as response:
                    if response.status != 200:
                        response_text = await response.text()
                        return self._handle_response_error(
                            response.status, response_text
                        )

                    relative_file_path = (await response.text()).strip()
                    if not relative_file_path:
                        logger.error("Random API returned an empty path")
                        return "请求失败：图床未返回媒体路径。"

                    file_url = f"{self.base_url}{relative_file_path}"
                    return {
                        "chain": self._build_random_media_chain(file_url),
                        "media_id": self._extract_media_id(relative_file_path),
                        "file_url": file_url,
                        "relative_file_path": relative_file_path,
                    }
        except Exception as e:
            logger.error(f"请求随机媒体失败: {e}")
            return "请求失败，请检查网络、base_url 与文件夹名。"

    async def get_random_file_from_keyword(
        self,
        keyword: str,
        folder_name: str = "",
        content_type: str = "image,video",
        include_tags: object = None,
    ):
        if self.keyword_recent_media_limit <= 0:
            result = await self._fetch_random_media_entry(
                folder_name, content_type, include_tags
            )
            if isinstance(result, dict):
                return result["chain"]
            return result

        history = self.keyword_recent_media_ids.get(keyword, [])
        dedupe_enabled = len(history) > 0
        retry_limit = self.keyword_dedupe_retry_limit if dedupe_enabled else 0

        best_fallback = None
        best_distance = -1

        # 首次 fetch
        result = await self._fetch_random_media_entry(
            folder_name, content_type, include_tags
        )
        if not isinstance(result, dict):
            return result

        media_id = result.get("media_id", "")
        if not dedupe_enabled or not media_id or media_id not in history:
            await self._remember_keyword_media_id(keyword, media_id)
            return result["chain"]

        distance = self._history_distance(history, media_id)
        if distance > best_distance:
            best_distance = distance
            best_fallback = result

        # 显式重试循环
        for retry_count in range(1, retry_limit + 1):
            logger.debug(
                f"/{keyword} 命中近期媒体 ID，重试 {retry_count}/{retry_limit}"
            )
            result = await self._fetch_random_media_entry(
                folder_name, content_type, include_tags
            )
            if not isinstance(result, dict):
                return result

            media_id = result.get("media_id", "")
            if not media_id or media_id not in history:
                await self._remember_keyword_media_id(keyword, media_id)
                return result["chain"]

            distance = self._history_distance(history, media_id)
            if distance > best_distance:
                best_distance = distance
                best_fallback = result

        selected = best_fallback or result
        if isinstance(selected, dict):
            selected_id = selected.get("media_id", "")
            await self._remember_keyword_media_id(keyword, selected_id)
            logger.debug(
                f"/{keyword} 达到重试上限，回退媒体 ID：{selected_id or '<empty>'}"
            )
            return selected["chain"]

        return "请求失败：未获取到可用媒体。"

    async def get_random_file_from_folder(
        self,
        folder_name: str = "",
        content_type: str = "image,video",
        include_tags: object = None,
    ):
        """Get one random image/video from the target folder."""
        result = await self._fetch_random_media_entry(
            folder_name, content_type, include_tags
        )
        if isinstance(result, dict):
            return result["chain"]
        return result

    async def _resolve_media_bytes(
        self,
        event: BaseAstrMessageEvent,
        kind: str,
        file_or_id: str | None,
        url: str | None,
        path: str | None,
    ) -> tuple[bytes | None, str | None]:
        """跨平台解析媒体字节数据。

        优先级：
        1) http(s) 直链下载（保持原有行为，不校验 SSL）
        2) 框架 MediaResolver：本地路径 / file:// / base64 / data URI
        3) Telegram 文件路径：拼接待下载 URL（client.base_url + /file/）
        4) OneBot file_id 兜底：call_action("get_file")

        Returns:
            (字节数据, 错误信息)。
        """
        source = (
            (url or "").strip() or (path or "").strip() or (file_or_id or "").strip()
        )
        if not source:
            return None, "无法获取媒体文件数据"

        # 1) http(s) 直链
        if source.startswith(("http://", "https://")):
            data = await self.download_image(source)
            if data:
                return data, None

        # 2) 框架 MediaResolver
        try:
            media_type = "video" if kind == "video" else "image"
            data = await MediaResolver(source, media_type=media_type).to_bytes()
            if data:
                return data, None
        except Exception as e:
            logger.debug(f"媒体解析失败(MediaResolver): kind={kind}, err={e}")

        # 3) Telegram 文件路径下载
        data = await self._telegram_download_file(event, source)
        if data:
            return data, None

        # 4) OneBot file_id 兜底
        data = await self._onebot_get_file(event, file_or_id or "")
        if data:
            return data, None

        return None, "无法获取媒体文件数据"

    async def upload_to_cloudflare_imgbed(
        self, image_data: bytes, folder_name: str, original_filename: str = None
    ) -> str | None:
        """上传文件到CloudFlare ImgBed"""
        if not self.upload_api_url:
            return "上传API地址未配置"

        upload_url = f"{self.upload_api_url}/upload"

        file_ext = ".jpg"
        content_type = "image/jpeg"

        ext_map = {
            ".jpg": ("image", "jpeg"),
            ".jpeg": ("image", "jpeg"),
            ".png": ("image", "png"),
            ".gif": ("image", "gif"),
            ".bmp": ("image", "bmp"),
            ".webp": ("image", "webp"),
            ".mp4": ("video", "mp4"),
            ".webm": ("video", "webm"),
            ".mov": ("video", "quicktime"),
            ".mkv": ("video", "x-matroska"),
            ".avi": ("video", "x-msvideo"),
            ".wmv": ("video", "x-ms-wmv"),
            ".flv": ("video", "x-flv"),
            ".m4v": ("video", "x-m4v"),
        }

        if original_filename:
            ext = os.path.splitext(original_filename)[1].lower()
            if ext in ext_map:
                major, minor = ext_map[ext]
                file_ext = ext
                content_type = f"{major}/{minor}"

        # 准备表单数据
        filename = f"upload{file_ext}"
        data = aiohttp.FormData()
        data.add_field("file", image_data, filename=filename, content_type=content_type)

        headers = {}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"

        params = {}
        if not self.api_token and self.auth_code:
            params["authCode"] = self.auth_code
        params["serverCompress"] = "false"  # 禁用压缩
        params["uploadFolder"] = folder_name
        params["returnFormat"] = "full"  # 使用完整格式

        try:
            async with aiohttp.ClientSession(
                connector=self._build_connector()
            ) as session:
                async with session.post(
                    upload_url, data=data, params=params, headers=headers
                ) as response:
                    response_text = await response.text()

                    if response.status != 200:
                        return self._handle_response_error(
                            response.status, response_text
                        )

                    try:
                        response_json = json.loads(
                            response_text
                        )  # Use json.loads since we already have response_text

                        if isinstance(response_json, list) and len(response_json) > 0:
                            src_path = response_json[0].get("src", "")
                            if src_path:
                                return self._build_upload_display_url(src_path)
                            else:
                                logger.error(
                                    f"上传成功但未找到链接，响应: {response_text}"
                                )
                                return "上传成功但未找到链接"
                        elif (
                            "data" in response_json
                            and isinstance(response_json["data"], list)
                            and len(response_json["data"]) > 0
                        ):
                            src_path = response_json["data"][0].get("src", "")
                            if src_path:
                                return self._build_upload_display_url(src_path)
                            else:
                                logger.error(
                                    f"上传成功但未找到链接，响应: {response_text}"
                                )
                                return "上传成功但未找到链接"
                        else:
                            logger.error(f"上传响应格式错误，响应: {response_text}")
                            return "上传响应格式错误"
                    except json.JSONDecodeError:
                        logger.error(
                            f"上传响应不是有效的JSON格式，响应: {response_text}"
                        )
                        return "上传响应不是有效的JSON格式"
        except Exception as e:
            logger.error(f"文件上传失败: err={type(e).__name__}")
            return "文件上传失败"

    async def _list_image_refs_from_event(
        self, event: BaseAstrMessageEvent
    ) -> list[dict]:
        messages = event.get_messages()

        reply_refs: list[dict] = []
        for seg in messages:
            if (
                isinstance(seg, ApiReply)
                and hasattr(seg, "chain")
                and isinstance(seg.chain, list)
            ):
                reply_refs.extend(self._collect_media_refs(seg.chain))

        if reply_refs:
            return reply_refs

        current_refs = self._collect_media_refs(messages)
        if current_refs:
            return current_refs

        # Telegram 兜底：从原始 Update 的 reply_to_message 提取媒体
        # （引用带说明文字的图片时，回复链会被替换为纯文本，无法直接看到图片）
        telegram_refs = await self._list_telegram_raw_reply_refs(event)
        if telegram_refs:
            return telegram_refs

        return []

    async def _read_media_bytes(
        self, event: AstrMessageEvent, media_ref: dict
    ) -> tuple[bytes | None, str | None, str | None]:
        url = media_ref.get("url")
        file_or_id = media_ref.get("file")
        path = media_ref.get("path")
        filename = media_ref.get("filename")
        kind = media_ref.get("kind")

        data, read_err = await self._resolve_media_bytes(
            event, kind, file_or_id, url, path
        )
        if data:
            return data, filename, None
        return None, filename, read_err or "无法获取媒体文件数据"

    # ==================== 列表 / 统计 / 删除 API ====================

    async def list_files(
        self,
        dir_path: str = "",
        start: int = 0,
        count: int | None = None,
        *,
        recursive: bool = False,
        search: str = "",
        file_type: str = "",
        include_tags: object = None,
        sum_only: bool = False,
    ):
        """调用 GET /api/manage/list。成功返回 dict，失败返回错误字符串。"""
        page_size = self.list_page_size if count is None else count
        params: dict = {
            "start": start,
            "count": -1 if sum_only else page_size,
        }
        if sum_only:
            params["sum"] = "true"
        if dir_path:
            params["dir"] = dir_path
        if recursive:
            params["recursive"] = "true"
        if search:
            params["search"] = search
        if file_type:
            params["fileType"] = file_type
        normalized_tags = self._normalize_tag_names(include_tags)
        if normalized_tags:
            params["includeTags"] = ",".join(normalized_tags)

        result = await self._api_request(
            "GET", "/api/manage/list", params=params, require_token=True
        )
        if isinstance(result, str):
            return result
        if not isinstance(result, dict):
            return "操作失败: 列表响应格式异常"
        return result

    async def delete_path(self, path: str, *, is_folder: bool = False):
        """调用 GET /api/manage/delete/{path}。成功返回 dict，失败返回错误字符串。"""
        normalized = (path or "").strip().lstrip("/")
        if not normalized:
            return "请指定要删除的路径。"

        if "\\" in normalized:
            return "路径不合法：不允许使用反斜杠。"
        decoded = unquote(normalized)
        if any(part in {"", ".", ".."} for part in decoded.split("/")):
            return "路径不合法：不允许使用路径遍历或空路径段。"

        encoded = quote(normalized, safe="/")
        params = {"folder": "true"} if is_folder else None
        result = await self._api_request(
            "GET",
            f"/api/manage/delete/{encoded}",
            params=params,
            require_token=True,
        )
        if isinstance(result, str):
            return result
        if not isinstance(result, dict):
            return "操作失败: 删除响应格式异常"
        return result

    def _format_list_reply(
        self,
        data: dict,
        dir_path: str,
        page: int,
        page_size: int,
        file_type: str = "",
    ) -> str:
        display_dir = dir_path.strip() or "/"
        directories = data.get("directories") or []
        files = data.get("files") or []
        total = data.get("totalCount")
        if total is None:
            total = data.get("returnedCount", len(files))

        lines = [f"📁 目录: {display_dir}"]
        if file_type:
            lines.append(f"类型筛选: {file_type}")

        if directories:
            dir_names = []
            for d in directories:
                name = str(d).strip().rstrip("/")
                dir_names.append(name.split("/")[-1] if name else str(d))
            lines.append(f"子目录: {', '.join(dir_names)}")
        else:
            lines.append("子目录: （无）")

        returned = len(files) if isinstance(files, list) else 0
        lines.append(f"文件 (第 {page} 页，共 {total} 个，本页 {returned} 个):")
        if not files:
            lines.append("（本页无文件）")
        else:
            for i, item in enumerate(files, start=1):
                if isinstance(item, dict):
                    name = item.get("name") or item.get("src") or str(item)
                else:
                    name = str(item)
                lines.append(f"{i}. {name}")

        try:
            total_int = int(total)
        except (TypeError, ValueError):
            total_int = returned
        total_pages = (
            max(1, (total_int + page_size - 1) // page_size) if page_size > 0 else 1
        )
        if page < total_pages:
            if dir_path:
                next_cmd = f"/imglist {dir_path} {page + 1}"
            else:
                next_cmd = f"/imglist {page + 1}"
            if file_type == "image":
                next_cmd = f"{next_cmd} img"
            elif file_type == "video":
                next_cmd = f"{next_cmd} vid"
            lines.append(f"下一页: {next_cmd}")

        return "\n".join(lines)

    def _extract_plain_text(self, event: AstrMessageEvent) -> str:
        """提取消息纯文本（优先 Plain 段）。"""
        for seg in event.get_messages():
            if isinstance(seg, Plain) and getattr(seg, "text", None):
                return str(seg.text).strip()
        message_str = getattr(event, "message_str", None)
        if isinstance(message_str, str) and message_str.strip():
            return message_str.strip()
        get_msg = getattr(event, "get_message_str", None)
        if callable(get_msg):
            try:
                value = get_msg()
                if isinstance(value, str):
                    return value.strip()
            except Exception:
                pass
        return ""

    def _strip_command_prefix(self, text: str, command_names: set[str]) -> str:
        """去掉唤醒前缀与命令名，返回剩余参数字符串。"""
        message_text = (text or "").strip()
        if not message_text:
            return ""

        try:
            cfg = self.context.get_config()
        except Exception:
            cfg = {}
        wake_prefixes = cfg.get("wake_prefix", []) if isinstance(cfg, dict) else []
        if isinstance(wake_prefixes, str):
            wake_prefixes = [wake_prefixes]
        for prefix in wake_prefixes:
            if isinstance(prefix, str) and prefix and message_text.startswith(prefix):
                message_text = message_text[len(prefix) :].strip()
                break

        if message_text.startswith("/"):
            message_text = message_text[1:].strip()

        parts = message_text.split()
        if not parts:
            return ""

        first = parts[0].lower()
        names = {n.lower() for n in command_names}
        if first in names:
            return " ".join(parts[1:]).strip()
        return " ".join(parts[1:]).strip() if len(parts) > 1 else ""

    def _parse_imglist_args(self, args: list[str]) -> tuple[str, int, str]:
        """解析 /imglist 参数 -> (dir, page, file_type)。"""
        dir_path = ""
        page = 1
        file_type = ""

        type_aliases = {
            "img": "image",
            "image": "image",
            "i": "image",
            "vid": "video",
            "video": "video",
            "v": "video",
        }

        tokens = [a.strip() for a in args if a and str(a).strip()]
        if not tokens:
            return dir_path, page, file_type

        if len(tokens) >= 2 and tokens[-1].lower() in type_aliases:
            file_type = type_aliases[tokens[-1].lower()]
            tokens = tokens[:-1]

        if not tokens:
            return dir_path, page, file_type

        if len(tokens) == 1 and re.fullmatch(r"\d+", tokens[0]):
            page = max(1, int(tokens[0]))
            return dir_path, page, file_type

        if len(tokens) >= 2 and re.fullmatch(r"\d+", tokens[-1]):
            page = max(1, int(tokens[-1]))
            dir_path = tokens[0] if len(tokens) == 2 else " ".join(tokens[:-1])
            return dir_path, page, file_type

        dir_path = tokens[0] if len(tokens) == 1 else " ".join(tokens)
        return dir_path, page, file_type

    # ==================== 命令处理方法 ====================

    @filter.command("img")
    async def get_image(self, event: AstrMessageEvent):
        """获取随机图片或视频"""
        result = await self.get_random_file_from_keyword("__img__", "", "image,video")
        if isinstance(result, list):
            yield event.chain_result(result)
        else:
            yield event.plain_result(result)

    @filter.command("imglist", alias={"列表"})
    async def cmd_imglist(self, event: AstrMessageEvent):
        """列出图床目录文件：/imglist [目录] [页码] [img|vid]"""
        if not event.is_admin():
            yield event.plain_result("此指令仅限管理员使用")
            return

        arg_text = self._strip_command_prefix(
            self._extract_plain_text(event),
            {"imglist", "列表"},
        )
        args = [p for p in arg_text.split() if p]
        dir_path, page, file_type = self._parse_imglist_args(args)
        page_size = self.list_page_size
        start = (page - 1) * page_size

        result = await self.list_files(
            dir_path=dir_path,
            start=start,
            count=page_size,
            file_type=file_type,
        )
        if isinstance(result, str):
            yield event.plain_result(result)
            return

        yield event.plain_result(
            self._format_list_reply(result, dir_path, page, page_size, file_type)
        )

    @filter.command("imgstat", alias={"统计"})
    async def cmd_imgstat(self, event: AstrMessageEvent, folder_name: str = None):
        """统计目录文件数：/imgstat [目录]"""
        if not event.is_admin():
            yield event.plain_result("此指令仅限管理员使用")
            return

        dir_path = (folder_name or "").strip()
        if not dir_path:
            arg_text = self._strip_command_prefix(
                self._extract_plain_text(event),
                {"imgstat", "统计"},
            )
            dir_path = arg_text.strip()

        result = await self.list_files(dir_path=dir_path, sum_only=True)
        if isinstance(result, str):
            yield event.plain_result(result)
            return

        total = result.get("sum")
        if total is None:
            total = result.get("totalCount", "未知")
        display_dir = dir_path or "/"
        yield event.plain_result(f"📊 目录: {display_dir}\n文件总数: {total}")

    @filter.command("imgdel", alias={"删除"})
    async def cmd_imgdel(self, event: AstrMessageEvent):
        """删除单个文件：/imgdel <文件路径>"""
        if not event.is_admin():
            yield event.plain_result("此指令仅限管理员使用")
            return

        path = self._strip_command_prefix(
            self._extract_plain_text(event),
            {"imgdel", "删除"},
        )
        if not path:
            yield event.plain_result(
                "参数错误！格式：/imgdel <文件路径>\n例如：/imgdel example/image.jpg"
            )
            return

        result = await self.delete_path(path, is_folder=False)
        if isinstance(result, str):
            yield event.plain_result(result)
            return

        if result.get("success") is False:
            err = result.get("error") or result.get("message") or "删除失败"
            yield event.plain_result(f"删除失败: {err}")
            return

        file_id = result.get("fileId") or path
        yield event.plain_result(f"已删除文件: {file_id}")

    @filter.command("imgdelfolder", alias={"删文件夹"})
    async def cmd_imgdelfolder(self, event: AstrMessageEvent):
        """递归删除文件夹：/imgdelfolder <目录>（会话二次确认）"""
        if not event.is_admin():
            yield event.plain_result("此指令仅限管理员使用")
            return

        arg_text = self._strip_command_prefix(
            self._extract_plain_text(event),
            {"imgdelfolder", "删文件夹"},
        )
        path = arg_text.strip()
        if not path:
            yield event.plain_result(
                "参数错误！格式：/imgdelfolder <目录>\n"
                "将提示二次确认后递归删除目录及其全部内容。"
            )
            return

        yield event.plain_result(
            f"危险操作：将递归删除目录「{path}」及其全部内容。\n"
            f"请回复「确认」继续，或「取消」中止。（60 秒内有效）"
        )
        confirmed, msg = await self.wait_user_confirm(event, timeout=60)
        if not confirmed:
            yield event.plain_result(msg or "已取消操作。")
            event.stop_event()
            return

        result = await self.delete_path(path, is_folder=True)
        if isinstance(result, str):
            yield event.plain_result(result)
            event.stop_event()
            return

        if result.get("success") is False:
            err = result.get("error") or result.get("message") or "删除失败"
            yield event.plain_result(f"删除文件夹失败: {err}")
            event.stop_event()
            return

        deleted = result.get("deleted") or []
        failed = result.get("failed") or []
        lines = [
            f"已删除文件夹: {path}",
            f"成功: {len(deleted)} 个文件",
        ]
        if failed:
            lines.append(f"失败: {len(failed)} 个")
            for item in failed[:10]:
                lines.append(f"- {item}")
            if len(failed) > 10:
                lines.append(f"... 另有 {len(failed) - 10} 个失败项")
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    @filter.command("上传", alias={"upload"})
    async def upload_image(
        self, event: AstrMessageEvent, folder_name: str = None, index_spec: str = None
    ):
        """上传媒体到CloudFlare ImgBed"""
        logger.info(f"/上传: folder={folder_name}, index_spec={index_spec}")
        if self.upload_admin_only:
            if not event.is_admin():
                yield event.plain_result("上传功能仅限管理员使用")
                return

        if not folder_name:
            yield event.plain_result("请指定上传的文件夹名，格式：/上传 文件夹名")
            return

        # 检测文件夹名是否包含英文标点
        if any(char in string.punctuation for char in folder_name):
            yield event.plain_result(f"文件夹名 {folder_name} 不允许包含英文标点")
            return

        forward_id = None
        found_json_forward = False
        if event.get_platform_name() == "aiocqhttp":
            forward_id, found_json_forward = await self._try_get_forward_id(event)
        if forward_id:
            media_refs = await self._list_media_refs_from_forward(event, forward_id)
            if not media_refs:
                yield event.plain_result("合并聊天记录中未找到可上传的图片/视频")
                return

            indexes, err = self._parse_index_spec(
                index_spec,
                len(media_refs),
                label="媒体文件",
                empty_msg="合并聊天记录中未找到图片/视频",
            )
            if err:
                yield event.plain_result(err)
                return

            logger.info(
                f"合并聊天记录上传开始: folder={folder_name}, total={len(media_refs)}, selected={len(indexes)}"
            )

            semaphore = asyncio.Semaphore(3)

            async def upload_one(i: int):
                ref = media_refs[i - 1]
                async with semaphore:
                    data, filename, read_err = await self._read_media_bytes(event, ref)
                    if read_err:
                        logger.warning(
                            f"合并聊天记录媒体读取失败: index={i}, err={read_err}"
                        )
                        return {
                            "index": i,
                            "ok": False,
                            "error": read_err,
                            "filename": filename,
                            "kind": ref.get("kind"),
                        }
                    result = await self.upload_to_cloudflare_imgbed(
                        data, folder_name, filename
                    )
                    if isinstance(result, str) and result.startswith("http"):
                        return {
                            "index": i,
                            "ok": True,
                            "url": result,
                            "filename": filename,
                            "kind": ref.get("kind"),
                        }
                    err_msg = result or "上传失败"
                    logger.warning(
                        f"合并聊天记录媒体上传失败: index={i}, err={err_msg}"
                    )
                    return {
                        "index": i,
                        "ok": False,
                        "error": err_msg,
                        "filename": filename,
                        "kind": ref.get("kind"),
                    }

            results = await asyncio.gather(*(upload_one(i) for i in indexes))

            ok_results = [r for r in results if r.get("ok")]
            fail_results = [r for r in results if not r.get("ok")]

            logger.info(
                f"合并聊天记录上传结束: folder={folder_name}, success={len(ok_results)}, fail={len(fail_results)}"
            )

            yield event.plain_result(self._build_upload_reply("上传完成", results))
            return

        if found_json_forward:
            yield event.plain_result(
                "检测到合并聊天记录（JSON 格式），当前无法提取其中的图片/视频，请发送可解析的合并转发消息"
            )
            return

        media_refs = await self._list_image_refs_from_event(event)
        if media_refs:
            indexes, err = self._parse_index_spec(
                index_spec,
                len(media_refs),
                label="媒体文件",
                empty_msg="未找到可上传的图片/视频",
            )
            if err:
                yield event.plain_result(err)
                return

            logger.info(
                f"媒体上传开始: folder={folder_name}, total={len(media_refs)}, selected={len(indexes)}"
            )

            semaphore = asyncio.Semaphore(3)

            async def upload_one(i: int):
                ref = media_refs[i - 1]
                async with semaphore:
                    data, filename, read_err = await self._read_media_bytes(event, ref)
                    if read_err:
                        logger.warning(f"媒体读取失败: index={i}, err={read_err}")
                        return {
                            "index": i,
                            "ok": False,
                            "error": read_err,
                            "filename": filename,
                            "kind": ref.get("kind"),
                        }
                    result = await self.upload_to_cloudflare_imgbed(
                        data, folder_name, filename
                    )
                    if isinstance(result, str) and result.startswith("http"):
                        return {
                            "index": i,
                            "ok": True,
                            "url": result,
                            "filename": filename,
                            "kind": ref.get("kind"),
                        }
                    err_msg = result or "上传失败"
                    logger.warning(f"媒体上传失败: index={i}, err={err_msg}")
                    return {
                        "index": i,
                        "ok": False,
                        "error": err_msg,
                        "filename": filename,
                        "kind": ref.get("kind"),
                    }

            results = await asyncio.gather(*(upload_one(i) for i in indexes))
            ok_results = [r for r in results if r.get("ok")]
            fail_results = [r for r in results if not r.get("ok")]

            logger.info(
                f"媒体上传结束: folder={folder_name}, success={len(ok_results)}, fail={len(fail_results)}"
            )

            yield event.plain_result(self._build_upload_reply("上传完成", results))
            return

        yield event.plain_result("未找到引用消息中的图片/视频")

    @filter.command("imglink")
    async def link_keyword_to_folder(
        self,
        event: AstrMessageEvent,
        keyword: str = None,
        folder_name: str = None,
        content_type: str = None,
    ):
        """关联关键词和文件夹"""
        if not event.is_admin():
            yield event.plain_result("此指令仅限管理员使用")
            return

        if not keyword:
            if not self.effective_keyword_map:
                yield event.plain_result("当前没有已设置的关键词映射。")
                return

            result = "当前关键词映射列表：\n"
            for key, mapping in self.effective_keyword_map.items():
                if isinstance(mapping, dict):
                    folder = mapping.get("folder", "")
                    tags = self._normalize_tag_names(mapping.get("tags", []))
                    ctype = mapping.get("content_type", "image,video")
                    source = mapping.get("source", "runtime")
                    source_text = "指令" if source == "runtime" else "模板"
                    target = folder or "根目录"
                    if tags:
                        target += f"，标签: {','.join(tags)}"
                    result += f"  /{key} -> {target} ({ctype}) [{source_text}]\n"
                else:
                    result += f"  /{key} -> {mapping}\n"
            result += (
                "\n使用 /imglink 关键词 文件夹名 [内容类型] 添加文件夹映射。"
                "\n使用 /imglinktag 关键词 标签1,标签2 [文件夹] [内容类型] 添加标签映射。"
                "\n内容类型可选: img(图片), vid(视频), 未指定则为全部"
            )
            yield event.plain_result(result.strip())
            return

        if not folder_name:
            yield event.plain_result(
                "参数错误！格式：/imglink 关键词 文件夹名 [内容类型]\n例如：/imglink test test 或 /imglink test test,test2 img\n内容类型可选: img(图片), vid(视频), 未指定则为全部\n\n不带参数使用 /imglink 可查看所有映射。"
            )
            return

        if content_type:
            if content_type.lower() in ["img", "image"]:
                final_content_type = "image"
            elif content_type.lower() in ["vid", "video"]:
                final_content_type = "video"
            else:
                yield event.plain_result(
                    "内容类型参数错误！可选值: img(图片), vid(视频)"
                )
                return
        else:
            final_content_type = "image,video"

        self.keyword_folder_map[keyword] = {
            "folder": folder_name,
            "content_type": final_content_type,
        }
        self.save_keyword_mappings()
        self.refresh_effective_keyword_map()

        content_type_desc = {
            "image": "图片",
            "video": "视频",
            "image,video": "图片或视频",
        }
        desc = content_type_desc.get(final_content_type, "图片或视频")

        yield event.plain_result(
            f"已将关键词 '{keyword}' 与文件夹 '{folder_name}' 关联（{desc}），现在发送 /{keyword} 即可获取其中随机一个文件夹的随机{desc}。"
        )

    @filter.command("imglinktag")
    async def link_keyword_to_tags(
        self,
        event: AstrMessageEvent,
        keyword: str = None,
        tags: str = None,
        folder_or_type: str = None,
        content_type: str = None,
    ):
        """关联关键词和图床标签，可选限制在指定文件夹内。"""
        if not event.is_admin():
            yield event.plain_result("此指令仅限管理员使用")
            return

        if not keyword or not tags:
            yield event.plain_result(
                "参数错误！格式：/imglinktag 关键词 标签1,标签2 [文件夹] [内容类型]\n"
                "例如：/imglinktag anime 二次元,插画\n"
                "或：/imglinktag wallpaper 风景,壁纸 wallpaper img"
            )
            return

        normalized_tags = self._normalize_tag_names(tags)
        if not normalized_tags:
            yield event.plain_result("标签不能为空。")
            return

        folder_name = folder_or_type or ""
        if content_type is None and folder_name.lower() in {
            "img",
            "image",
            "vid",
            "video",
        }:
            content_type = folder_name
            folder_name = ""

        if content_type:
            normalized_content_type = content_type.lower()
            if normalized_content_type in ["img", "image"]:
                final_content_type = "image"
            elif normalized_content_type in ["vid", "video"]:
                final_content_type = "video"
            else:
                yield event.plain_result(
                    "内容类型参数错误！可选值: img(图片), vid(视频)"
                )
                return
        else:
            final_content_type = "image,video"

        mapping_entry = self._build_mapping_entry(
            folders=folder_name,
            tags=normalized_tags,
            content_type=final_content_type,
            source="runtime",
        )
        if not mapping_entry:
            yield event.plain_result("标签映射不能为空。")
            return

        self.keyword_folder_map[keyword] = {
            "folder": mapping_entry["folder"],
            "tags": mapping_entry["tags"],
            "content_type": mapping_entry["content_type"],
        }
        self.save_keyword_mappings()
        self.refresh_effective_keyword_map()

        content_type_desc = {
            "image": "图片",
            "video": "视频",
            "image,video": "图片或视频",
        }
        desc = content_type_desc.get(final_content_type, "图片或视频")
        scope = f"文件夹 '{folder_name}' 内" if folder_name else "全图床"
        yield event.plain_result(
            f"已将关键词 '{keyword}' 关联到标签 '{','.join(normalized_tags)}'（{scope}，{desc}），"
            f"现在发送 /{keyword} 即可获取符合条件的随机{desc}。"
        )

    @filter.command("imgunlink")
    async def unlink_keyword(
        self,
        event: AstrMessageEvent,
        keyword: str = None,
        folders_to_remove: str = None,
    ):
        """取消关键词关联或删除部分文件夹"""
        if not event.is_admin():
            yield event.plain_result("此指令仅限管理员使用")
            return

        if not keyword:
            yield event.plain_result(
                "参数错误！格式：/imgunlink 关键词 [文件夹名]\n例如：/imgunlink test 或 /imgunlink test 3cy,test1"
            )
            return

        if keyword not in self.keyword_folder_map:
            if keyword in self.config_keyword_map:
                yield event.plain_result(
                    f"关键词 '{keyword}' 来自模板配置，不能通过 /imgunlink 删除，请到插件配置中修改。"
                )
            else:
                yield event.plain_result(f"关键词 '{keyword}' 不存在映射。")
            return

        if not folders_to_remove:
            # 删除整个关键词映射
            del self.keyword_folder_map[keyword]
            self.save_keyword_mappings()
            self.refresh_effective_keyword_map()
            if keyword not in self.effective_keyword_map:
                self.keyword_recent_media_ids.pop(keyword, None)
            await self._persist_keyword_recent_media_ids()
            if keyword in self.config_keyword_map:
                yield event.plain_result(
                    f"已删除关键词 '{keyword}' 的指令映射，当前将回退为模板配置。"
                )
            else:
                yield event.plain_result(f"已完全删除关键词 '{keyword}' 的所有映射。")
            return

        # 删除指定的文件夹
        mapping = self.keyword_folder_map[keyword]
        if isinstance(mapping, dict):
            current_folders_str = mapping.get("folder", "")
        else:
            current_folders_str = mapping

        current_folders = [
            f.strip()
            for f in current_folders_str.replace("，", ",").split(",")
            if f.strip()
        ]
        remove_list = [
            f.strip()
            for f in folders_to_remove.replace("，", ",").split(",")
            if f.strip()
        ]

        new_folders = []
        removed_count = 0
        not_found = []

        for f in current_folders:
            if f in remove_list:
                removed_count += 1
            else:
                new_folders.append(f)

        for f in remove_list:
            if f not in current_folders:
                not_found.append(f)

        if removed_count == 0:
            yield event.plain_result(
                f"关键词 '{keyword}' 的映射中未找到指定的文件夹: {', '.join(not_found)}"
            )
            return

        if not new_folders:
            # 如果删完了，直接删除关键词
            del self.keyword_folder_map[keyword]
            self.refresh_effective_keyword_map()
            if keyword not in self.effective_keyword_map:
                self.keyword_recent_media_ids.pop(keyword, None)
                msg = f"已删除关键词 '{keyword}' 关联的所有文件夹，该关键词已失效。"
            else:
                msg = f"已删除关键词 '{keyword}' 的指令映射，当前将回退为模板配置。"
        else:
            new_folder_str = ",".join(new_folders)
            if isinstance(mapping, dict):
                mapping["folder"] = new_folder_str
            else:
                self.keyword_folder_map[keyword] = new_folder_str
            self.refresh_effective_keyword_map()
            msg = f"已从关键词 '{keyword}' 中删除 {removed_count} 个文件夹。当前关联：{new_folder_str}"
            if not_found:
                msg += f"\n注：未找到以下文件夹：{', '.join(not_found)}"

        self.save_keyword_mappings()
        if keyword not in self.effective_keyword_map:
            await self._persist_keyword_recent_media_ids()
        yield event.plain_result(msg)

    # ==================== 动态命令处理 ====================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_dynamic_commands(self, event: AstrMessageEvent):
        """处理群聊和私聊消息中的动态命令"""
        async for result in self._process_dynamic_command(event):
            yield result
            event.stop_event()

    async def _process_dynamic_command(self, event: AstrMessageEvent):
        """处理动态命令"""
        message_text = ""
        for seg in event.get_messages():
            if isinstance(seg, Plain):
                message_text = seg.text.strip()
                break

        if not message_text:
            return

        is_private = event.get_group_id() is None
        if not is_private and not getattr(event, "is_at_or_wake_command", False):
            return

        # 去除唤醒前缀（如有），提取命令内容
        try:
            cfg = self.context.get_config(event.unified_msg_origin)
        except Exception:
            cfg = self.context.get_config()
        wake_prefixes = cfg.get("wake_prefix", [])
        if isinstance(wake_prefixes, str):
            wake_prefixes = [wake_prefixes]
        for prefix in wake_prefixes:
            if isinstance(prefix, str) and prefix and message_text.startswith(prefix):
                message_text = message_text[len(prefix) :].strip()
                break

        parts = [p for p in message_text.split() if p]
        if not parts:
            return

        keyword = parts[0]
        force_content_type: str | None = None
        if len(parts) >= 2:
            type_arg = parts[1].lower()
            if type_arg in ["v", "vid", "video"]:
                force_content_type = "video"
            elif type_arg in ["i", "img", "image"]:
                force_content_type = "image"

        if keyword in self.effective_keyword_map:
            mapping = self.effective_keyword_map[keyword]
            if isinstance(mapping, dict):
                folder_name_raw = mapping.get("folder", "")
                include_tags = self._normalize_tag_names(mapping.get("tags", []))
                content_type = mapping.get("content_type", "image,video")
            else:
                folder_name_raw = mapping
                include_tags = []
                content_type = "image,video"

            if force_content_type:
                content_type = force_content_type

            folders = [
                f.strip()
                for f in str(folder_name_raw or "").replace("，", ",").split(",")
                if f.strip()
            ]
            if not folders and not include_tags:
                return

            folder_name = random.choice(folders) if folders else ""
            logger.debug(
                f"动态命令 /{keyword} 触发，从 {folders or ['根目录']} 中随机选择文件夹: "
                f"{folder_name or '根目录'}, tags={include_tags}, content_type={content_type}"
            )

            result = await self.get_random_file_from_keyword(
                keyword, folder_name, content_type, include_tags
            )

            if isinstance(result, list):
                yield event.chain_result(result)
            else:
                yield event.plain_result(result)

    async def terminate(self):
        """插件销毁时的清理工作"""
        await self._persist_keyword_recent_media_ids()
        logger.info("CF图床助手已卸载")
