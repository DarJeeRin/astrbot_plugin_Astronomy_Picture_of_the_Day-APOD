# ruff: noqa: UP006, UP035, UP045
import asyncio
import hashlib
import os
import random
import re
import tempfile
import time
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register


class _APODHTMLParser(HTMLParser):
    """Extract readable text and media from NASA's Basic HTML document."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.images = []
        self.videos = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden_depth += 1
        if self.hidden_depth:
            return
        if tag in {"p", "br", "div", "li"}:
            self.text.append(" ")
        if tag == "img":
            src = dict(attrs).get("src")
            if src:
                self.images.append(src)

        if tag in {"source", "video", "iframe", "embed"}:
            src = dict(attrs).get("src")
            if src:
                self.videos.append(src)

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden_depth:
            self.hidden_depth -= 1
        if tag in {"p", "div", "li"}:
            self.text.append(" ")

    def handle_data(self, data):
        if not self.hidden_depth:
            self.text.append(data)


def _log_url(url: str) -> str:
    # Omit query strings, fragments and userinfo (URLs can contain credentials).
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.hostname or ''}{parsed.path}"


def _log_error(exc: Exception) -> str:
    return re.sub(r"https?://[^\s]+", lambda match: _log_url(match.group()), str(exc))


class _LoggedImage(Comp.Image):
    """Keep AstrBot's image preparation, but make failures visible to APOD logs."""

    async def convert_to_base64(self):
        return await self._prepare_logged(super().convert_to_base64)

    async def convert_to_file_path(self):
        return await self._prepare_logged(super().convert_to_file_path)

    async def _prepare_logged(self, prepare):
        started = time.monotonic()
        logger.info(f"APOD 图片准备开始 stage=image_prepare url={_log_url(self.url or self.file)} timeout=framework")
        try:
            result = await prepare()
            logger.info(f"APOD 图片准备完成 stage=image_prepare elapsed={time.monotonic() - started:.2f}s")
            return result
        except Exception as exc:
            logger.error(
                f"APOD 媒体失败 stage=image_prepare media=image "
                f"url={_log_url(self.url or self.file)} "
                f"elapsed={time.monotonic() - started:.2f}s "
                f"error={type(exc).__name__} detail={_log_error(exc)}"
            )
            raise


