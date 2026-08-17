import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistant.config import ConfigManager
from assistant.tools import FileToolExecutor
from assistant.memory import MemoryManager
from assistant.judge import ImportanceJudge
from assistant.brain.chat_engine import ChatEngine


class FileToolTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.cfg = ConfigManager(self.root / 'config.json')
        self.cfg.set('tools.workspace_root', str(self.root), validate=False)
        self.cfg.set('tools.auto_approve', True, validate=False)

    def test_write_edit_read_roundtrip(self):
        ex = FileToolExecutor(self.cfg)
        self.assertTrue(ex.execute('write_file', {'path': 'a.txt', 'content': 'hello'})['ok'])
        self.assertEqual((self.root / 'a.txt').read_text(encoding='utf-8'), 'hello')
        self.assertTrue(ex.execute('edit_file', {'path': 'a.txt', 'old_text': 'hello', 'new_text': 'hi'})['ok'])
        result = ex.execute('read_file', {'path': 'a.txt'})
        self.assertTrue(result['ok'])
        self.assertIn('hi', result['content'])

    def test_outside_workspace_blocked(self):
        ex = FileToolExecutor(self.cfg)
        result = ex.execute('write_file', {'path': '../../evil.txt', 'content': 'x'})
        self.assertFalse(result['ok'])
        self.assertIn('超出工作区', result['error'])

    def test_approval_required(self):
        self.cfg.set('tools.auto_approve', False, validate=False)
        ex = FileToolExecutor(self.cfg, approver=lambda *a: (False, '测试拒绝'))
        result = ex.execute('write_file', {'path': 'b.txt', 'content': 'x'})
        self.assertFalse(result['ok'])
        self.assertIn('测试拒绝', result['error'])


class ToolLoopTest(unittest.TestCase):
    def test_model_can_read_file(self):
        root = Path(tempfile.mkdtemp())
        (root / 'data.txt').write_text('hello file', encoding='utf-8')
        cfg = ConfigManager(root / 'config.json')
        cfg.set('tools.workspace_root', str(root), validate=False)
        cfg.set('tools.auto_approve', True, validate=False)
        memory = MemoryManager(root / 'memory', cfg)

        class FakeHarness:
            def __init__(self): self.n = 0
            def is_configured(self): return True
            def chat_json(self, messages, **kw): return {'ids': []}
            def chat_full(self, messages, **kw):
                self.n += 1
                if self.n == 1:
                    return {'role': 'assistant', 'content': '', 'reasoning_content': '先读取文件',
                            'tool_calls': [{'id': 'c1', 'type': 'function',
                                            'function': {'name': 'read_file', 'arguments': {'path': 'data.txt'}}}]}
                return {'role': 'assistant', 'content': '文件内容是 hello file', 'reasoning_content': '', 'tool_calls': []}

        engine = ChatEngine(cfg, memory, ImportanceJudge(cfg), FakeHarness())
        events = list(engine.stream_turn('读取文件'))
        self.assertIn('tool', [e.kind for e in events])
        self.assertTrue(any(e.kind == 'answer' and 'hello file' in e.text for e in events))


if __name__ == '__main__':
    unittest.main()


class SensitiveFileTest(unittest.TestCase):
    def test_config_file_blocked_by_default(self):
        root = Path(tempfile.mkdtemp())
        (root / 'config.json').write_text('{"deepseek":{"api_key":"sk-secret"}}', encoding='utf-8')
        cfg = ConfigManager(root / 'c.json')
        cfg.set('tools.workspace_root', str(root), validate=False)
        cfg.set('tools.auto_approve', True, validate=False)
        ex = FileToolExecutor(cfg)
        result = ex.execute('read_file', {'path': 'config.json'})
        self.assertFalse(result['ok'])
        self.assertIn('敏感文件', result['error'])
