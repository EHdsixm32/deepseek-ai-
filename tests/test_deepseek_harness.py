import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistant.brain import DeepSeekHarness
from assistant.config import ConfigManager


class DeepSeekRefreshTest(unittest.TestCase):
    def test_settings_key_change_reflected_without_rebuild(self):
        cfg = ConfigManager(Path(tempfile.mkdtemp()) / 'config.json')
        cfg.set('deepseek.api_key', '')
        harness = DeepSeekHarness(cfg)
        self.assertFalse(harness.is_configured())
        cfg.set('deepseek.api_key', 'sk-test-1234567890')
        self.assertTrue(harness.is_configured())
        self.assertEqual(harness.api_key, 'sk-test-1234567890')
        cfg.set('deepseek.api_key', '')
        self.assertFalse(harness.is_configured())


if __name__ == '__main__':
    unittest.main()


class ChatFullTest(unittest.TestCase):
    def test_tool_calls_parsed_from_raw_message(self):
        from assistant.brain.deepseek_harness import DeepSeekHarness
        h = DeepSeekHarness(api_key='sk-test')
        h._request_raw = lambda body: {
            'choices': [{'message': {
                'role': 'assistant',
                'content': '我先看一下文件',
                'reasoning_content': '需要读取文件',
                'tool_calls': [{
                    'id': 'call_1', 'type': 'function',
                    'function': {'name': 'read_file', 'arguments': '{"path": "a.txt"}'},
                }],
            }}],
        }
        msg = h.chat_full([{'role': 'user', 'content': 'hi'}])
        self.assertEqual(msg['tool_calls'][0]['function']['name'], 'read_file')
        self.assertEqual(msg['tool_calls'][0]['function']['arguments'], {'path': 'a.txt'})
        self.assertIn('需要读取文件', msg['reasoning_content'])
