import unittest
from types import SimpleNamespace
import aiosqlite
from storage.turn_store import SQLiteTurnStore

class PersistedCooldownTest(unittest.IsolatedAsyncioTestCase):
    async def test_visible_completed_turns_across_gaps_forks_and_tool_calls(self):
        async with aiosqlite.connect(":memory:") as db:
            await db.executescript("""CREATE TABLE messages(id TEXT PRIMARY KEY,parent_message_id TEXT,role TEXT,content TEXT,conversation_id TEXT, channel_id TEXT,is_proactive INTEGER);
            CREATE TABLE assistant_runs(conversation_id TEXT,assistant_message_id TEXT,status TEXT);""")
            parent=None
            async def add(mid,role,body,status=None,prev=None):
                nonlocal parent
                await db.execute("insert into messages values(?,?,?,?,?,?,0)",(mid,parent if prev is None else prev,role,body,"c","ch"))
                if status:await db.execute("insert into assistant_runs values('c',?,?)",(mid,status))
                parent=mid
            for i in range(1,8):
                await add("u"+str(i),"user","hello")
                await add("a"+str(i),"assistant",'[{"type":"tool_call"},{"type":"text","text":"ok"}]', "completed")
            await add("think","assistant",'[{"type":"thinking","text":"internal"}]',"completed")
            await add("cut","assistant","partial","cancelled")
            await add("new","user","question")
            obj=SimpleNamespace(_conn=db)
            self.assertEqual(await SQLiteTurnStore.completed_assistant_path(obj,"c","ch","new"),["a2","a3","a4","a5","a6","a7"])
            await add("fork","user","new topic",prev="a2")
            self.assertEqual(await SQLiteTurnStore.completed_assistant_path(obj,"c","ch","fork"),["a1","a2"])
