import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistant.app import build_context
from assistant.config import ConfigManager
from assistant.judge import ImportanceJudge
from assistant.memory import MemoryManager


class FakeHarness:
    configured = True
    def is_configured(self): return True
    def chat_json(self, messages, **kwargs): return {'ids': ['X']}
    def chat(self, messages, **kwargs):
        if kwargs.get('stream'):
            return iter(['好的', '，我记得。'])
        return 'ok'


class FakeChatEngine:
    def __init__(self, memory):
        self.memory = memory
        self.harness = FakeHarness()
        self.history = []
        self.config = ConfigManager(Path(tempfile.mkdtemp()) / 'c.json')
        self.judge = ImportanceJudge(self.config)
    def stream_reply(self, text):
        yield '好'
    def finalize_turn(self, user, assistant):
        return {'judge': {'importance': 0.1, 'threshold': 0.45}, 'entry': None}
    def reset_conversation(self): self.history.clear()


class RetrievalTest(unittest.TestCase):
    def test_fake_roundtrip(self):
        root = Path(tempfile.mkdtemp())
        cfg = ConfigManager(root / 'c.json')
        mm = MemoryManager(root, cfg)
        e, _ = mm.add_entry('Python装饰器', '# 装饰器细节', entry_type='chat',
                            importance=0.8, weight=0.8)
        from assistant.brain.chat_engine import ChatEngine
        engine = ChatEngine(cfg, mm, ImportanceJudge(cfg), FakeHarness())
        # 模拟 LLM 检索器返回刚写入的 id
        engine.harness.chat_json = lambda messages, **kw: {'ids': [e.id]}
        directory, selected = engine.build_context('之前学的装饰器')
        self.assertIn(e.id, [x.id for x in selected])
        self.assertNotIn('装饰器细节', directory)


if __name__ == '__main__':
    unittest.main()

