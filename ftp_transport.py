"""Single-stream passive FTP. Passwords are supplied by the caller, never saved."""
import ftplib
import re
import urllib.parse


class RestRejected(Exception):
    pass


class FTPSource:
    def __init__(self, url, username="anonymous", password=""):
        parsed = urllib.parse.urlsplit(url)
        self.host, self.port = parsed.hostname, parsed.port or 21
        self.path = urllib.parse.unquote(parsed.path)
        if not self.path or self.path.endswith("/"):
            raise ValueError("FTP 地址应指向文件，不能是目录")
        self.username, self.password = username, password
        self.ftp = ftplib.FTP(timeout=12)

    def __enter__(self):
        try:
            self.ftp.connect(self.host, self.port)
            self.ftp.login(self.username, self.password or ("flowget@" if self.username == "anonymous" else ""))
            self.ftp.set_pasv(True)
            self.ftp.voidcmd("TYPE I")
            return self
        except BaseException:
            self.ftp.close()
            raise

    def metadata(self):
        size, modified = None, ""
        try:
            size = self.ftp.size(self.path)
            if size is not None and size < 0:
                raise ValueError("FTP 服务器返回负文件大小")
        except ftplib.error_perm:
            pass
        try:
            response = self.ftp.sendcmd("MDTM " + self.path)
            if re.fullmatch(r"213 \d{14}(?:\.\d+)?", response):
                modified = response[4:]
        except ftplib.error_perm:
            pass
        return size, modified

    def probe(self):
        size, modified = self.metadata()
        rest = False
        if size is not None and modified:
            try:
                rest = self.ftp.sendcmd("REST 0").startswith("350")
            except ftplib.error_perm:
                pass
        return {"size": size, "validator": f"{self.username}:{size}:{modified}" if modified and size is not None else "",
            "validator_header": "FTP SIZE+MDTM", "range": rest, "single": True,
            "ftp_metadata": [size, modified]}

    def retrieve(self, offset, consume):
        if offset:
            try:
                self.ftp.sendcmd("REST " + str(offset))
            except ftplib.error_perm as exc:
                raise RestRejected("FTP 服务器拒绝 REST，改为从头下载") from exc
        with self.ftp.transfercmd("RETR " + self.path) as stream:
            while block := stream.recv(64 * 1024):
                consume(block)
        self.ftp.voidresp()

    def __exit__(self, *_):
        self.ftp.close()
