import asyncio
import time
import unittest
from pipeline.association_scheduler import choose, render, actually_mentioned, lookup_status

class TurnBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_unfinished_source_does_not_block_ready_one(self):
        f = asyncio.get_running_loop().create_future()
        ready = asyncio.get_running_loop().create_future()
        ready.set_result(dict(event_id="event",use="mention",source_window="原档",excerpt="原档"))
        got = await choose((f,ready),(),time.monotonic()+.05)
        self.assertEqual(got["_assoc_origin"],"current_query")
        f.cancel()

    async def test_expired_wait_and_duplicate(self):
        f=asyncio.get_running_loop().create_future()
        t=time.monotonic()
        self.assertIsNone(await choose((f,None),(),t+.02))
        self.assertLess(time.monotonic()-t,.18)
        f.set_result(dict(event_id="dup",use="mention",source_window="text"))
        self.assertIsNone(await choose((f,None),("dup",),time.monotonic()+.05))

    async def test_source_capsule_and_no_unearned_credit(self):
        item=dict(date="2040-04-04",excerpt="给样件盖章",source_window="灯变蓝以后，给样件盖章，再把样件放回隔板。", use="mention")
        s=render(item)
        self.assertIn("放回隔板",s)
        self.assertLessEqual(len(s),300)
        self.assertFalse(actually_mentioned(item,"", "嗯，知道了。"))
        self.assertFalse(render({**item,"excerpt":"编造的时间"}))
        self.assertNotIn("样件",render({**item,"use":"understand"}))


    async def test_paraphrased_event_with_distinctive_anchors(self):
        item={"use":"mention","source_window":"4月16日，陶瓷样块通过 Prism 耐温检测。",
              "excerpt":"陶瓷样块通过"}
        self.assertTrue(actually_mentioned(item,"在哪天？","那次 Prism 检测记录的是4月16日。"))
        self.assertFalse(actually_mentioned(item,"Prism，4月16日？","Prism，4月16日。"))

    def test_lookup_status_reports_only_the_fact(self):
        self.assertEqual(lookup_status(),"这轮已经查过。")
        item={"use":"mention","date":"2026-01-01","excerpt":"引用","source_window":"长一点的原文引用"}
        self.assertEqual(render(item).splitlines()[-1],lookup_status())
