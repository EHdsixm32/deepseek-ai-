import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistant.config import ConfigManager
from assistant.judge import ActivityEvent, ImportanceJudge
from assistant.memory import MemoryManager


class ImportanceTest(unittest.TestCase):
    def test_important_event_stored(self):
        cfg = ConfigManager(Path(tempfile.mkdtemp()) / 'c.json')
        j = ImportanceJudge(cfg)
        r = j.judge(ActivityEvent(topic='重要：明天合同截止', source='chat', detail='必须记住'))
        self.assertTrue(r.should_store)
        self.assertGreater(r.weight, 0.4)

    def test_trivial_event_dropped(self):
        cfg = ConfigManager(Path(tempfile.mkdtemp()) / 'c.json')
        j = ImportanceJudge(cfg)
        r = j.judge(ActivityEvent(topic='今天天气不错', source='task_manager', duration_seconds=2))
        self.assertFalse(r.should_store)

    def test_time_decay(self):
        import datetime as dt
        from assistant.judge.importance import recency_decay
        self.assertAlmostEqual(recency_decay(0), 1.0)
        self.assertLess(recency_decay(100), recency_decay(1))

    def test_recalc_directory(self):
        root = Path(tempfile.mkdtemp())
        cfg = ConfigManager(root / 'c.json')
        mm = MemoryManager(root, cfg)
        mm.add_entry('重要项目进度', '# body', entry_type='work', importance=0.9, weight=0.9)
        j = ImportanceJudge(cfg)
        pairs = j.recalc_directory(mm.list_entries())
        self.assertGreaterEqual(len(pairs), 1)


class MemoryCommandTest(unittest.TestCase):
    def test_remember_command_always_stored(self):
        from assistant.config import ConfigManager
        from assistant.judge import ActivityEvent, ImportanceJudge
        cfg = ConfigManager(Path(tempfile.mkdtemp()) / 'c.json')
        r = ImportanceJudge(cfg).judge(ActivityEvent(topic='请记住我的项目叫星海', source='chat'))
        self.assertTrue(r.should_store)


if __name__ == '__main__':
    unittest.main()
