"""Run with unittest; real AstrBot APIs, SQLite, and a local HTTP fixture.

NASA example source: https://github.com/nasa/apod-api/blob/master/README.md
Run from an external working directory to keep framework data out of checkout.
"""
import asyncio
import importlib.util
import base64
import io
import json
import os
import tempfile
import sys
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec, patch
from zoneinfo import ZoneInfo

# Isolate framework-generated configuration and database from user data.
_RUNTIME = tempfile.TemporaryDirectory(prefix="apod-tests-")
os.environ["ASTRBOT_ROOT"] = _RUNTIME.name

from aiohttp import ClientSession, web
from PIL import Image as PILImage
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context
from astrbot.core import db_helper

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('apod_under_test', ROOT / 'main.py')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
APOD = module.APOD
SAMPLE = json.loads((ROOT / 'tests/fixtures/nasa_apod.json').read_text())


class NormalizationTests(unittest.TestCase):
    def test_official_migrated_example(self):
        data = APOD._normalize_apod_data(SAMPLE)
        self.assertEqual(data['image_url'], SAMPLE['hdurl'])
        self.assertNotEqual(data['image_url'], SAMPLE['url'])
        self.assertNotIn('<a', data['explanation'])
        self.assertIn('NGC 6302', data['explanation'])

    def test_html_entities_and_hidden_content(self):
        self.assertEqual(APOD._plain_text('<p>A &amp; B</p><script>bad</script><p>C</p>'), 'A & B C')

    def test_missing_hdurl_extracts_basic_html_image(self):
        data = dict(SAMPLE, hdurl=None, basic_html='<img src="/images/example.jpg">')
        result = APOD._normalize_apod_data(data)
        self.assertEqual(result['image_url'], 'https://science.nasa.gov/images/example.jpg')

    def test_article_is_never_used_as_image(self):
        data = APOD._normalize_apod_data(dict(SAMPLE, hdurl='', basic_html=''))
        self.assertEqual(data['image_url'], '')

    def test_non_http_media_is_rejected(self):
        data = APOD._normalize_apod_data(dict(SAMPLE, hdurl='javascript:bad', basic_html='<img src="data:bad">'))
        self.assertEqual(data['image_url'], '')

    def test_upstream_image_classification_preserves_featured_video_link(self):
        fixture = json.loads((ROOT / 'tests/fixtures/nasa_external_gif.json').read_text())
        data = APOD._normalize_apod_data(fixture)
        self.assertEqual(data['media_type'], 'image')
        self.assertIn('www.esa.int/ESA_Multimedia/', data['external_media_url'])
        self.assertTrue(data['image_url'])

    def test_featured_media_precedes_related_gif_links(self):
        data = dict(SAMPLE, explanation='<a href="https://example.com/related.gif">older example</a><a href="https://www.esa.int/featured">featured <b>video</b></a>')
        self.assertEqual(APOD._normalize_apod_data(data)['external_media_url'], 'https://www.esa.int/featured')

    def test_invalid_payloads(self):
        for data in ([], {}, dict(SAMPLE, date='invalid'), dict(SAMPLE, explanation=None)):
            with self.subTest(data=type(data)):
                self.assertIsNone(APOD._normalize_apod_data(data))


class FrameworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db_helper.initialize()
        self.context = create_autospec(Context, instance=True)
        self.context.send_message.return_value = True
        self.plugin = APOD(self.context, {
            'image': True, 'is_divided': False,
            'title': {'is_show': True, 'is_translate': False},
            'explanation': {'is_show': True, 'is_translate': False},
            'date': {'is_show': True}, 'push': {'enabled': False},
            'timeout': 5, 'retry_count': 1,
        })
        self.plugin.plugin_id = 'apod-test-' + uuid.uuid4().hex
        await self.plugin.initialize()
        self.status = 200
        self.calls = []
        self.payload = dict(SAMPLE, date=datetime.now(ZoneInfo('America/New_York')).date().isoformat())
        async def handler(request):
            self.calls.append(request.path)
            return web.json_response(self.payload, status=self.status)
        app = web.Application()
        app.router.add_get('/{day}', handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        owner = self
        class LocalSession:
            def __init__(self, **kwargs):
                owner.assertTrue(kwargs.pop('trust_env'))
                self.session = ClientSession(**kwargs)
            async def __aenter__(self): return self
            async def __aexit__(self, *args): await self.session.close()
            def get(self, url):
                owner.assertRegex(url, r'^https://science\.nasa\.gov/wp-json/wp/v2/apod-basic/\d{6}$')
                owner.assertNotIn('api_key', url)
                return self.session.get(f'http://127.0.0.1:{port}/{url.rsplit("/", 1)[1]}')
        self.network = patch.object(module.aiohttp, 'ClientSession', LocalSession)
        self.network.start()

    async def asyncTearDown(self):
        self.network.stop()
        await self.plugin.terminate()
        await self.runner.cleanup()
        await db_helper.engine.dispose()

    async def test_new_endpoint_without_token_and_sqlite_cache(self):
        data = await self.plugin.get_cache_apod()
        self.assertEqual(data['image_url'], SAMPLE['hdurl'])
        self.assertIn('retrieved_at', data)
        self.assertEqual(await self.plugin.get_cache_apod(), data)
        self.assertEqual(self.calls, ['/' + datetime.now(ZoneInfo('America/New_York')).strftime('%y%m%d')])

    async def test_legacy_token_is_not_transmitted(self):
        self.plugin.token = 'legacy-token-must-not-be-sent'
        self.assertIsNotNone(await self.plugin.get_apod())

    async def test_scheduled_task_terminates_without_delivery(self):
        self.plugin.config['push'] = {
            'enabled': True,
            'target_unified_msg_origins': ['aiocqhttp:GroupMessage:fixture'],
        }
        await self.plugin.initialize()
        task = self.plugin.push_task
        self.assertIsNotNone(task)
        await asyncio.sleep(0)
        await self.plugin.terminate()
        self.assertTrue(task.cancelled())
        self.context.send_message.assert_not_awaited()

    async def test_previous_apod_day_refreshes(self):
        stale = APOD._normalize_apod_data(SAMPLE)
        stale['retrieved_at'] = datetime.now().isoformat()
        await self.plugin.put_cache(APOD.APOD_CACHE_KEY, stale)
        self.assertEqual((await self.plugin.get_cache_apod())['date'], self.payload['date'])
        self.assertEqual(len(self.calls), 1)

    async def test_actual_astrbot_command_results(self):
        # Exercise AstrMessageEvent result factories, without a connected platform.
        event = AstrMessageEvent.__new__(AstrMessageEvent)
        event.session = 'aiocqhttp:GroupMessage:fixture'
        event.send = AsyncMock()
        result = [r async for r in self.plugin.apod(event)]
        self.assertEqual(result, [])
        result = [event.send.call_args.args[0]]
        self.assertEqual(len(result[0].chain), 4)
        self.assertIsInstance(result[0].chain[0], Image)
        self.assertEqual(result[0].chain[0].file, SAMPLE['hdurl'])
        self.assertIsInstance(result[0].chain[1], Plain)
        self.assertNotIn('<strong>', result[0].chain[-1].text)
        self.plugin.is_divided = True
        event.send.reset_mock()
        self.assertEqual(len([r async for r in self.plugin.apod(event)]), 3)
        event.send.assert_awaited_once()

    async def test_translation_contract_and_cache(self):
        self.plugin.title['is_translate'] = True
        self.plugin.provider = 'fixture-provider'
        self.context.llm_generate.return_value = SimpleNamespace(completion_text='蝴蝶星云')
        data = await self.plugin.get_cache_apod()
        first = await self.plugin._build_display_payload(data)
        self.assertEqual(first['title'], '蝴蝶星云')
        self.assertEqual(await self.plugin._build_display_payload(data), first)
        self.context.llm_generate.assert_awaited_once()
        kwargs = self.context.llm_generate.call_args.kwargs
        self.assertEqual(kwargs['chat_provider_id'], 'fixture-provider')
        self.assertEqual(kwargs['prompt'], SAMPLE['title'])

    async def test_push_contract_and_deduplication(self):
        self.plugin.target_unified_msg_origins = ['aiocqhttp:GroupMessage:fixture']
        await self.plugin._run_push_once()
        self.context.send_message.assert_awaited_once()
        message = self.context.send_message.call_args.args[1]
        self.assertIsInstance(message, MessageChain)
        self.assertEqual(message.chain[0].file, SAMPLE['hdurl'])
        await self.plugin._run_push_once()
        self.context.send_message.assert_awaited_once()

    async def test_unmatched_platform_does_not_mark_sent(self):
        self.plugin.target_unified_msg_origins = ['aiocqhttp:GroupMessage:fixture']
        self.context.send_message.return_value = False
        await self.plugin._run_push_once()
        self.assertIsNone(await self.plugin.get_cache(APOD.PUSH_LAST_SENT_DATE_KEY))

    async def test_503_retries_and_403_does_not(self):
        self.status = 503
        self.assertIsNone(await self.plugin.get_apod())
        self.assertEqual(len(self.calls), 2)
        self.assertIn('暂时不可用', self.plugin.last_apod_error)
        self.calls.clear()
        self.status = 403
        self.assertIsNone(await self.plugin.get_apod())
        self.assertEqual(len(self.calls), 1)
        self.assertIn('访问被拒绝', self.plugin.last_apod_error)

    async def test_invalid_success_response_is_reported(self):
        self.payload = {'code': 'no_apod'}
        self.assertIsNone(await self.plugin.get_apod())
        self.assertIn('数据格式无效', self.plugin.last_apod_error)

    async def test_external_gif_reply_sends_image_and_cleans_file(self):
        fixture = json.loads((ROOT / 'tests/fixtures/nasa_external_gif.json').read_text())
        data = APOD._normalize_apod_data(fixture)
        event = AstrMessageEvent.__new__(AstrMessageEvent)
        event.session = 'aiocqhttp:GroupMessage:fixture'
        seen = []
        async def send(message):
            images = [item for item in message.chain if isinstance(item, Image)]
            self.assertEqual(len(images), 1)
            seen.append(images[0].file)
            self.assertTrue(Path(images[0].file).exists())
            self.assertFalse(any(isinstance(item, module.Comp.Video) for item in message.chain))
        event.send = AsyncMock(side_effect=send)
        with tempfile.NamedTemporaryFile(suffix='.gif', delete=False) as file:
            path = file.name
        with patch.object(self.plugin, 'get_cache_apod', AsyncMock(return_value=data)), patch.object(self.plugin, '_resolve_external_media', AsyncMock(return_value=('https://example.com/original.gif', 'gif'))), patch.object(self.plugin, '_download_gif', AsyncMock(return_value=path)) as download:
            self.assertEqual([r async for r in self.plugin.apod(event)], [])
        self.assertEqual(seen, [path])
        self.assertFalse(Path(path).exists())
        download.assert_awaited_once_with('https://example.com/original.gif')
        self.assertFalse(self.plugin.video_download)  # GIF uses image, not video opt-in.

    async def test_external_disabled_preserves_static_image_and_link(self):
        fixture = json.loads((ROOT / 'tests/fixtures/nasa_external_gif.json').read_text())
        data = APOD._normalize_apod_data(fixture)
        self.plugin.external_media_enabled = False
        with patch.object(self.plugin, '_resolve_external_media', AsyncMock()) as resolve:
            payload = await self.plugin._build_display_payload(data)
        resolve.assert_not_awaited()
        self.assertEqual(payload['media_type'], 'image')
        self.assertEqual(payload['url'], fixture['hdurl'])
        self.assertIn('www.esa.int', payload['external_url'])

    async def test_external_gif_push_retains_fields_and_cleans_file(self):
        fixture = json.loads((ROOT / 'tests/fixtures/nasa_external_gif.json').read_text())
        fixture['date'] = datetime.now(ZoneInfo('America/New_York')).date().isoformat()
        data = APOD._normalize_apod_data(fixture)
        self.plugin.target_unified_msg_origins = ['aiocqhttp:GroupMessage:fixture']
        with tempfile.NamedTemporaryFile(suffix='.gif', delete=False) as file:
            path = file.name
        with patch.object(self.plugin, 'get_cache_apod', AsyncMock(return_value=data)), patch.object(self.plugin, '_resolve_external_media', AsyncMock(return_value=('https://example.com/original.gif', 'gif'))), patch.object(self.plugin, '_download_gif', AsyncMock(return_value=path)):
            await self.plugin._run_push_once()
        message = self.context.send_message.call_args.args[1]
        self.assertEqual(message.chain[0].file, path)
        self.assertFalse(Path(path).exists())
        cached = await self.plugin.get_cache(APOD._build_push_payload_cache_key(data['date']))
        self.assertEqual(cached['media_type'], 'gif')
        self.assertEqual(cached['url'], 'https://example.com/original.gif')
        self.assertTrue(cached['external_url'])
        self.assertEqual(await self.plugin._get_or_build_push_payload(data, data['date']), cached)

    async def test_external_resolve_failure_keeps_thumbnail_and_link(self):
        fixture = json.loads((ROOT / 'tests/fixtures/nasa_external_gif.json').read_text())
        with patch.object(self.plugin, '_resolve_external_media', AsyncMock(return_value=None)):
            payload = await self.plugin._build_display_payload(APOD._normalize_apod_data(fixture))
        chain = self.plugin._build_chain_from_payload(payload)
        self.assertEqual(chain[0].file, fixture['hdurl'])
        self.assertIn('www.esa.int', chain[1].text)

    async def test_random_command_fetches_history_without_overwriting_daily_cache(self):
        daily = await self.plugin.get_cache_apod()
        self.payload = dict(SAMPLE)
        offset = (datetime.strptime(SAMPLE['date'], '%Y-%m-%d').date() - datetime(1995, 6, 16).date()).days
        event = AstrMessageEvent.__new__(AstrMessageEvent)
        event.session = 'aiocqhttp:GroupMessage:fixture'
        event.send = AsyncMock()
        with patch.object(module.random, 'randint', return_value=offset):
            self.assertEqual([r async for r in self.plugin.apod_random(event)], [])
        self.assertEqual(self.calls[-1], '/141001')
        self.assertEqual(await self.plugin.get_cache(APOD.APOD_CACHE_KEY), daily)
        self.assertIsNone(await self.plugin.get_cache(APOD.PUSH_LAST_SENT_DATE_KEY))
        chain = event.send.call_args.args[0].chain
        self.assertEqual(chain[0].file, SAMPLE['hdurl'])
        self.assertIn(SAMPLE['date'], chain[2].text)

    async def test_random_calls_fetch_each_time_and_use_archive_bounds(self):
        last = (datetime.now(ZoneInfo('America/New_York')).date() - datetime(1995, 6, 16).date()).days
        with patch.object(module.random, 'randint', side_effect=[0, last]) as pick:
            self.assertIsNotNone(await self.plugin.get_random_apod())
            self.assertIsNotNone(await self.plugin.get_random_apod())
        self.assertEqual(pick.call_args_list[0].args, (0, last))
        self.assertEqual(self.calls, ['/950616', '/' + datetime.now(ZoneInfo('America/New_York')).strftime('%y%m%d')])

    async def test_random_missing_dates_reselects_with_limit(self):
        self.status = 404
        with patch.object(module.random, 'randint', side_effect=[0, 1, 2]):
            self.assertIsNone(await self.plugin.get_random_apod())
        self.assertEqual(self.calls, ['/950616', '/950617', '/950618'])
        self.assertEqual(self.plugin.last_apod_status, 404)

    async def test_random_access_denial_does_not_reselect(self):
        self.status = 403
        self.assertIsNone(await self.plugin.get_random_apod())
        self.assertEqual(len(self.calls), 1)
        self.assertIn('访问被拒绝', self.plugin.last_apod_error)

    async def test_random_failure_returns_feedback(self):
        self.status = 403
        event = AstrMessageEvent.__new__(AstrMessageEvent)
        result = [r async for r in self.plugin.apod_random(event)]
        self.assertIn('访问被拒绝', result[0].chain[0].text)

    async def test_random_video_reuses_download_and_cleanup(self):
        data = APOD._normalize_apod_data(json.loads((ROOT / 'tests/fixtures/nasa_video.json').read_text()))
        self.plugin.video_download = True
        event = AstrMessageEvent.__new__(AstrMessageEvent)
        event.session = 'aiocqhttp:GroupMessage:fixture'
        event.send = AsyncMock()
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as file:
            path = file.name
        with patch.object(self.plugin, 'get_random_apod', AsyncMock(return_value=data)), patch.object(self.plugin, '_download_video', AsyncMock(return_value=path)) as download:
            self.assertEqual([r async for r in self.plugin.apod_random(event)], [])
        download.assert_awaited_once_with(data['media_url'])
        self.assertTrue(any(isinstance(item, module.Comp.Video) for item in event.send.call_args.args[0].chain))
        self.assertFalse(Path(path).exists())

    async def test_video_without_source_reports_error(self):
        self.payload.update(media_type='video', hdurl='', basic_html='')
        event = AstrMessageEvent.__new__(AstrMessageEvent)
        result = [r async for r in self.plugin.apod(event)]
        self.assertIn('视频链接失败', result[0].chain[0].text)

    async def test_reply_send_failure_logged(self):
        event = AstrMessageEvent.__new__(AstrMessageEvent)
        event.session = 'aiocqhttp:GroupMessage:fixture'
        event.send = AsyncMock(side_effect=asyncio.TimeoutError())
        with patch.object(module.logger, 'error') as log:
            result = [r async for r in self.plugin.apod(event)]
        self.assertIn('发送失败', result[0].chain[0].text)
        text = log.call_args.args[0]
        self.assertIn('stage=send', text)
        self.assertIn('TimeoutError', text)
        self.assertIn('elapsed=', text)
        self.assertIn('media=image', text)

    async def test_push_send_failure_logged_and_not_marked(self):
        self.plugin.target_unified_msg_origins = ['aiocqhttp:GroupMessage:fixture']
        self.context.send_message.side_effect = OSError('adapter failed')
        with patch.object(module.logger, 'error') as log:
            await self.plugin._run_push_once()
        self.assertIn('stage=send', log.call_args.args[0])
        self.assertIsNone(await self.plugin.get_cache(APOD.PUSH_LAST_SENT_DATE_KEY))

    async def test_video_download_and_file_lifetime_during_send(self):
        self.payload = json.loads((ROOT / 'tests/fixtures/nasa_video.json').read_text())
        self.payload['date'] = datetime.now(ZoneInfo('America/New_York')).date().isoformat()
        self.plugin.video_download = True
        event = AstrMessageEvent.__new__(AstrMessageEvent)
        event.session = 'aiocqhttp:GroupMessage:fixture'
        paths = []
        async def send(message):
            for item in message.chain:
                if isinstance(item, module.Comp.Video):
                    paths.append(item.path)
                    self.assertTrue(Path(item.path).exists())
        event.send = AsyncMock(side_effect=send)
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as file:
            file.write(b'fixture')
            path = file.name
        with patch.object(self.plugin, '_download_video', AsyncMock(return_value=path)) as download:
            self.assertEqual([r async for r in self.plugin.apod(event)], [])
        self.assertEqual(paths, [path])
        self.assertFalse(Path(path).exists())
        self.assertTrue(download.call_args.args[0].endswith('.mp4'))



class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = APOD(None, {'video': {'download': True}, 'push': {'enabled': False}})
        await self.plugin.initialize()
        self.created = []
        mkstemp = module.tempfile.mkstemp
        def tracked(*args, **kwargs):
            fd, path = mkstemp(*args, **kwargs)
            self.created.append(path)
            return fd, path
        self.files = patch.object(module.tempfile, 'mkstemp', tracked)
        self.files.start()
        self.first_chunk = asyncio.Event()
        output = io.BytesIO()
        frames = [PILImage.new('RGB', (8, 8), color) for color in ['red', 'blue']]
        frames[0].save(output, format='GIF', save_all=True, append_images=frames[1:], duration=100, loop=0)
        self.gif_bytes = output.getvalue()
        async def handler(request):
            if request.path == '/page':
                return web.Response(text='<img src="preview.gif"><a href="original.gif">Download original</a>', content_type='text/html')
            if request.path == '/movie-page':
                return web.Response(text='<video><source src="/download" type="video/mp4"></video>', content_type='text/html')
            if request.path == '/oversize-page':
                return web.Response(text='x' * (2 * 1024 * 1024 + 1), content_type='text/html')
            if request.path == '/original.gif':
                return web.Response(body=self.gif_bytes, content_type='image/gif')
            if request.path == '/invalid.gif':
                return web.Response(body=b'not a gif', content_type='image/gif')
            if request.path == '/html':
                return web.Response(text='<html>not video</html>', content_type='text/html')
            if request.path == '/large':
                return web.Response(body=b'x' * (1024 * 1024 + 1), content_type='video/mp4')
            if request.path == '/empty':
                return web.Response(body=b'', content_type='video/mp4')
            if request.path == '/slow':
                response = web.StreamResponse(headers={'Content-Type': 'video/mp4'})
                await response.prepare(request)
                await response.write(b'x' * 128)
                self.first_chunk.set()
                await asyncio.sleep(0.1)
                try:
                    await response.write(b'y')
                except ConnectionResetError:
                    pass
                return response
            return web.Response(body=b'exact-video-download-bytes', content_type='video/mp4')
        app = web.Application()
        app.router.add_get('/{path}', handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.base = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'

    async def asyncTearDown(self):
        self.files.stop()
        await self.runner.cleanup()
        for path in self.created:
            self.plugin._remove_temp_file(path)
        await self.plugin.terminate()

    async def test_external_page_prefers_original_gif_link(self):
        result = await self.plugin._resolve_external_media(self.base + '/page')
        self.assertEqual(result, (self.base + '/original.gif', 'gif'))

    async def test_html5_source_without_extension(self):
        result = await self.plugin._resolve_external_media(self.base + '/movie-page')
        self.assertEqual(result, (self.base + '/download', 'video'))

    async def test_oversize_external_page_is_bounded(self):
        with patch.object(module.logger, 'error') as log:
            self.assertIsNone(await self.plugin._resolve_external_media(self.base + '/oversize-page'))
        self.assertIn('stage=external_resolve', log.call_args.args[0])

    async def test_external_disabled_prevents_even_cached_gif_download(self):
        self.plugin.external_media_enabled = False
        with patch.object(module.aiohttp, 'ClientSession') as session:
            self.assertIsNone(await self.plugin._download_gif(self.base + '/original.gif'))
        session.assert_not_called()

    async def test_gif_download_and_onebot_serialization_preserve_frames(self):
        path = await self.plugin._download_gif(self.base + '/original.gif')
        self.assertIsNotNone(path)
        self.assertEqual(Path(path).read_bytes(), self.gif_bytes)
        payload = await AiocqhttpMessageEvent._from_segment_to_dict(module._LoggedImage(file=path))
        preserved = base64.b64decode(payload['data']['file'].removeprefix('base64://'))
        self.assertEqual(preserved, self.gif_bytes)
        with PILImage.open(io.BytesIO(preserved)) as image:
            self.assertEqual(image.n_frames, 2)

    async def test_invalid_gif_signature_is_rejected_and_cleaned(self):
        with patch.object(module.logger, 'error') as log:
            self.assertIsNone(await self.plugin._download_gif(self.base + '/invalid.gif'))
        self.assertIn('stage=gif_download', log.call_args.args[0])
        self.assertTrue(all(not Path(p).exists() for p in self.created))

    async def test_download_exact_bytes(self):
        path = await self.plugin._download_video(self.base + '/direct.mp4')
        self.assertIsNotNone(path)
        self.assertEqual(Path(path).read_bytes(), b'exact-video-download-bytes')

    async def test_timeout_reports_progress_and_cleans_partial_file(self):
        self.plugin.video_download_timeout = 0.03
        with patch.object(module.logger, 'error') as log:
            self.assertIsNone(await self.plugin._download_video(self.base + '/slow'))
        text = log.call_args.args[0]
        self.assertIn('stage=video_download', text)
        self.assertIn('TimeoutError', text)
        self.assertIn('bytes=128', text)
        self.assertIn('timeout=0.03s', text)
        self.assertTrue(self.created)
        self.assertTrue(all(not Path(p).exists() for p in self.created))

    async def test_cancel_cleans_partial_file(self):
        task = asyncio.create_task(self.plugin._download_video(self.base + '/slow'))
        await self.first_chunk.wait()
        # The stream has delivered its first chunk; wait for the writer to run.
        for _ in range(10):
            if self.created:
                break
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(all(not Path(p).exists() for p in self.created))

    async def test_html_empty_and_size_limit_are_reported(self):
        self.plugin.video_max_download_mb = 1
        for route in ['/html', '/empty', '/large']:
            with self.subTest(route=route), patch.object(module.logger, 'error') as log:
                self.assertIsNone(await self.plugin._download_video(self.base + route))
                self.assertIn('stage=video_download', log.call_args.args[0])
        self.assertTrue(all(not Path(p).exists() for p in self.created))

    async def test_external_page_skips_download_with_reason(self):
        with patch.object(module.logger, 'info') as log:
            self.assertIsNone(await self.plugin._download_video('https://www.youtube.com/watch?v=example'))
        self.assertIn('reason=external_page', log.call_args.args[0])

    async def test_image_prepare_failure_is_logged(self):
        image = module._LoggedImage(file='https://example.com/image.jpg?secret=private')
        with patch.object(Image, 'convert_to_base64', AsyncMock(side_effect=asyncio.TimeoutError())), patch.object(module.logger, 'error') as log:
            with self.assertRaises(asyncio.TimeoutError):
                await image.convert_to_base64()
        self.assertIn('stage=image_prepare', log.call_args.args[0])
        self.assertIn('TimeoutError', log.call_args.args[0])
        self.assertNotIn('secret=', log.call_args.args[0])



if __name__ == '__main__':
    unittest.main()
