import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from syncer.storage import ensure_storage_layout


@pytest.fixture
def layout(tmp_path):
    return ensure_storage_layout(tmp_path)


@pytest.fixture
def master_and_replica(tmp_path):
    """The ordinary one-master/one-replica pair, both already created, with
    the replica's landing subfolder for the (dir-type, "master"-named) master
    pre-created too, since check.py namespaces a dir master's content under
    <replica>/<master-basename>/.
    """
    master = tmp_path / "master"
    replica = tmp_path / "replica"
    master.mkdir()
    replica.mkdir()
    (replica / "master").mkdir()
    return master, replica


class LoopbackGitHub:
    """A 127.0.0.1 stand-in for api.github.com and its asset host: replays one
    canned response to any path and records the requests it received, so the
    update check and download run over real HTTP without touching the
    network."""

    def __init__(self):
        self.reply()
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append((self.path, self.headers))
                time.sleep(outer.delay)
                self.send_response(outer.status)
                for name, value in outer.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()

    def reply(self, status=200, body=b"{}", headers=None, delay=0.0):
        self.status, self.body, self.headers, self.delay = status, body, headers or {}, delay

    def reply_json(self, payload, **kwargs):
        self.reply(body=json.dumps(payload).encode(), **kwargs)

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def github():
    server = LoopbackGitHub()
    yield server
    server.close()
