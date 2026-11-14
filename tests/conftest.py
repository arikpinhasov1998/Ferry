import os
import socket
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def ferry_home(tmp_path, monkeypatch):
    home = tmp_path / "ferry-home"
    monkeypatch.setenv("FERRY_HOME", str(home))
    return home


def make_tree(root, files: dict):
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode()
        p.write_bytes(content)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------- tiny SFTP server ---
@pytest.fixture
def sftp_server(tmp_path):
    """An in-process SFTP server rooted at a temp folder; accepts any password."""
    paramiko = pytest.importorskip("paramiko")
    root = tmp_path / "sftp-root"
    root.mkdir()

    class Server(paramiko.ServerInterface):
        def check_auth_password(self, username, password):
            return paramiko.AUTH_SUCCESSFUL

        def get_allowed_auths(self, username):
            return "password"

        def check_channel_request(self, kind, chanid):
            return paramiko.OPEN_SUCCEEDED

    class Handle(paramiko.SFTPHandle):
        def stat(self):
            try:
                return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)

        def chattr(self, attr):
            try:
                paramiko.SFTPServer.set_file_attr(self.filename, attr)
                return paramiko.SFTP_OK
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)

    class SFTP(paramiko.SFTPServerInterface):
        def _p(self, path):
            return str(root) + self.canonicalize(path)

        def canonicalize(self, path):
            return os.path.normpath("/" + path).replace("\\", "/") if not path.startswith("/") else \
                os.path.normpath(path).replace("\\", "/")

        def list_folder(self, path):
            try:
                out = []
                for name in os.listdir(self._p(path)):
                    a = paramiko.SFTPAttributes.from_stat(os.lstat(os.path.join(self._p(path), name)))
                    a.filename = name
                    out.append(a)
                return out
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)

        def stat(self, path):
            try:
                return paramiko.SFTPAttributes.from_stat(os.stat(self._p(path)))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)

        def lstat(self, path):
            try:
                return paramiko.SFTPAttributes.from_stat(os.lstat(self._p(path)))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)

        def open(self, path, flags, attr):
            p = self._p(path)
            try:
                fd = os.open(p, flags | getattr(os, "O_BINARY", 0), 0o666)
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            if flags & os.O_WRONLY:
                mode = "ab" if flags & os.O_APPEND else "wb"
            elif flags & os.O_RDWR:
                mode = "a+b" if flags & os.O_APPEND else "r+b"
            else:
                mode = "rb"
            f = os.fdopen(fd, mode)
            h = Handle(flags)
            h.filename = p
            h.readfile = f
            h.writefile = f
            return h

        def remove(self, path):
            try:
                os.remove(self._p(path))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def rename(self, old, new):
            try:
                os.rename(self._p(old), self._p(new))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def posix_rename(self, old, new):
            try:
                os.replace(self._p(old), self._p(new))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def mkdir(self, path, attr):
            try:
                os.mkdir(self._p(path))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def rmdir(self, path):
            try:
                os.rmdir(self._p(path))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def chattr(self, path, attr):
            try:
                paramiko.SFTPServer.set_file_attr(self._p(path), attr)
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

    host_key = paramiko.RSAKey.generate(2048)
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(10)
    port = sock.getsockname()[1]
    stop = threading.Event()
    transports = []

    def serve():
        sock.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = paramiko.Transport(conn)
            t.add_server_key(host_key)
            t.set_subsystem_handler("sftp", paramiko.SFTPServer, SFTP)
            t.start_server(server=Server())
            transports.append(t)

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    os.environ["FERRY_SFTP_PASSWORD"] = "x"
    yield root, port
    stop.set()
    for t in transports:
        t.close()
    sock.close()
    os.environ.pop("FERRY_SFTP_PASSWORD", None)