@register("apod", "Cysheper", "NASA APOD plugin", "0.1.2")
class APOD(Star):
    APOD_API_URL = "https://science.nasa.gov/wp-json/wp/v2/apod-basic"
    APOD_CACHE_KEY = "apod_cache:v3"
    PUSH_LAST_SENT_DATE_KEY = "apod_push:last_sent_date"
    PUSH_PAYLOAD_KEY_PREFIX = "apod_push:last_payload:v3:"

    DIRECT_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm"}
    EXTERNAL_VIDEO_PAGE_HOSTS = {
        "b23.tv",
        "bilibili.com",
        "dailymotion.com",
        "vimeo.com",
        "youtu.be",
        "youtube.com",
        "youtube-nocookie.com",
    }
    VIDEO_CONTENT_TYPE_EXTENSIONS = {
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/webm": ".webm",
        "video/x-m4v": ".m4v",
    }

    def __init__(self, context: Context, config: AstrBotConfig):
        self.config = config
        self.context = context
        self.last_apod_error: Optional[str] = None
        self.last_apod_status: Optional[int] = None
        self.push_task: Optional[asyncio.Task] = None

    @staticmethod
    def _ensure_dict(value: Any) -> Dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _ensure_str_list(value: Any) -> List[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            # 兼容字符串配置：支持换行或逗号分隔。
            normalized = value.replace(",", "\n")
            return [item.strip() for item in normalized.splitlines() if item.strip()]
        return []

    def _needs_translation(self) -> bool:
        explanation_needs_translation = bool(self.explanation.get("is_show")) and bool(
            self.explanation.get("is_translate")
        )
        title_needs_translation = bool(self.title.get("is_show")) and bool(
            self.title.get("is_translate")
        )
        return explanation_needs_translation or title_needs_translation

    @staticmethod
    def _is_valid_apod_data(apod_data: Any) -> bool:
        return (
            isinstance(apod_data, dict)
            and "date" in apod_data
            and "explanation" in apod_data
        )

    @staticmethod
    def _plain_text(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        parser = _APODHTMLParser()
        parser.feed(value)
        return " ".join("".join(parser.text).split())

    @staticmethod
    def _http_url(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        value = value.strip()
        parsed = urlparse(value)
        return value if parsed.scheme in {"https", "http"} and parsed.netloc else ""

    @classmethod
    def _normalize_apod_data(cls, data: Any) -> Optional[dict]:
        if not cls._is_valid_apod_data(data):
            return None
        if not isinstance(data["date"], str):
            return None
        try:
            datetime.strptime(data["date"], "%Y-%m-%d")
        except ValueError:
            return None
        if not isinstance(data["explanation"], str):
            return None
        normalized = dict(data)
        normalized["title"] = cls._plain_text(data.get("title"))
        normalized["explanation"] = cls._plain_text(data["explanation"])
        image_url = cls._http_url(data.get("hdurl"))
        # The migrated API's `url` is an article permalink, never an image.
        if not image_url and data.get("media_type") == "image":
            basic_html = data.get("basic_html")
            if isinstance(basic_html, str):
                parser = _APODHTMLParser()
                parser.feed(basic_html)
                for src in parser.images:
                    image_url = cls._http_url(
                        urljoin(
                            cls._http_url(data.get("permalink")) or cls.APOD_API_URL,
                            src,
                        )
                    )
                    if image_url:
                        break
        normalized["hdurl"] = image_url
        # Keep a dedicated media URL: article URLs must not reach Image.fromURL.
        normalized["image_url"] = image_url
        media_type = str(data.get("media_type", "image")).strip().lower()
        if media_type == "iframe":
            media_type = "video"
        normalized["media_type"] = media_type
        normalized["media_url"] = image_url if media_type == "image" else ""
        normalized["thumbnail_url"] = ""
        if media_type == "video":
            parser = _APODHTMLParser()
            parser.feed(data.get("basic_html") if isinstance(data.get("basic_html"), str) else "")
            for src in parser.videos:
                candidate = cls._http_url(urljoin(
                    cls._http_url(data.get("permalink")) or cls.APOD_API_URL, src
                ))
                if candidate:
                    normalized["media_url"] = candidate
                    break
            normalized["thumbnail_url"] = image_url or cls._youtube_thumbnail_url(normalized["media_url"])
        return normalized

    @staticmethod
    def _build_translation_cache_key(text: str) -> str:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"translate_cache:{digest}"

    @classmethod
    def _build_push_payload_cache_key(cls, apod_date: str) -> str:
        return f"{cls.PUSH_PAYLOAD_KEY_PREFIX}{apod_date}"

    @staticmethod
    def _normalize_daily_push_time(value: Any) -> str:
        raw = str(value).strip() if value is not None else ""
        if not raw:
            return "09:00"
        try:
            parsed = datetime.strptime(raw, "%H:%M")
            return parsed.strftime("%H:%M")
        except ValueError:
            logger.warning(
                f"push.daily_push_time 配置无效：{raw}，将回退为默认值 09:00（格式示例：08:30）。"
            )
            return "09:00"

    def _seconds_until_next_daily_push(self) -> int:
        now = datetime.now()
        push_time = datetime.strptime(self.daily_push_time, "%H:%M").time()
        next_push_at = now.replace(
            hour=push_time.hour, minute=push_time.minute, second=0, microsecond=0
        )
        if next_push_at <= now:
            next_push_at += timedelta(days=1)
        return max(1, int((next_push_at - now).total_seconds()))

    async def initialize(self):
        logger.info("正在初始化 NASA APOD 插件...")

        # 先读取配置，并把嵌套配置统一规范成字典。
        self.token = self.config.get("token", "")

        self.image = self.config.get("image", True)
        self.video = self._ensure_dict(self.config.get("video", {}))
        self.video_download = bool(self.video.get("download", False))
        self.video_max_download_mb = max(1, int(self.video.get("max_download_mb", 100)))
        self.video_download_timeout = max(1, int(self.video.get("download_timeout", 120)))
        self.explanation = self._ensure_dict(self.config.get("explanation", {}))
        self.title = self._ensure_dict(self.config.get("title", {}))
        self.provider = self.config.get("provider", "")
        self.date = self._ensure_dict(self.config.get("date", {}))
        self.is_divided = self.config.get("is_divided", True)
        self.timeout = max(1, int(self.config.get("timeout", 120)))
        self.retry_count = max(0, int(self.config.get("retry_count", 2)))

        self.push = self._ensure_dict(self.config.get("push", {}))
        self.push_enabled = bool(self.push.get("enabled", True))
        self.target_unified_msg_origins = self._ensure_str_list(
            self.push.get("target_unified_msg_origins", [])
        )
        self.daily_push_time = self._normalize_daily_push_time(
            self.push.get("daily_push_time", "09:00")
        )
        self.max_groups_per_round = max(0, int(self.push.get("max_groups_per_round", 0)))

        # 如果启用了翻译但没有配置 provider，就提前提示。
        if self._needs_translation() and not self.provider:
            logger.warning(
                "已启用翻译功能，但未配置 provider，请在插件配置中填写 `provider` 字段。"
            )

        if self.push_enabled and self.target_unified_msg_origins:
            self.push_task = asyncio.create_task(self._push_loop())
            logger.info(
                f"APOD 自动推送任务已启动：每天 {self.daily_push_time} 执行，目标会话 {len(self.target_unified_msg_origins)} 个。"
            )
        elif self.push_enabled:
            logger.warning(
                "APOD 自动推送已启用，但未配置 target_unified_msg_origins，不会执行群推送。"
            )
        else:
            logger.info("APOD 自动推送已禁用。")

    def _validate_apod_output(self, apod_data: Dict[str, Any]) -> Optional[str]:
        if apod_data.get("media_type") == "video":
            if not apod_data.get("media_url"):
                return "获取 APOD 视频链接失败，请稍后重试。"
            return None
        if apod_data.get("media_type") != "image":
            return "暂不支持今天的 APOD 媒体类型。"
        url = apod_data.get("image_url")
        if self.image and not url:
            return "获取 APOD 图片链接失败，请稍后重试。"
        return None

    @classmethod
    def _is_direct_video_url(cls, url: str) -> bool:
        path = urlparse(url).path.lower()
        return Path(path).suffix in cls.DIRECT_VIDEO_EXTENSIONS

    @classmethod
    def _is_external_video_page_url(cls, url: str) -> bool:
        hostname = (urlparse(url).hostname or "").lower()
        return any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in cls.EXTERNAL_VIDEO_PAGE_HOSTS
        )

    @classmethod
    def _video_suffix(cls, url: str, content_type: str = "") -> str:
        url_suffix = Path(urlparse(url).path).suffix.lower()
        if url_suffix in cls.DIRECT_VIDEO_EXTENSIONS:
            return url_suffix
        normalized_content_type = content_type.split(";", 1)[0].strip().lower()
        return cls.VIDEO_CONTENT_TYPE_EXTENSIONS.get(normalized_content_type, ".mp4")

    @staticmethod
    def _youtube_thumbnail_url(url: str) -> str:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        video_id = ""
        if hostname == "youtu.be" or hostname.endswith(".youtu.be"):
            video_id = parsed.path.strip("/").split("/", 1)[0]
        elif hostname == "youtube.com" or hostname.endswith(".youtube.com"):
            path_parts = [part for part in parsed.path.split("/") if part]
            if len(path_parts) >= 2 and path_parts[0] in {"embed", "shorts", "live"}:
                video_id = path_parts[1]
            else:
                video_id = parse_qs(parsed.query).get("v", [""])[0]

        if not re.fullmatch(r"[A-Za-z0-9_-]{6,}", video_id):
            return ""
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

    async def _download_video(self, url: str) -> Optional[str]:
        if not self.video_download or not url or self._is_external_video_page_url(url):
            reason = "disabled" if not self.video_download else "external_page" if url else "missing_url"
            logger.info(f"APOD 视频不下载 stage=download_skipped reason={reason} url={_log_url(url)}")
            return None

        max_bytes = self.video_max_download_mb * 1024 * 1024
        temp_path: Optional[str] = None
        started = time.monotonic()
        downloaded = 0
        content_length = None
        logger.info(f"APOD 视频下载开始 url={_log_url(url)} timeout={self.video_download_timeout}s limit={self.video_max_download_mb}MB")
        try:
            timeout = aiohttp.ClientTimeout(total=self.video_download_timeout)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.get(url, allow_redirects=True) as response:
                    if response.status >= 400:
                        raise ValueError(f"HTTP {response.status}")

                    content_type = response.headers.get("Content-Type", "")
                    normalized_content_type = content_type.split(";", 1)[0].strip().lower()
                    allowed_generic_types = {
                        "application/octet-stream",
                        "binary/octet-stream",
                    }
                    if normalized_content_type and not (
                        normalized_content_type.startswith("video/")
                        or normalized_content_type in allowed_generic_types
                    ):
                        raise ValueError(f"非视频响应 Content-Type={normalized_content_type}")
                    if not normalized_content_type and not self._is_direct_video_url(
                        str(response.url)
                    ):
                        raise ValueError("非视频直链且缺少 Content-Type")
                    content_length = response.headers.get("Content-Length")
                    if content_length and int(content_length) > max_bytes:
                        raise ValueError(f"Content-Length={content_length} 超过下载上限 {self.video_max_download_mb} MB")

                    suffix = self._video_suffix(str(response.url), content_type)
                    fd, temp_path = tempfile.mkstemp(prefix="astrbot_apod_", suffix=suffix)
                    downloaded = 0
                    with os.fdopen(fd, "wb") as video_file:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            downloaded += len(chunk)
                            if downloaded > max_bytes:
                                raise ValueError(
                                    f"视频超过下载上限 {self.video_max_download_mb} MB"
                                )
                            video_file.write(chunk)
            if downloaded == 0:
                raise ValueError("视频响应为空")
            logger.info(f"APOD 视频下载完成 url={_log_url(url)} bytes={downloaded} elapsed={time.monotonic() - started:.2f}s")
            return temp_path
        except asyncio.CancelledError:
            self._remove_temp_file(temp_path)
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
            logger.error(
                f"APOD 媒体失败 stage=video_download media=video url={_log_url(url)} "
                f"elapsed={time.monotonic() - started:.2f}s timeout={self.video_download_timeout}s "
                f"bytes={downloaded} expected_bytes={content_length or 'unknown'} "
                f"error={type(exc).__name__} detail={_log_error(exc)}；将返回视频链接"
            )
            if temp_path:
                self._remove_temp_file(temp_path)
            return None

    @staticmethod
    def _remove_temp_file(path: Optional[str]):
        if not path:
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(f"清理 APOD 临时视频失败：{exc}")

    async def _build_display_payload(self, apod_data: Dict[str, Any]) -> Dict[str, str]:
        explanation = apod_data.get("explanation")
        title = apod_data.get("title")
        url = apod_data.get("media_url") or apod_data.get("image_url")
        apod_date = apod_data.get("date")

        explanation_zh, title_zh = None, None
        try:
            # 使用哈希键缓存翻译结果，避免对同一段文本重复调用 LLM。
            if (
                self.explanation.get("is_show")
                and self.explanation.get("is_translate")
                and self.provider
                and explanation
            ):
                explanation_cache_key = self._build_translation_cache_key(explanation)
                cached_translation = await self.get_cache(explanation_cache_key)
                if cached_translation is not None:
                    explanation_zh = cached_translation
                else:
                    explanation_zh = await self.translate_explanation(
                        explanation.strip(), self.provider
                    )
                    await self.put_cache(explanation_cache_key, explanation_zh)

            if (
                self.title.get("is_show")
                and self.title.get("is_translate")
                and self.provider
                and title
            ):
                title_cache_key = self._build_translation_cache_key(title)
                cached_translation = await self.get_cache(title_cache_key)
                if cached_translation is not None:
                    title_zh = cached_translation
                else:
                    title_zh = await self.translate_explanation(
                        title.strip(), self.provider
                    )
                    await self.put_cache(title_cache_key, title_zh)
        except Exception as exc:
            logger.error(f"翻译 APOD 内容失败：{exc}")

        return {
            "url": str(url).strip() if url else "",
            "media_type": str(apod_data.get("media_type", "image")),
            "thumbnail_url": str(apod_data.get("thumbnail_url") or ""),
            "title": (title_zh or title or "").strip(),
            "date": str(apod_date).strip() if apod_date else "",
            "explanation": (explanation_zh or explanation or "").strip(),
        }

    def _build_chain_from_payload(
        self, payload: Dict[str, str], video_path: Optional[str] = None
    ) -> List[Any]:
        chain = []
        if payload.get("media_type", "image") == "video":
            if self.image and payload.get("thumbnail_url"):
                chain.append(_LoggedImage(file=payload["thumbnail_url"]))
            if video_path:
                chain.append(Comp.Video.fromFileSystem(path=video_path))
            elif payload.get("url"):
                chain.append(Comp.Plain(f"视频链接：{payload['url']}\n"))
        elif self.image and payload.get("url"):
            chain.append(_LoggedImage(file=payload["url"]))
        if self.title.get("is_show") and payload.get("title"):
            chain.append(Comp.Plain(f"标题：{payload['title']}\n"))
        if self.date.get("is_show") and payload.get("date"):
            chain.append(Comp.Plain(f"日期：{payload['date']}\n"))
        if self.explanation.get("is_show") and payload.get("explanation"):
            chain.append(Comp.Plain(payload["explanation"]))
        return chain

    def _get_round_targets(self) -> List[str]:
        targets = list(self.target_unified_msg_origins)
        if self.max_groups_per_round > 0:
            return targets[: self.max_groups_per_round]
        return targets

    async def _get_or_build_push_payload(
        self, apod_data: Dict[str, Any], apod_date: str
    ) -> Dict[str, str]:
        payload_key = self._build_push_payload_cache_key(apod_date)
        cached_payload = await self.get_cache(payload_key)
        if isinstance(cached_payload, dict):
            cached_date = str(cached_payload.get("date", "")).strip()
            if cached_date == apod_date:
                return {
                    "url": str(cached_payload.get("url", "")).strip(),
                    "media_type": str(cached_payload.get("media_type", "image")),
                    "thumbnail_url": str(cached_payload.get("thumbnail_url") or ""),
                    "title": str(cached_payload.get("title", "")).strip(),
                    "date": cached_date,
                    "explanation": str(cached_payload.get("explanation", "")).strip(),
                }

        payload = await self._build_display_payload(apod_data)
        await self.put_cache(payload_key, payload)
        return payload

    async def _send_logged(self, send, chain: MessageChain, payload: Dict[str, str], target: str) -> bool:
        started = time.monotonic()
        kinds = ",".join(
            "image" if isinstance(item, Comp.Image) else
            "video" if isinstance(item, Comp.Video) else "text"
            for item in chain.chain
        )
        logger.info(f"APOD 发送开始 target={target} date={payload.get('date')} media={kinds}")
        try:
            delivered = await send(chain)
            if delivered is False:
                raise ValueError("未找到目标会话对应的平台")
        except Exception as exc:
            logger.error(
                f"APOD 媒体失败 stage=send target={target} date={payload.get('date')} "
                f"media={kinds} url={_log_url(payload.get('url', ''))} "
                f"elapsed={time.monotonic() - started:.2f}s "
                f"error={type(exc).__name__} detail={_log_error(exc)}"
            )
            return False
        logger.info(f"APOD 发送接口完成 target={target} elapsed={time.monotonic() - started:.2f}s")
        return True

    async def _run_push_once(self):
        targets = self._get_round_targets()
        if not targets:
            logger.info("自动推送任务：当前未配置可用 target_unified_msg_origins，跳过本轮。")
            return

        if self._needs_translation() and not self.provider:
            logger.warning("自动推送任务：已启用翻译但未配置 provider，跳过本轮。")
            return

        apod_data = await self.get_cache_apod()
        if not apod_data:
            logger.warning(
                f"自动推送任务：拉取 APOD 失败，原因：{self.last_apod_error or '未知错误'}"
            )
            return

        apod_date = str(apod_data.get("date", "")).strip()
        if not apod_date:
            logger.warning("自动推送任务：APOD 数据缺少 date，跳过本轮。")
            return

        last_sent_date = await self.get_cache(self.PUSH_LAST_SENT_DATE_KEY)
        if str(last_sent_date).strip() == apod_date:
            logger.info(f"自动推送任务：{apod_date} 已推送过，跳过重复推送。")
            return

        validation_error = self._validate_apod_output(apod_data)
        if validation_error:
            logger.warning(f"自动推送任务：{validation_error}")
            return

        payload = await self._get_or_build_push_payload(apod_data, apod_date)
        success_count = 0
        video_path = None
        if payload.get("media_type") == "video":
            video_path = await self._download_video(payload.get("url", ""))
        try:
            chain = self._build_chain_from_payload(payload, video_path)
            if not chain:
                return
            for target in targets:
                async def send(msg, target=target):
                    return await self.context.send_message(target, msg)
                if await self._send_logged(send, MessageChain(chain), payload, target):
                    success_count += 1
        finally:
            self._remove_temp_file(video_path)

        if success_count > 0:
            await self.put_cache(self.PUSH_LAST_SENT_DATE_KEY, apod_date)
            logger.info(
                f"自动推送任务：{apod_date} 推送完成，成功 {success_count}/{len(targets)}。"
            )
        else:
            logger.warning("自动推送任务：本轮所有目标发送失败，不更新已推送日期。")

    async def _push_loop(self):
        logger.info("APOD 自动推送定时任务已进入运行状态。")
        while True:
            wait_seconds = self._seconds_until_next_daily_push()
            next_run_at = (datetime.now() + timedelta(seconds=wait_seconds)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            logger.info(
                f"APOD 自动推送定时任务：下次将在 {next_run_at} 执行（配置时间 {self.daily_push_time}）。"
            )
            try:
                await asyncio.sleep(wait_seconds)
                await self._run_push_once()
            except asyncio.CancelledError:
                logger.info("APOD 自动推送任务已取消。")
                raise
            except Exception as exc:
                logger.error(f"APOD 自动推送定时任务发生异常：{exc}")

    @filter.command("apod")
    async def apod(self, event: AstrMessageEvent):
        async for result in self._reply_apod(event):
            yield result

    @filter.command("apod_random")
    async def apod_random(self, event: AstrMessageEvent):
        """Select a historical APOD without changing the daily APOD cache."""
        async for result in self._reply_apod(event, random_apod=True):
            yield result

    async def _reply_apod(self, event: AstrMessageEvent, random_apod: bool = False):
        logger.info("正在获取 NASA 随机 APOD..." if random_apod else "正在获取 NASA 每日天文图片...")

        if self._needs_translation() and not self.provider:
            logger.warning(
                "已启用翻译功能，但未配置 provider，请在插件配置中填写 `provider` 字段。"
            )
            yield event.plain_result(
                "已启用翻译功能，但未配置 provider，请在插件配置中填写 provider 字段。"
            )
            return

        # Random requests bypass the daily data cache and push state.
        apod_data = (
            await self.get_random_apod() if random_apod else await self.get_cache_apod()
        )
        if not apod_data:
            yield event.plain_result(
                self.last_apod_error or "获取 APOD 数据失败，请稍后重试。"
            )
            return

        validation_error = self._validate_apod_output(apod_data)
        if validation_error:
            yield event.plain_result(validation_error)
            return

        payload = await self._build_display_payload(apod_data)

        video_path = None
        if payload.get("media_type") == "video":
            video_path = await self._download_video(payload.get("url", ""))
        try:
            chain = self._build_chain_from_payload(payload, video_path)
            if not chain:
                yield event.plain_result("当前配置未启用任何可返回的内容。")
                return
            target = getattr(event, "unified_msg_origin", "unknown")
            groups = [[item] for item in chain] if self.is_divided else [chain]
            for items in groups:
                if any(isinstance(item, (Comp.Image, Comp.Video)) for item in items):
                    if not await self._send_logged(event.send, MessageChain(items), payload, target):
                        yield event.plain_result("APOD 媒体发送失败，请查看 AstrBot 日志。")
                else:
                    yield event.chain_result(items)
        finally:
            self._remove_temp_file(video_path)

    # 插件自带的 KV 存储足够保存 APOD 数据、推送状态和翻译结果。
    async def put_cache(self, key: str, value: Any):
        logger.info(f"正在写入缓存，键：{key}")
        try:
            await self.put_kv_data(key, value)
        except Exception as exc:
            logger.error(f"写入缓存失败，键：{key}，错误：{exc}")

    async def get_cache(self, key: str) -> Optional[Any]:
        logger.info(f"正在读取缓存，键：{key}")
        try:
            return await self.get_kv_data(key, None)
        except Exception as exc:
            logger.error(f"读取缓存失败，键：{key}，错误：{exc}")
            return None

    async def translate_explanation(self, explanation: str, provider_id: str) -> str:
        logger.info("正在翻译 APOD 内容...")
        llm_resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            system_prompt=(
                "You are a professional astronomy translator. Translate the input text "
                "into Simplified Chinese accurately, and do not add any extra explanation."
            ),
            prompt=explanation,
        )
        return llm_resp.completion_text

    # 把“拉取并写入缓存”的逻辑集中到这里，保证所有刷新路径行为一致。
    async def _fetch_and_cache_apod(self) -> Optional[dict]:
        logger.info("正在从 NASA API 获取最新 APOD 数据...")
        apod_data = await self.get_apod()
        if apod_data is None:
            return None

        if not self._is_valid_apod_data(apod_data):
            logger.error(f"获取到的 APOD 数据无效：{apod_data}")
            return None

        apod_data["retrieved_at"] = datetime.now().isoformat()
        await self.put_cache(self.APOD_CACHE_KEY, apod_data)
        return apod_data

    # 只有在缓存结构有效且时间未过期时，才真正使用缓存。
    async def get_cache_apod(self) -> Optional[dict]:
        apod_data = await self.get_cache(self.APOD_CACHE_KEY)
        if apod_data is None:
            return await self._fetch_and_cache_apod()

        if not self._is_valid_apod_data(apod_data):
            logger.warning("缓存中的 APOD 数据无效，正在刷新缓存。")
            return await self._fetch_and_cache_apod()

        retrieved_at_raw = apod_data.get("retrieved_at")
        if not retrieved_at_raw:
            logger.info("缓存中的 APOD 数据缺少 retrieved_at，正在刷新缓存。")
            return await self._fetch_and_cache_apod()

        try:
            retrieved_at = datetime.fromisoformat(retrieved_at_raw)
        except (TypeError, ValueError):
            logger.warning("缓存中的 APOD 时间格式无效，正在刷新缓存。")
            return await self._fetch_and_cache_apod()

        now = datetime.now()
        apod_today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        if (
            (now - retrieved_at).total_seconds() > 12 * 3600
            or retrieved_at.date() != now.date()
            or apod_data.get("date") != apod_today
        ):
            logger.info("缓存中的 APOD 数据已过期，正在刷新缓存。")
            return await self._fetch_and_cache_apod()

        return apod_data

    async def get_random_apod(self) -> Optional[dict]:
        # NASA's public JSON API exposes date routes, not a random JSON route.
        first_day = datetime(1995, 6, 16).date()
        today = datetime.now(ZoneInfo("America/New_York")).date()
        for _ in range(3):
            selected = first_day + timedelta(
                days=random.randint(0, (today - first_day).days)
            )
            logger.info(f"随机 APOD 日期：{selected.isoformat()}")
            data = await self.get_apod(selected.strftime("%y%m%d"))
            if data is not None:
                return data
            # Some early archive dates are missing; reselect only for HTTP 404.
            if self.last_apod_status != 404:
                return None
        return None

    # 对上游临时错误进行重试，但鉴权失败时直接停止。
    async def get_apod(self, apod_day: Optional[str] = None) -> Optional[dict]:
        # NASA's migrated public endpoint uses YYMMDD and does not require a key.
        if apod_day is None:
            apod_day = datetime.now(ZoneInfo("America/New_York")).strftime("%y%m%d")
        base_url = f"{self.APOD_API_URL}/{apod_day}"
        retryable_statuses = {502, 503, 504}
        self.last_apod_error = None
        self.last_apod_status = None
        timeout = aiohttp.ClientTimeout(total=self.timeout)

        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            for attempt in range(self.retry_count + 1):
                self.last_apod_status = None
                try:
                    async with session.get(base_url) as response:
                        self.last_apod_status = response.status
                        if response.status >= 400:
                            error_text = await response.text()
                            logger.error(
                                f"获取 APOD 数据失败：状态码={response.status}，响应内容={error_text}"
                            )

                            if response.status == 429:
                                self.last_apod_error = (
                                    "NASA API 已达到速率限制，请稍后再试。"
                                )
                            elif response.status in retryable_statuses:
                                self.last_apod_error = (
                                    "NASA APOD 服务暂时不可用，请稍后重试。"
                                )
                                if attempt < self.retry_count:
                                    await asyncio.sleep(min(2**attempt, 4))
                                    continue
                            elif response.status in {401, 403}:
                                self.last_apod_error = "NASA APOD 接口访问被拒绝，请检查网络或上游访问策略。"
                            else:
                                self.last_apod_error = f"从 NASA 获取 APOD 数据失败（HTTP {response.status}）。"
                            return None

                        apod_data = self._normalize_apod_data(await response.json())
                        if apod_data is None:
                            self.last_apod_error = "NASA APOD 返回的数据格式无效。"
                            return None
                        self.last_apod_error = None
                        return apod_data
                except asyncio.TimeoutError:
                    logger.error(
                        f"获取 APOD 数据超时：请求在 {self.timeout} 秒后超时。"
                    )
                    self.last_apod_error = "请求 NASA APOD 超时，请稍后重试。"
                    if attempt < self.retry_count:
                        await asyncio.sleep(min(2**attempt, 4))
                        continue
                    return None
                except aiohttp.ClientError as exc:
                    logger.error(f"获取 APOD 数据时发生客户端错误：{exc}")
                    self.last_apod_error = "连接 NASA APOD 时发生网络错误，请稍后重试。"
                    if attempt < self.retry_count:
                        await asyncio.sleep(min(2**attempt, 4))
                        continue
                    return None
                except Exception as exc:
                    logger.error(f"获取 APOD 数据时发生未知错误：{exc}")
                    self.last_apod_error = "获取 APOD 数据时发生未知错误。"
                    return None
                finally:
                    if not self.last_apod_error:
                        logger.info(
                            f"APOD 数据获取尝试 {attempt + 1}/{self.retry_count + 1} 已完成。"
                        )

        return None

    async def terminate(self):
        logger.info("正在终止 NASA APOD 插件...")
        if self.push_task and not self.push_task.done():
            self.push_task.cancel()
            try:
                await self.push_task
            except asyncio.CancelledError:
                pass
