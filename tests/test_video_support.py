import asyncio
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _Image:
    @staticmethod
    def fromURL(url):
        return ("image", url)


class _Video:
    @staticmethod
    def fromFileSystem(path):
        return ("video", path)


def _plain(text):
    return ("plain", text)


class _FakeContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            yield chunk


class _FakeResponse:
    def __init__(self, url, headers, chunks):
        self.url = url
        self.headers = headers
        self.status = 200
        self.content = _FakeContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _FakeSession:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def get(self, *_args, **_kwargs):
        return self.response


def _install_astrbot_stubs():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    components = types.ModuleType("astrbot.api.message_components")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")

    components.Image = _Image
    components.Video = _Video
    components.Plain = _plain
    api.AstrBotConfig = dict
    api.MessageChain = type("MessageChain", (), {})
    api.logger = _Logger()

    event.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event.filter = types.SimpleNamespace(command=lambda _name: lambda func: func)

    star.Context = type("Context", (), {})
    star.Star = type("Star", (), {})
    star.register = lambda *_args, **_kwargs: lambda cls: cls

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.message_components": components,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
        }
    )


def _install_aiohttp_stub():
    try:
        import aiohttp  # noqa: F401
    except ModuleNotFoundError:
        aiohttp = types.ModuleType("aiohttp")
        aiohttp.ClientError = type("ClientError", (Exception,), {})
        aiohttp.ClientTimeout = lambda **kwargs: kwargs
        aiohttp.ClientSession = lambda **_kwargs: None
        sys.modules["aiohttp"] = aiohttp


_install_astrbot_stubs()
_install_aiohttp_stub()
_SPEC = importlib.util.spec_from_file_location(
    "apod_plugin_main", Path(__file__).resolve().parents[1] / "main.py"
)
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MODULE)
APOD = _MODULE.APOD


class VideoSupportTests(unittest.TestCase):
    def setUp(self):
        self.plugin = APOD.__new__(APOD)
        self.plugin.image = True
        self.plugin.title = {"is_show": False, "is_translate": False}
        self.plugin.date = {"is_show": False}
        self.plugin.explanation = {"is_show": False, "is_translate": False}
        self.plugin.provider = ""
        self.plugin.video_download = True
        self.plugin.video_max_download_mb = 100
        self.plugin.timeout = 10

    def test_detects_video_page_hosts_without_blocking_direct_files(self):
        self.assertTrue(
            APOD._is_external_video_page_url(
                "https://www.youtube.com/embed/abcdefghijk"
            )
        )
        self.assertTrue(
            APOD._is_external_video_page_url("https://player.vimeo.com/video/123")
        )
        self.assertFalse(
            APOD._is_external_video_page_url("https://cdn.example.com/apod.mp4")
        )
        self.assertTrue(APOD._is_direct_video_url("https://cdn.example.com/apod.mp4?x=1"))

    def test_external_video_chain_contains_thumbnail_and_link(self):
        payload = {
            "media_type": "video",
            "media_url": "https://www.youtube.com/embed/abcdefghijk",
            "thumbnail_url": "https://example.com/thumb.jpg",
        }

        chain = self.plugin._build_chain_from_payload(payload)

        self.assertEqual(chain[0], ("image", "https://example.com/thumb.jpg"))
        self.assertEqual(
            chain[1],
            ("plain", "视频链接：https://www.youtube.com/embed/abcdefghijk\n"),
        )

    def test_downloaded_video_chain_uses_local_video_component(self):
        payload = {
            "media_type": "video",
            "media_url": "https://cdn.example.com/apod.mp4",
            "thumbnail_url": "https://example.com/thumb.jpg",
        }

        chain = self.plugin._build_chain_from_payload(payload, "C:/tmp/apod.mp4")

        self.assertEqual(chain[1], ("video", "C:/tmp/apod.mp4"))
        self.assertFalse(any(item[0] == "plain" for item in chain))

    def test_video_payload_preserves_thumbnail_and_link(self):
        payload = asyncio.run(
            self.plugin._build_display_payload(
                {
                    "media_type": "video",
                    "url": "https://youtu.be/abcdefghijk",
                    "thumbnail_url": "https://example.com/thumb.jpg",
                    "title": "Title",
                    "date": "2026-08-21",
                    "explanation": "Explanation",
                }
            )
        )

        self.assertEqual(payload["media_type"], "video")
        self.assertEqual(payload["media_url"], "https://youtu.be/abcdefghijk")
        self.assertEqual(payload["thumbnail_url"], "https://example.com/thumb.jpg")

    def test_youtube_thumbnail_is_derived_when_nasa_omits_it(self):
        payload = asyncio.run(
            self.plugin._build_display_payload(
                {
                    "media_type": "video",
                    "url": "https://www.youtube.com/embed/abcdefghijk",
                    "title": "Title",
                    "date": "2026-08-21",
                    "explanation": "Explanation",
                }
            )
        )

        self.assertEqual(
            payload["thumbnail_url"],
            "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg",
        )

    def test_downloads_video_content_without_a_file_extension(self):
        response = _FakeResponse(
            "https://cdn.example.com/video?id=1",
            {"Content-Type": "video/mp4", "Content-Length": "3"},
            [b"abc"],
        )
        with patch.object(
            _MODULE.aiohttp,
            "ClientSession",
            return_value=_FakeSession(response),
        ):
            path = asyncio.run(
                self.plugin._download_video("https://cdn.example.com/video?id=1")
            )

        self.assertIsNotNone(path)
        try:
            with open(path, "rb") as video_file:
                self.assertEqual(video_file.read(), b"abc")
            self.assertTrue(path.endswith(".mp4"))
        finally:
            if path and os.path.exists(path):
                os.remove(path)

    def test_youtube_is_never_downloaded(self):
        with patch.object(
            _MODULE.aiohttp,
            "ClientSession",
            side_effect=AssertionError("network must not be used"),
        ):
            result = asyncio.run(
                self.plugin._download_video("https://youtube.com/watch?v=abcdefghijk")
            )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
