"""No live world access; these assertions check the synthetic fixture provider."""
import importlib.util
from pathlib import Path
import unittest
spec = importlib.util.spec_from_file_location('mock_provider', Path(__file__).with_name('mock_provider.py'))
mock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock)


class MockProviderTests(unittest.TestCase):
    def test_normal_reply(self):
        data, reason = mock.response_chunks({'messages': [{'role': 'user', 'content': 'Hello'}]})
        self.assertIn('Hello', data[0]['content'])
        self.assertEqual('stop', reason)

    def test_question_is_real_tool_call(self):
        data, reason = mock.response_chunks({'messages': [{'role': 'user', 'content': 'ask me a question'}], 'tools': [{'function': {'name': 'ask_user'}}]})
        self.assertEqual('tool_calls', reason)
        self.assertEqual('ask_user', data[0]['tool_calls'][0]['function']['name'])

    def test_answer_does_not_repeat_question(self):
        data, reason = mock.response_chunks({'messages': [{'role': 'user', 'content': 'ask me'}, {'role': 'tool', 'content': 'Aurora'}]})
        self.assertEqual('stop', reason)
        self.assertIn('Aurora', data[0]['content'])

    def test_missing_tool_is_visible_failure(self):
        data, reason = mock.response_chunks({'messages': [{'role': 'user', 'content': 'ask me'}]})
        self.assertIn('configuration error', data[0]['content'])

    def test_capacity_injection_is_not_the_users_message(self):
        data, reason = mock.response_chunks({'messages':[{'role':'user','content':'ask me a question'},{'role':'user','content':'[Live tool capacity] used=0; limit=128'}], 'tools':[{'function':{'name':'ask_user'}}]})
        self.assertEqual('tool_calls', reason)

    def test_tool_result_survives_capacity_injection(self):
        data, reason = mock.response_chunks({'messages':[{'role':'user','content':'ask me'},{'role':'tool','content':'Aurora'},{'role':'user','content':'[Live tool capacity] used=1'}]})
        self.assertEqual('stop', reason)
        self.assertIn('received your answer', data[0]['content'])

    def test_restart_continuation_is_an_answer(self):
        data, reason=mock.response_chunks({'messages':[{'role':'user','content':'[ask_user] The user answered the pending prompt "Question": Recovery Aurora'},{'role':'user','content':'[Live tool capacity] used=0'}]})
        self.assertIn('received your answer',data[0]['content'])
        self.assertIn('Recovery Aurora',data[0]['content'])


if __name__ == '__main__':
    unittest.main()
