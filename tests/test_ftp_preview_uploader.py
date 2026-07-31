from pathlib import Path

from services.ftp_preview_uploader import FtpPreviewUploader


class FakeFtp:
    def __init__(self):
        self.calls = []
        self.uploaded = []

    def connect(self, host, timeout):
        self.calls.append(("connect", host, timeout))

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def cwd(self, path):
        self.calls.append(("cwd", path))

    def storbinary(self, command, source):
        self.calls.append(("storbinary", command))
        self.uploaded.append((command, source.read()))

    def quit(self):
        self.calls.append(("quit",))


def test_uploads_multiple_previews_with_one_ftp_connection(tmp_path: Path):
    first = tmp_path / "first_preview.png"
    second = tmp_path / "second_preview.png"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    ftp = FakeFtp()
    uploader = FtpPreviewUploader(
        "ftp.example.test",
        "user",
        "password",
        remote_dir="inbox/press/",
        ftp_factory=lambda: ftp,
    )

    result = uploader.upload([first, second])

    assert result == ["first_preview.png", "second_preview.png"]
    assert ftp.calls == [
        ("connect", "ftp.example.test", 20),
        ("login", "user", "password"),
        ("cwd", "inbox/press"),
        ("storbinary", "STOR first_preview.png"),
        ("storbinary", "STOR second_preview.png"),
        ("quit",),
    ]
    assert ftp.uploaded == [
        ("STOR first_preview.png", b"first"),
        ("STOR second_preview.png", b"second"),
    ]


def test_uses_press_when_account_is_already_in_inbox(tmp_path: Path):
    preview = tmp_path / "preview.png"
    preview.write_bytes(b"preview")

    class InboxFtp(FakeFtp):
        def cwd(self, path):
            self.calls.append(("cwd", path))
            if path == "inbox/press":
                from ftplib import error_perm

                raise error_perm("550 directory missing")

    ftp = InboxFtp()
    uploader = FtpPreviewUploader(
        "ftp.example.test", "user", "password", ftp_factory=lambda: ftp
    )

    uploader.upload([preview])

    assert ("cwd", "inbox/press") in ftp.calls
    assert ("cwd", "press") in ftp.calls
