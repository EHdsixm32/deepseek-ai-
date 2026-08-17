import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistant.config import ConfigManager
from assistant.memory import MemoryManager, MemoryEntry


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.cfg = ConfigManager(self.root / 'config.json')
        self.mm = MemoryManager(self.root, self.cfg)

    def test_initial_directory(self):
        self.assertTrue((self.root / '目录.md').exists())
        entries = self.mm.list_entries()
        self.assertGreaterEqual(len(entries), 1)

    def test_add_read_update_roundtrip(self):
        e, path = self.mm.add_entry('学习 Python 装饰器', '# 正文\n详细内容', entry_type='chat',
                                    importance=0.8, weight=0.7, tags=['学习'])
        self.assertTrue(path.exists())
        loaded = self.mm.get_entry(e.id)
        self.assertEqual(loaded.topic, '学习 Python 装饰器')
        self.assertIn('详细内容', self.mm.read_purpose(loaded))
        self.mm.update_entry_field(e.id, 'weight', 0.55)
        self.assertAlmostEqual(self.mm.get_entry(e.id).weight, 0.55)

    def test_directory_only_contains_address_not_body(self):
        self.mm.add_entry('主题A', '# 这是目的文件正文独有内容XYZ', entry_type='chat',
                          importance=0.9, weight=0.8)
        text = self.mm.read_directory_text()
        self.assertNotIn('正文独有内容XYZ', text)

    def test_path_traversal_blocked(self):
        e, _ = self.mm.add_entry('主题B', 'body', importance=0.8, weight=0.8)
        e.purpose = '../../etc/passwd'
        self.assertIsNone(self.mm.resolve_purpose_path(e))

    def test_low_importance_chat_not_stored(self):
        self.mm.set_threshold(0.9)
        e = self.mm.record_chat('随便聊聊', [{'role': 'user', 'content': 'hi'}], importance=0.2)
        self.assertIsNone(e)


if __name__ == '__main__':
    unittest.main()
