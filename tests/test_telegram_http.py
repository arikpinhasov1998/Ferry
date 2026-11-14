import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ferry.telegram import TelegramApi


@pytest.fixture
def mock_telegram():
    calls = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            method = self.path.rsplit("/", 1)[-1]
            calls.append((method, self.headers["Content-Type"], body))
            if method == "badMethod":
                res, code = {"ok": False, "error_code": 409, "description": "Conflict: other getUpdates"}, 409
            else:
                res, code = {"ok": True, "result": {"message_id": 1, "method": method}}, 200
            data = json.dumps(res).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    api = TelegramApi("123:abc")
    api.base = f"http://127.0.0.1:{srv.server_address[1]}/bot123:abc/"
    yield api, calls
    srv.shutdown()
    srv.server_close()


def test_send_message_with_buttons(mock_telegram):
    api, calls = mock_telegram
    api.send(42, "<b>hi</b> " + "x" * 5000, [[("⏸ Pause", "p:run1"), ("⏹ Cancel", "c:run1")]])
    method, ctype, body = calls[-1]
    payload = json.loads(body)
    assert method == "sendMessage" and ctype == "application/json"
    assert payload["chat_id"] == 42 and payload["parse_mode"] == "HTML"
    assert len(payload["text"]) == 4096
    assert payload["reply_markup"]["inline_keyboard"][0][1] == {"text": "⏹ Cancel", "callback_data": "c:run1"}


def test_send_document_multipart(mock_telegram):
    api, calls = mock_telegram
    api.send_document(42, "ferry.log", b"line1\nline2\n", "caption <b>x</b>")
    method, ctype, body = calls[-1]
    assert method == "sendDocument" and ctype.startswith("multipart/form-data; boundary=")
    boundary = ctype.split("boundary=")[1].encode()
    assert body.count(b"--" + boundary) == 5
    assert b'filename="ferry.log"' in body and b"line1\nline2\n" in body and b'name="chat_id"\r\n\r\n42' in body


def test_error_code(mock_telegram):
    api, calls = mock_telegram
    with pytest.raises(RuntimeError) as e:
        api.call("badMethod")
    assert e.value.code == 409 and "Conflict" in str(e.value)
