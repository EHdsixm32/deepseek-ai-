import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistant.config import ConfigManager
from assistant.memory import MemoryManager
from assistant.judge import ImportanceJudge
from assistant.brain.chat_engine import ChatEngine


class HistoryNoDuplicateTest(unittest.TestCase):
    def test_second_turn_sees_each_message_once(self):
        root = Path(tempfile.mkdtemp())
        cfg = ConfigManager(root / 'config.json')
        cfg.set('tools.enabled', False, validate=False)
        mm = MemoryManager(root / 'memory', cfg)

        class H:
            def __init__(self):
                self.calls = 0
                self.last_messages = []
            def is_configured(self): return True
            def chat_json(self, messages, **kw): return {'ids': []}
            def chat_full(self, messages, **kw):
                self.calls += 1
                self.last_messages = [dict(m) for m in messages]
                return {'role': 'assistant', 'content': '回答', 'reasoning_content': '', 'tool_calls': []}

        h = H()
        eng = ChatEngine(cfg, mm, ImportanceJudge(cfg), h)
        ans1 = ''.join(eng.stream_reply('第一个问题'))
        eng.finalize_turn('第一个问题', ans1)
        ans2 = ''.join(eng.stream_reply('第二个问题'))
        eng.finalize_turn('第二个问题', ans2)
        self.assertEqual(ans1, '回答')
        self.assertEqual(ans2, '回答')
        user_count = sum(1 for m in h.last_messages if m.get('role') == 'user' and m.get('content') == '第二个问题')
        self.assertEqual(user_count, 1)
        prev_user = sum(1 for m in h.last_messages if m.get('role') == 'user' and m.get('content') == '第一个问题')
        self.assertEqual(prev_user, 1)
        prev_assistant = sum(1 for m in h.last_messages if m.get('role') == 'assistant' and m.get('content') == '回答')
        self.assertEqual(prev_assistant, 1)


if __name__ == '__main__':
    unittest.main()
