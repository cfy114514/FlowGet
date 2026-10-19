"""Bounded, same-site HTML link discovery; no scripts, cookies or browser state."""
from collections import deque
from html.parser import HTMLParser
import mimetypes
import posixpath
import threading
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

from engine import check_url, clean_name


DEFAULT_TYPES = ".zip,.7z,.rar,.gz,.pdf,.epub,.exe,.msi,.mp4,.mkv,.webm,.mp3,.flac,.jpg,.png"


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.base = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "base" and not self.base:
            self.base = attrs.get("href", "")
        key = "href" if tag in {"a", "link"} else "src" if tag in {"img", "script", "source", "video", "audio", "iframe"} else None
        if key and attrs.get(key) and len(self.links) < 10000:
            self.links.append((attrs[key], tag in {"a", "iframe"}))


class Crawler:
    def __init__(self, url, *, depth=2, max_pages=50, extensions=DEFAULT_TYPES, same_directory=True,
            use_proxy=True, delay=0.15, max_results=1000):
        self.url = check_url(url)
        self.depth, self.max_pages = int(depth), int(max_pages)
        if not 0 <= self.depth <= 10 or not 1 <= self.max_pages <= 500:
            raise ValueError("抓取深度应为 0–10，页面数应为 1–500")
        self.extensions = {x.strip().lower() for x in extensions.replace("，", ",").split(",") if x.strip()}
        if not self.extensions or any(x != "*" and (not x.startswith(".") or "/" in x or len(x) > 20) for x in self.extensions):
            raise ValueError("文件类型请填写逗号分隔的扩展名，如 .zip,.pdf；* 表示所有类型")
        self.seed = urllib.parse.urlsplit(self.url)
        path = posixpath.normpath(urllib.parse.unquote(self.seed.path or "/"))
        self.prefix = (path.rstrip("/") + "/") if self.seed.path.endswith("/") else posixpath.dirname(path).rstrip("/") + "/"
        self.same_directory = same_directory
        self.delay, self.max_results = max(0, delay), max(1, min(1000, int(max_results)))
        self.use_proxy = use_proxy
        self.max_body, self.max_bytes = 2 * 1024**2, 20 * 1024**2
        self.items, self.errors = {}, []
        self.pages, self.received = 0, 0
        self.truncated = False
        self.policy = None

    def in_scope(self, url, *, path=True):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname != self.seed.hostname:
            return False
        seed_port = self.seed.port or (443 if self.seed.scheme == "https" else 80)
        target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        defaults = self.seed.port in (None, 80 if self.seed.scheme == "http" else 443)
        if target_port != seed_port and not (defaults and parsed.port in (None, 80 if parsed.scheme == "http" else 443)):
            return False
        if self.seed.scheme == "https" and parsed.scheme != "https":
            return False
        target_path = posixpath.normpath(urllib.parse.unquote(parsed.path or "/"))
        return not path or not self.same_directory or target_path == self.prefix.rstrip("/") or target_path.startswith(self.prefix)

    def _fetch(self, url, *, robots=False):
        crawler = self
        class ScopedRedirects(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                newurl = check_url(urllib.parse.urljoin(req.full_url, newurl))
                if not crawler.in_scope(newurl, path=not robots):
                    raise ValueError("跳转超出抓取范围：" + newurl)
                if not robots and crawler.policy and not crawler.policy.can_fetch("FlowGet", newurl):
                    raise ValueError("跳转目标被 robots.txt 禁止")
                if urllib.parse.urlsplit(req.full_url).scheme == "https" and not newurl.startswith("https:"):
                    raise ValueError("已拒绝 HTTPS 降级跳转")
                return super().redirect_request(req, fp, code, msg, headers, newurl)
        opener = urllib.request.build_opener(ScopedRedirects(), *([] if self.use_proxy else [urllib.request.ProxyHandler({})]))
        return opener.open(urllib.request.Request(url, headers={"User-Agent": "FlowGet/0.3", "Accept-Encoding": "identity"}), timeout=12)

    def _robots(self):
        url = urllib.parse.urlunsplit((self.seed.scheme, self.seed.netloc, "/robots.txt", "", ""))
        policy = urllib.robotparser.RobotFileParser()
        try:
            with self._fetch(url, robots=True) as response:
                body = response.read(512 * 1024 + 1)
                if len(body) > 512 * 1024:
                    raise ValueError("robots.txt 超出 512 KB 限制")
                policy.parse(body.decode("utf-8", errors="replace").splitlines())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                policy.parse([])
            elif exc.code in (401, 403):
                policy.parse(["User-agent: *", "Disallow: /"])
            else:
                raise
        return policy

    def _matches(self, url, content_type=""):
        extension = posixpath.splitext(urllib.parse.unquote(urllib.parse.urlsplit(url).path))[1].lower()
        guessed = mimetypes.guess_all_extensions(content_type) if content_type else []
        return "*" in self.extensions or extension in self.extensions or bool(self.extensions.intersection(guessed))

    def _add(self, url, source, kind="链接"):
        if url in self.items:
            return
        if len(self.items) >= self.max_results:
            self.truncated = True
            return
        self.items[url] = {"url": url, "name": clean_name(urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])),
            "source": source, "kind": kind}

    def report(self, stop):
        return {"items": list(self.items.values()), "pages": self.pages, "bytes": self.received,
            "errors": self.errors[:100], "cancelled": stop.is_set(), "truncated": self.truncated}

    def run(self, stop=None, on_update=None):
        stop = stop or threading.Event()
        if stop.is_set():
            return self.report(stop)
        try:
            policy = self._robots()
            self.policy = policy
        except Exception as exc:
            self.errors.append("无法读取 robots.txt，未开始抓取：" + str(exc))
            return self.report(stop)
        if not policy.can_fetch("FlowGet", self.url):
            self.errors.append("起始页面被 robots.txt 禁止，未开始抓取")
            return self.report(stop)
        pending, seen, scheduled = deque([(self.url, 0)]), set(), {self.url}
        def permitted(url):
            return self.in_scope(url) and policy.can_fetch("FlowGet", url)
        delay = max(self.delay, policy.crawl_delay("FlowGet") or 0)
        while pending and not stop.is_set():
            if self.pages >= self.max_pages or self.received >= self.max_bytes or len(self.items) >= self.max_results:
                self.truncated = True
                break
            url, level = pending.popleft()
            if url in seen or not permitted(url):
                continue
            seen.add(url)
            self.pages += 1
            try:
                with self._fetch(url) as response:
                    final = check_url(response.geturl())
                    if not permitted(final):
                        continue
                    seen.add(final)
                    kind = response.headers.get_content_type()
                    if self._matches(final, kind):
                        self._add(final, url, kind)
                    if kind not in {"text/html", "application/xhtml+xml"}:
                        continue
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise ValueError("网页返回压缩内容，未解析")
                    body = bytearray()
                    while not stop.is_set():
                        block = response.read1(min(64*1024, self.max_body + 1 - len(body)))
                        if not block:
                            break
                        body.extend(block)
                        self.received += len(block)
                        if len(body) > self.max_body or self.received > self.max_bytes:
                            self.truncated = True
                            raise ValueError("网页超过抓取大小预算，未解析")
                    if stop.is_set():
                        break
                    encoding = response.headers.get_content_charset() or "utf-8"
                    parser = Links()
                    parser.feed(body.decode(encoding, errors="replace"))
                    base = urllib.parse.urljoin(final, parser.base) if parser.base else final
                    for raw, navigation in parser.links:
                        try:
                            link = check_url(urllib.parse.urljoin(base, raw))
                            if not permitted(link):
                                continue
                            if self._matches(link):
                                self._add(link, final)
                            ext = posixpath.splitext(urllib.parse.urlsplit(link).path)[1].lower()
                            if navigation and level < self.depth and ext in {"", ".html", ".htm", ".xhtml", ".php", ".asp", ".aspx"} and link not in scheduled:
                                if len(scheduled) < self.max_pages * 10:
                                    pending.append((link, level+1))
                                    scheduled.add(link)
                                else:
                                    self.truncated = True
                        except (ValueError, UnicodeError):
                            continue
            except Exception as exc:
                if len(self.errors) < 100:
                    self.errors.append(url + "：" + str(exc))
            finally:
                if on_update:
                    on_update(self.report(stop))
            if pending:
                stop.wait(delay)
        return self.report(stop)
