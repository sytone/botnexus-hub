"""Synthetic OpenAI-compatible provider. Test containers only; no external calls."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import time
import uuid


def response_chunks(payload):
    messages = payload.get('messages', [])
    def content(message):
        value = message.get('content', '')
        if isinstance(value, list):
            value = ' '.join(str(part.get('text','')) for part in value if isinstance(part,dict))
        return str(value)
    meaningful = [m for m in messages if not content(m).startswith('[Live tool capacity]')]
    if meaningful and meaningful[-1].get('role') == 'tool':
        answer = content(meaningful[-1])
        return [{'content': 'Fixture agent received your answer: ' + answer[:300]}], 'stop'
    user = next((content(m) for m in reversed(meaningful) if m.get('role') == 'user'), '')
    continuation = next((content(m) for m in reversed(meaningful) if content(m).startswith('[ask_user] The user answered')), '')
    if continuation:
        return [{'content': 'Fixture agent received your answer: ' + continuation[-300:]}], 'stop'
    if 'ask me' in user.lower() or 'question' in user.lower():
        names = [t.get('function', {}).get('name') for t in payload.get('tools', [])]
        if 'ask_user' not in names:
            return [{'content': 'Fixture configuration error: ask_user tool unavailable.'}], 'stop'
        args = {'prompt': 'Which launch name should this test world use?', 'input_type': 'choice_or_free_form', 'choices': [{'value': 'aurora', 'label': 'Aurora'}, {'value': 'harbor', 'label': 'Harbor'}], 'allow_multiple': False}
        return [{'tool_calls': [{'index': 0, 'id': 'call_' + uuid.uuid4().hex, 'type': 'function', 'function': {'name': 'ask_user', 'arguments': json.dumps(args)}}]}], 'tool_calls'
    return [{'content': 'Fixture agent reply: ' + user[:300]}], 'stop'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == '/health':
            value = {'status': 'ok', 'fixture': True}
        elif self.path in ('/v1/models', '/models'):
            value = {'object': 'list', 'data': [{'id': 'e2e', 'object': 'model'}]}
        else:
            self.send_error(404)
            return
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if self.path not in ('/v1/chat/completions', '/chat/completions'):
            self.send_error(404)
            return
        n = int(self.headers.get('Content-Length', '0'))
        if n > 2_000_000:
            self.send_error(413)
            return
        data = json.loads(self.rfile.read(n))
        deltas, finish = response_chunks(data)
        key = 'chatcmpl-' + uuid.uuid4().hex
        now = int(time.time())
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        for delta in [{'role': 'assistant'}, *deltas, {}]:
            terminal = not delta
            frame = {'id': key, 'object': 'chat.completion.chunk', 'created': now, 'model': 'e2e', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish if terminal else None}]}
            if terminal:
                frame['usage'] = {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30}
            self.wfile.write(('data: ' + json.dumps(frame) + '\n\n').encode())
            self.wfile.flush()
        self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
