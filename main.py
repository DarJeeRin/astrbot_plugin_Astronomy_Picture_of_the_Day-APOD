# ruff: noqa: UP006, UP035, UP045
import asyncio
import hashlib
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# 尝试使用框架提供的 MessageChain；若不存在则回退到轻量包装类，保证主动推送时有 `.chain` 属性。
try:
    from astrbot.api import MessageChain  # type: ignore
except Exception:
    class MessageChain:  # pragma: no cover - fallback
        def __init__(self, chain):
            self.chain = chain


@register("apod", "Cysheper", "NASA APOD plugin", "0.0.1")
class APOD(Star):
    APOD_CACHE_KEY = "apod_cache"
    PUSH_LAST_SENT_DATE_KEY = "apod_push:last_sent_date"
    PUSH_PAYLOAD_KEY_PREFIX = "apod_push:last_payload:"

    def __init__(self, context: Context, config: AstrBotConfig):
        self.config = config
        self.context = context
        self.last_apod_error: Optional[str] = None
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
        if not self.token:
            logger.warning("未配置 NASA API Token，请在插件配置中填写 `token` 字段。")

        self.image = self.config.get("image", True)
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
        if apod_data.get("media_type") != "image" and self.image:
            return "今天的 APOD 不是图片类型，请稍后再试。"
        url = apod_data.get("hdurl", apod_data.get("url"))
        if self.image and not url:
            return "获取 APOD 图片链接失败，请稍后重试。"
        return None

    async def _build_display_payload(self, apod_data: Dict[str, Any]) -> Dict[str, str]:
        explanation = apod_data.get("explanation")
        title = apod_data.get("title")
        url = apod_data.get("hdurl", apod_data.get("url"))
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
            "title": (title_zh or title or "").strip(),
            "date": str(apod_date).strip() if apod_date else "",
            "explanation": (explanation_zh or explanation or "").strip(),
        }

    def _build_chain_from_payload(self, payload: Dict[str, str]) -> List[Any]:
        chain = []
        if self.image and payload.get("url"):
            chain.append(Comp.Image.fromURL(payload["url"]))
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
                    "title": str(cached_payload.get("title", "")).strip(),
                    "date": cached_date,
                    "explanation": str(cached_payload.get("explanation", "")).strip(),
                }

        payload = await self._build_display_payload(apod_data)
        await self.put_cache(payload_key, payload)
        return payload

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
        chain = self._build_chain_from_payload(payload)
        if not chain:
            logger.warning("自动推送任务：当前配置未启用任何可发送内容，跳过本轮。")
            return

        for target in targets:
            try:
                msg_list = self._build_chain_from_payload(payload)
                # 包装为 MessageChain（框架需要具有 .chain 属性的对象）
                msg = MessageChain(msg_list)
                logger.debug(
                    "send_message target=%s type=%s has_chain=%s",
                    target,
                    type(msg),
                    hasattr(msg, "chain"),
                )
                await self.context.send_message(target, msg)
                success_count += 1
            except Exception as exc:
                logger.error(f"自动推送任务：向会话 {target} 发送失败：{exc}")

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
        logger.info("正在获取 NASA 每日天文图片...")

        if self._needs_translation() and not self.provider:
            logger.warning(
                "已启用翻译功能，但未配置 provider，请在插件配置中填写 `provider` 字段。"
            )
            yield event.plain_result(
                "已启用翻译功能，但未配置 provider，请在插件配置中填写 provider 字段。"
            )
            return

        # 如果今天的 APOD 缓存仍然有效，就优先复用缓存数据。
        apod_data = await self.get_cache_apod()
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

        # 如果配置为分开发送，就把内容拆成多条消息返回。
        if self.is_divided:
            has_output = False

            if self.image and payload.get("url"):
                has_output = True
                yield event.image_result(payload["url"])
            if self.title.get("is_show") and payload.get("title"):
                has_output = True
                yield event.plain_result(f"标题：{payload['title']}")
            if self.date.get("is_show") and payload.get("date"):
                has_output = True
                yield event.plain_result(f"日期：{payload['date']}")
            if self.explanation.get("is_show") and payload.get("explanation"):
                has_output = True
                yield event.plain_result(payload["explanation"])
            if not has_output:
                yield event.plain_result("当前配置未启用任何可返回的内容。")
            return

        # 否则把所有内容拼成一条消息链返回。
        chain = self._build_chain_from_payload(payload)
        if not chain:
            yield event.plain_result("当前配置未启用任何可返回的内容。")
            return

        yield event.chain_result(chain)

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
        if (
            now - retrieved_at
        ).total_seconds() > 12 * 3600 or retrieved_at.date() != now.date():
            logger.info("缓存中的 APOD 数据已过期，正在刷新缓存。")
            return await self._fetch_and_cache_apod()

        return apod_data

    # 对上游临时错误进行重试，但鉴权失败时直接停止。
    async def get_apod(self) -> Optional[dict]:
        if not self.token:
            self.last_apod_error = (
                "未配置 NASA API Token，请在插件配置中填写 token 字段。"
            )
            return None

        base_url = f"https://api.nasa.gov/planetary/apod?api_key={self.token}"
        retryable_statuses = {502, 503, 504}
        self.last_apod_error = None
        timeout = aiohttp.ClientTimeout(total=self.timeout)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for attempt in range(self.retry_count + 1):
                try:
                    async with session.get(base_url) as response:
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
                                self.last_apod_error = "NASA API Token 无效或未授权。"
                            else:
                                self.last_apod_error = f"从 NASA 获取 APOD 数据失败（HTTP {response.status}）。"
                            return None

                        self.last_apod_error = None
                        return await response.json()
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
