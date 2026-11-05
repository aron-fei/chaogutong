# -*- coding: utf-8 -*-
"""market-monitor 最小回归测试集（体检 P1-5）。

运行：
    /opt/homebrew/anaconda3/bin/python3 -m unittest discover -s tests -v
约束：不触发真实下单、不依赖外网行情（网络相关一律 mock 或走本地缓存），
     整套应在 15 秒内跑完，适合每次改动后作为「交付全绿」的判据。
"""
import ast
import contextlib
import io
import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestStaticRules(unittest.TestCase):
    def test_no_silent_except(self):
        """交付铁律：全项目不得残留 except ...: pass（30 处已清零）。"""
        pat = re.compile(r'except[^\n]*:\s*\n\s*pass\b')
        bad = []
        for p in ROOT.glob('*.py'):
            if '.bak' in p.name:
                continue
            if pat.search(p.read_text(encoding='utf-8', errors='ignore')):
                bad.append(p.name)
        self.assertEqual(bad, [], f"仍有静默吞异常: {bad}")

    def test_all_python_parse(self):
        for p in ROOT.glob('*.py'):
            if '.bak' in p.name:
                continue
            ast.parse(p.read_text(encoding='utf-8', errors='ignore'), filename=p.name)

    def test_no_secret_in_gitignore_gaps(self):
        gi = (ROOT / '.gitignore').read_text(encoding='utf-8')
        self.assertIn('data/access_key.json', gi)
        self.assertIn('portfolio.json', gi)
        self.assertIn('.workbuddy/', gi)


class TestLogUtil(unittest.TestCase):
    def test_quiet_warn_suppresses(self):
        import log_util
        log_util._last.clear()
        log_util._suppressed.clear()
        tag = 'unittest.tag'
        for _ in range(4):
            log_util.quiet_warn(tag, ValueError('boom'))
        self.assertEqual(log_util._suppressed.get(tag), 3,
                         '同一 tag 60s 内应只打印一次')


class TestThemeBoard(unittest.TestCase):
    def test_auto_name_themes_whitelist(self):
        import theme_board
        fake = [{'name': '三羊马', 'code': '001317'},
                {'name': '羊绒股份', 'code': '600001'},
                {'name': '金羊科技', 'code': '300001'},
                {'name': '特力A', 'code': '000001'},
                {'name': '特发信息', 'code': '000070'},
                {'name': '和而泰', 'code': '002402'},
                {'name': '和顺电气', 'code': '300141'}]
        got = theme_board.auto_name_themes(fake)
        self.assertIn('羊字辈', got)
        self.assertEqual(len(got['羊字辈']), 3)
        self.assertNotIn('特字辈', got, '公司名常用字不应被误判为字辈')
        self.assertNotIn('和字辈', got)

    def test_pseudo_concept_filter(self):
        import theme_board
        for nm in ['连续亏损', '最近多板', '历史新高', '昨日首板', '高市盈率']:
            self.assertTrue(theme_board._is_pseudo(nm), nm)
        for nm in ['商业航天', '光通信模块', '固态电池', '人形机器人', 'CPO概念']:
            self.assertFalse(theme_board._is_pseudo(nm), nm)

    def test_rule_themes(self):
        import theme_board
        with patch.object(theme_board.config, 'ZT_THEME_RULES',
                          [{'name': '航天系', 'name_kw': ['航天'], 'codes': ['600118']}]):
            got = theme_board.rule_themes([
                {'name': '航天电子', 'code': '600879'},
                {'name': '中国卫星', 'code': '600118'},
                {'name': '贵州茅台', 'code': '600519'},
            ])
            self.assertIn('航天系', got)
            self.assertEqual(len(got['航天系']), 2)


class TestApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app
        cls.app = app
        cls.client = app.app.test_client()

    def test_gate_never_echoes_key(self):
        """P0-1：非回环 403 页面不得回显 ACCESS_KEY。"""
        r = self.client.get('/', environ_base={'REMOTE_ADDR': '192.168.9.9'})
        self.assertEqual(r.status_code, 403)
        body = r.get_data(as_text=True)
        self.assertNotIn(self.app.ACCESS_KEY, body)
        self.assertIn('访问受限', body)

    def test_gate_api_403_json(self):
        r = self.client.get('/api/health', environ_base={'REMOTE_ADDR': '192.168.9.9'})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json().get('ok'), False)

    def test_gate_loopback_ok(self):
        r = self.client.get('/api/health', environ_base={'REMOTE_ADDR': '127.0.0.1'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json().get('ok'))

    def test_health_extended_shape(self):
        """P2-4：健康端点要能反映线程/数据/缓存/题材状态。"""
        d = self.client.get('/api/health').get_json()
        for k in ('threads', 'data', 'theme', 'slow_cache',
                  'suppressed_exceptions', 'uptime_s'):
            self.assertIn(k, d, k)
        self.assertIn('last_update', d['data'])

    def test_slow_cache_hit_and_stale(self):
        calls = {'n': 0}

        def producer():
            calls['n'] += 1
            if calls['n'] == 1:
                return {'v': 1}
            raise RuntimeError('upstream down')

        p1, c1 = self.app._cached('ut.key', 60, producer, force=True)
        self.assertEqual(c1, 200)
        self.assertEqual(p1['v'], 1)
        p2, c2 = self.app._cached('ut.key', 60, producer)
        self.assertTrue(p2['_cache']['hit'])
        self.assertEqual(calls['n'], 1, 'TTL 内不应重复调用 producer')
        # 预期会走失败降级分支，屏蔽其告警输出，保持测试日志干净
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            p3, c3 = self.app._cached('ut.key', 0, producer)
        self.assertEqual(c3, 200)
        self.assertTrue(p3['_cache']['stale'], '生成失败应回吐上一次成功值')
        self.assertIn('_warning', p3)
        self.assertEqual(p3['v'], 1)

    def test_review_routes_cached(self):
        with patch.object(self.app.review, 'review', return_value={'ok': True}) as m:
            a = self.client.get('/api/review').get_json()
            b = self.client.get('/api/review').get_json()
            self.assertEqual(a.get('ok'), True)
            self.assertTrue(b.get('_cache', {}).get('hit'))
            self.assertEqual(m.call_count, 1, '第二次必须命中缓存')

    def test_zt_theme_groups_sorted_by_count(self):
        """用户要求：涨停家数最多的板块排最前；一股可属多个题材。"""
        fake = {
            'zt_pool_all': [
                {'code': '000001', 'name': '甲', 'industry': '光伏设备', 'lianban': 1, 'pct': 10},
                {'code': '000002', 'name': '乙', 'industry': '光伏设备', 'lianban': 2, 'pct': 10},
                {'code': '000003', 'name': '丙', 'industry': '半导体', 'lianban': 4, 'pct': 10},
            ],
        }
        with patch.object(self.app.tboard, 'themes_for',
                          side_effect=lambda c: {'000001': ['光伏概念', '商业航天'],
                                                 '000002': ['光伏概念'],
                                                 '000003': []}[c]), \
             patch.object(self.app, '_build_zt_board_groups', return_value=[]):
            groups = self.app._build_zt_theme_groups(fake, {})
        self.assertGreaterEqual(len(groups), 2)
        self.assertEqual(groups[0]['name'], '光伏概念')
        self.assertEqual(groups[0]['count'], 2)
        self.assertEqual(groups[0]['max_lianban'], 2)
        # 未命中题材的股票必须行业兜底，不能丢票
        self.assertTrue(any(g['kind'] == 'industry' for g in groups))

    def test_dashboard_has_theme_payload(self):
        d = self.client.get('/api/dashboard').get_json()
        for k in ('zt_board_groups', 'zt_theme_groups', 'theme_status'):
            self.assertIn(k, d, k)


if __name__ == '__main__':
    unittest.main(verbosity=2)
