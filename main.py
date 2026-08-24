"""
AstrBot 插件：萌宅下载 —— 签名待补全版

【说明】
1. WebUI 填写 email / password（搜索、下载需要登录）。
2. _build_app_headers() 未实现正版 X-App 签名，会 403，需自行逆向补全。
3. 可从 GET /api/app/status → attestation 取 signSecret / pkg / ver。
4. 纯文本优先 OneBot 直发，减轻 Reply+At 导致的 NapCat 超时。
5. 建议关闭 send_as_forward，list_limit=5，并关闭「引用回复 / @发送者」。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import time
import uuid
from typing import Any, AsyncGenerator, Optional

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Node, Plain

try:
    from astrbot.api import AstrBotConfig
except ImportError:
    AstrBotConfig = dict  # type: ignore

BASE_URL = "https://cn-api.mengzhai.club"
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
TOKEN_REFRESH_AHEAD_SEC = 60
HTTP_TIMEOUT = 20.0
PLAIN_CHUNK_SIZE = 500


@register(
    "astrbot_plugin_mengzhai",
    "grok",
    "萌宅下载：搜索/最新/热门/详情/下载（签名待补全）",
    "1.1.3-unsigned",
)
class MengzhaiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self._client: Optional[httpx.AsyncClient] = None

        self._token: str = ""
        self._token_expire_ms: int = 0
        self._token_lock = asyncio.Lock()
        self._last_login_email: str = ""
        self._last_login_password: str = ""

        self._att_secret: str = ""
        self._att_pkg: str = "com.mz.game"
        self._att_ver: str = "1"
        self._att_lock = asyncio.Lock()
        self._att_fetched_at: float = 0.0

        self._cooldown: dict[str, float] = {}

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            if self.config is None:
                return default
            if isinstance(self.config, dict):
                return self.config.get(key, default)
            return getattr(self.config, key, default)
        except Exception:
            return default

    def _email(self) -> str:
        return str(self._cfg("email", "") or "").strip()

    def _password(self) -> str:
        return str(self._cfg("password", "") or "").strip()

    def _admin_only(self) -> bool:
        return bool(self._cfg("admin_only", False))

    def _cooldown_sec(self) -> int:
        try:
            return max(0, int(self._cfg("cooldown_seconds", 8) or 0))
        except Exception:
            return 8

    def _list_limit(self) -> int:
        try:
            n = int(self._cfg("list_limit", 5) or 5)
            return max(1, min(15, n))
        except Exception:
            return 5

    def _send_as_forward(self) -> bool:
        return bool(self._cfg("send_as_forward", False))

    def _has_credentials(self) -> bool:
        return bool(self._email() and self._password())

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=BASE_URL,
                timeout=HTTP_TIMEOUT,
                headers={"Accept": "application/json"},
                follow_redirects=True,
            )
        return self._client

    async def terminate(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def _check_admin(self, event: AstrMessageEvent) -> Optional[str]:
        if self._admin_only() and not event.is_admin():
            return "本插件仅管理员可用。"
        return None

    def _check_cooldown(self, event: AstrMessageEvent) -> Optional[str]:
        if event.is_admin():
            return None
        sec = self._cooldown_sec()
        if sec <= 0:
            return None
        sid = str(event.get_sender_id() or "")
        last = self._cooldown.get(sid, 0.0)
        remain = sec - (time.time() - last)
        if remain > 0:
            return f"操作过快，请 {int(remain) + 1} 秒后再试。"
        return None

    def _mark_cooldown(self, event: AstrMessageEvent) -> None:
        if event.is_admin():
            return
        if self._cooldown_sec() <= 0:
            return
        sid = str(event.get_sender_id() or "")
        if sid:
            self._cooldown[sid] = time.time()

    async def _refresh_attestation(self, force: bool = False) -> None:
        """拉取 signSecret / pkg / ver，供你自行实现签名时使用。"""
        now = time.time()
        if not force and self._att_secret and (now - self._att_fetched_at) < 300:
            return
        async with self._att_lock:
            now = time.time()
            if not force and self._att_secret and (now - self._att_fetched_at) < 300:
                return
            client = await self._get_client()
            try:
                r = await client.get(
                    "/api/app/status",
                    headers={"User-Agent": "com.mz.game/3.62", "Accept": "application/json"},
                )
                data = r.json()
            except Exception as e:
                logger.warning(f"[mengzhai] fetch attestation failed: {e}")
                return
            att = (data or {}).get("attestation") or {}
            secret = str(att.get("signSecret") or "").strip()
            if secret:
                self._att_secret = secret
            self._att_pkg = str(att.get("pkg") or "com.mz.game").strip() or "com.mz.game"
            self._att_ver = str(att.get("ver") or "1").strip() or "1"
            self._att_fetched_at = time.time()
            logger.info(
                f"[mengzhai] attestation meta pkg={self._att_pkg} ver={self._att_ver}"
            )

    def _build_app_headers(self, method: str, path: str) -> dict[str, str]:
        """
        TODO: 自行实现 X-App 签名，否则接口会 403。

        可用：
          self._att_secret / self._att_pkg / self._att_ver
        需要自行补：
          证书 SHA256、拼串顺序、HMAC 等（逆向 libmz_guard.so 或抓包）
        path 签名时一般不要带 ?query。
        """
        headers = {
            "User-Agent": f"{self._att_pkg}/3.62",
            "Accept": "application/json",
        }
        # ---- 签名占位：自行实现后取消注释并改正确 ----
        # if self._att_secret:
        #     ts = str(int(time.time()))
        #     nonce = uuid.uuid4().hex
        #     pure_path = path.split("?", 1)[0] or "/"
        #     if not pure_path.startswith("/"):
        #         pure_path = "/" + pure_path
        #     method_u = (method or "GET").upper()
        #     # msg = "??? 你自己按逆向结果拼接 ???"
        #     # auth = hmac.new(
        #     #     self._att_secret.encode("utf-8"),
        #     #     msg.encode("utf-8"),
        #     #     hashlib.sha256,
        #     # ).hexdigest()
        #     # headers.update({
        #     #     "X-App-Pkg": self._att_pkg,
        #     #     "X-App-Sig": "???证书SHA256大写???",
        #     #     "X-App-Ver": self._att_ver,
        #     #     "X-App-Ts": ts,
        #     #     "X-App-Nonce": nonce,
        #     #     "X-App-Auth": auth,
        #     # })
        return headers

    def _token_valid(self) -> bool:
        if not self._token:
            return False
        now_ms = int(time.time() * 1000)
        return now_ms < (self._token_expire_ms - TOKEN_REFRESH_AHEAD_SEC * 1000)

    async def _ensure_token(self, force: bool = False) -> str:
        email, password = self._email(), self._password()
        if not email or not password:
            raise RuntimeError("未配置萌宅账号或密码，请在 WebUI 填写。")

        if (
            not force
            and self._token_valid()
            and email == self._last_login_email
            and password == self._last_login_password
        ):
            return self._token

        async with self._token_lock:
            if (
                not force
                and self._token_valid()
                and email == self._last_login_email
                and password == self._last_login_password
            ):
                return self._token

            await self._refresh_attestation()
            client = await self._get_client()
            headers = self._build_app_headers("POST", "/api/auth/login")
            headers["Content-Type"] = "application/json"
            try:
                r = await client.post(
                    "/api/auth/login",
                    json={"email": email, "password": password},
                    headers=headers,
                )
            except httpx.TimeoutException as e:
                raise RuntimeError(f"登录超时: {e}") from e
            except httpx.HTTPError as e:
                raise RuntimeError(f"登录网络错误: {e}") from e

            try:
                data = r.json()
            except Exception as e:
                raise RuntimeError(f"登录响应非 JSON (HTTP {r.status_code})") from e

            if r.status_code >= 400 or not data.get("success"):
                err = data.get("error") or data.get("message") or r.text[:200]
                raise RuntimeError(f"登录失败: {err}")

            token = str(data.get("token") or "").strip()
            if not token:
                raise RuntimeError("登录成功但未返回 token")
            expires = data.get("expiresAt") or 0
            try:
                expires = int(expires)
            except Exception:
                expires = int(time.time() * 1000) + 3600 * 1000

            self._token = token
            self._token_expire_ms = expires
            self._last_login_email = email
            self._last_login_password = password
            logger.info("[mengzhai] login ok")
            return self._token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Any = None,
        need_auth: bool = False,
        retry_401: bool = True,
    ) -> dict:
        await self._refresh_attestation()
        client = await self._get_client()
        headers = self._build_app_headers(method, path)

        if need_auth:
            token = await self._ensure_token()
            headers["Authorization"] = f"Bearer {token}"

        try:
            r = await client.request(
                method, path, params=params, json=json_body, headers=headers
            )
        except httpx.TimeoutException as e:
            raise RuntimeError(f"请求超时: {e}") from e
        except httpx.HTTPError as e:
            raise RuntimeError(f"网络错误: {e}") from e

        if r.status_code == 401 and need_auth and retry_401:
            self._token = ""
            self._token_expire_ms = 0
            token = await self._ensure_token(force=True)
            headers = self._build_app_headers(method, path)
            headers["Authorization"] = f"Bearer {token}"
            try:
                r = await client.request(
                    method, path, params=params, json=json_body, headers=headers
                )
            except httpx.HTTPError as e:
                raise RuntimeError(f"网络错误: {e}") from e

        try:
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"响应非 JSON (HTTP {r.status_code}): {r.text[:200]}") from e

        if r.status_code == 401:
            raise RuntimeError(f"未授权 (HTTP 401): {data.get('error') or data}")
        if r.status_code == 403:
            code = data.get("code") or ""
            if code == "APP_ATTESTATION_FAILED":
                self._att_secret = ""
                self._att_fetched_at = 0
                raise RuntimeError(
                    f"完整性校验失败（请先实现 _build_app_headers）: {data.get('error') or data}"
                )
            raise RuntimeError(f"拒绝访问 (HTTP 403): {data.get('error') or data}")
        if isinstance(data, dict) and data.get("success") is False:
            code = data.get("code") or ""
            if code == "DOWNLOAD_RATE_LIMITED":
                retry = data.get("retryAfterSec") or "?"
                raise RuntimeError(f"下载限流，约 {retry} 秒后再试")
            raise RuntimeError(f"接口错误: {data.get('error') or data}")
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {data}")
        return data if isinstance(data, dict) else {"success": True, "data": data}

    @staticmethod
    def _extract_items(data: dict) -> list[dict]:
        candidates: list = []
        for key in ("items", "list", "data", "results", "records"):
            v = data.get(key)
            if isinstance(v, list):
                candidates = v
                break
            if isinstance(v, dict):
                for k2 in ("items", "list", "records"):
                    if isinstance(v.get(k2), list):
                        candidates = v[k2]
                        break
        if not candidates and isinstance(data.get("item"), dict):
            candidates = [data["item"]]
        seen, out = set(), []
        for it in candidates:
            if not isinstance(it, dict):
                continue
            sid = str(it.get("id") or "").strip()
            if sid and sid in seen:
                continue
            if sid:
                seen.add(sid)
            out.append(it)
        return out

    @staticmethod
    def _match_score(item: dict, keyword: str) -> int:
        kw = (keyword or "").strip().lower()
        if not kw:
            return 0
        title = str(item.get("title") or item.get("name") or "").lower()
        if kw in title:
            return 3
        tags = item.get("tags") or []
        tag_str = (
            " ".join(str(t) for t in tags).lower()
            if isinstance(tags, list)
            else str(tags).lower()
        )
        if kw in tag_str:
            return 2
        desc = str(
            item.get("description")
            or item.get("desc")
            or item.get("summary")
            or item.get("intro")
            or ""
        ).lower()
        return 1 if kw in desc else 0

    def _sort_search_items(self, items: list[dict], keyword: str) -> list[dict]:
        return sorted(
            items,
            key=lambda it: (-self._match_score(it, keyword), str(it.get("title") or "")),
        )

    def _format_list(self, items: list[dict], title: str, keyword: str = "") -> str:
        limit = self._list_limit()
        items = items[:limit]
        if not items:
            return f"【{title}】无结果"
        lines = [f"【{title}】{len(items)}条"]
        for i, it in enumerate(items, 1):
            name = str(it.get("title") or it.get("name") or "?").strip()
            sid = str(it.get("id") or "").strip()
            size = str(
                it.get("packageSize") or it.get("fileSize") or it.get("size") or ""
            ).strip()
            mark = {3: "*", 2: "+", 1: "\~"}.get(self._match_score(it, keyword), "")
            line = f"{i}.{name}{mark}"
            if size:
                line += f" {size}"
            lines.append(line)
            if sid:
                lines.append(sid)
        lines.append("下:/mz下载 <ID>")
        return "\n".join(lines)

    @staticmethod
    def _format_detail(item: dict) -> str:
        name = str(item.get("title") or item.get("name") or "?").strip()
        sid = str(item.get("id") or "").strip()
        size = str(
            item.get("packageSize") or item.get("fileSize") or item.get("size") or "-"
        ).strip()
        cat = str(item.get("categoryName") or item.get("category") or "-").strip()
        lines = [f"【详情】{name}", f"ID:{sid}", f"{cat} {size}"]
        if sid:
            lines.append(f"下:/mz下载 {sid}")
        return "\n".join(lines)

    @staticmethod
    def _format_download(data: dict, software_id: str) -> str:
        url = data.get("downloadUrl") or data.get("directUrl") or data.get("url") or ""
        if isinstance(url, dict):
            url = url.get("url") or url.get("downloadUrl") or ""
        url = str(url).strip()
        name = str(data.get("fileName") or data.get("title") or software_id).strip()
        size = str(
            data.get("fileSize") or data.get("packageSize") or data.get("size") or "-"
        ).strip()
        lines = [f"【下载】{name}", f"大小:{size}", f"ID:{software_id}"]
        lines.append(url if url else "无下载地址")
        return "\n".join(lines)

    @staticmethod
    def _split_text(text: str, size: int = PLAIN_CHUNK_SIZE) -> list[str]:
        text = (text or "").strip()
        if not text:
            return []
        if len(text) <= size:
            return [text]
        chunks, buf, buf_len = [], [], 0
        for line in text.split("\n"):
            add = len(line) + (1 if buf else 0)
            if buf and buf_len + add > size:
                chunks.append("\n".join(buf))
                buf, buf_len = [line], len(line)
            else:
                buf.append(line)
                buf_len += add
        if buf:
            chunks.append("\n".join(buf))
        return chunks

    async def _send_raw_plain(self, event: AstrMessageEvent, text: str) -> bool:
        """OneBot 直发纯文本，尽量不带 Reply/At。"""
        text = (text or "").strip()
        if not text:
            return False
        bot = getattr(event, "bot", None)
        if bot is None:
            return False

        chunks = self._split_text(text, PLAIN_CHUNK_SIZE)
        try:
            group_id = None
            try:
                group_id = event.get_group_id()
            except Exception:
                pass
            if not group_id:
                msg_obj = getattr(event, "message_obj", None)
                group_id = getattr(msg_obj, "group_id", None) if msg_obj else None

            for i, chunk in enumerate(chunks):
                body = chunk if len(chunks) == 1 else f"({i + 1}/{len(chunks)})\n{chunk}"
                if group_id:
                    await bot.send_group_msg(group_id=int(group_id), message=body)
                else:
                    uid = event.get_sender_id()
                    await bot.send_private_msg(user_id=int(uid), message=body)
                if i + 1 < len(chunks):
                    await asyncio.sleep(0.35)
            return True
        except Exception as e:
            logger.warning(f"[mengzhai] raw send failed: {e}")
            return False

    async def _deliver(self, event: AstrMessageEvent, text: str) -> AsyncGenerator:
        text = (text or "").strip()
        if not text:
            return

        if self._send_as_forward():
            try:
                uin_raw = str(event.get_self_id() or "").strip()
                if uin_raw and uin_raw not in ("0", "None"):
                    try:
                        uin = int(uin_raw)
                    except ValueError:
                        uin = uin_raw
                    parts = self._split_text(text, 1500)
                    nodes = [
                        Node(uin=uin, name="萌宅下载", content=[Plain(p)]) for p in parts
                    ]
                    try:
                        from astrbot.api.message_components import Nodes

                        yield event.chain_result([Nodes(nodes)])
                    except Exception:
                        yield event.chain_result(nodes)
                    return
            except Exception as e:
                logger.warning(f"[mengzhai] forward failed: {e}")

        if await self._send_raw_plain(event, text):
            try:
                if hasattr(event, "stop_event"):
                    event.stop_event()
            except Exception:
                pass
            return

        parts = self._split_text(text, PLAIN_CHUNK_SIZE)
        n = len(parts)
        for i, chunk in enumerate(parts):
            if n > 1:
                yield event.plain_result(f"({i + 1}/{n})\n{chunk}")
            else:
                yield event.plain_result(chunk)

    def _extract_keyword(self, event: AstrMessageEvent, keyword: str = "") -> str:
        kw = (keyword or "").strip()
        if kw:
            return kw
        msg = (event.message_str or "").strip()
        for prefix in ("mz搜索", "mzsearch", "MZ搜索", "/mz搜索", "/mzsearch"):
            if msg.lower().startswith(prefix.lower()):
                msg = msg[len(prefix) :].strip()
                break
        msg = re.sub(r"^/\s*", "", msg).strip()
        for prefix in ("mz搜索", "mzsearch"):
            if msg.lower().startswith(prefix.lower()):
                msg = msg[len(prefix) :].strip()
        return msg

    @filter.command("mz搜索")
    async def cmd_search(self, event: AstrMessageEvent, keyword: str = ""):
        if err := self._check_admin(event):
            yield event.plain_result(err)
            return
        if err := self._check_cooldown(event):
            yield event.plain_result(err)
            return
        kw = self._extract_keyword(event, keyword)
        if not kw:
            yield event.plain_result("用法: /mz搜索 <关键词>")
            return
        try:
            if not self._has_credentials():
                yield event.plain_result("请先在 WebUI 填写萌宅账号密码")
                return
            data = await self._request(
                "GET", "/api/software", params={"keyword": kw}, need_auth=True
            )
            items = self._sort_search_items(self._extract_items(data), kw)
            text = self._format_list(items, f"搜:{kw}", keyword=kw)
            self._mark_cooldown(event)
            async for r in self._deliver(event, text):
                yield r
        except Exception as e:
            logger.exception("[mengzhai] search error")
            yield event.plain_result(f"搜索失败: {e}")

    @filter.command("mz最新")
    async def cmd_latest(self, event: AstrMessageEvent):
        if err := self._check_admin(event):
            yield event.plain_result(err)
            return
        if err := self._check_cooldown(event):
            yield event.plain_result(err)
            return
        try:
            data = await self._request("GET", "/api/software/home/latest", need_auth=False)
            text = self._format_list(self._extract_items(data), "最新")
            self._mark_cooldown(event)
            async for r in self._deliver(event, text):
                yield r
        except Exception as e:
            logger.exception("[mengzhai] latest error")
            yield event.plain_result(f"获取最新失败: {e}")

    @filter.command("mz热门")
    async def cmd_hot(self, event: AstrMessageEvent):
        if err := self._check_admin(event):
            yield event.plain_result(err)
            return
        if err := self._check_cooldown(event):
            yield event.plain_result(err)
            return
        try:
            data = await self._request(
                "GET", "/api/software/home/most-liked", need_auth=False
            )
            text = self._format_list(self._extract_items(data), "热门")
            self._mark_cooldown(event)
            async for r in self._deliver(event, text):
                yield r
        except Exception as e:
            logger.exception("[mengzhai] hot error")
            yield event.plain_result(f"获取热门失败: {e}")

    @filter.command("mz详情")
    async def cmd_detail(self, event: AstrMessageEvent, software_id: str = ""):
        if err := self._check_admin(event):
            yield event.plain_result(err)
            return
        if err := self._check_cooldown(event):
            yield event.plain_result(err)
            return
        sid = (software_id or "").strip()
        if not sid:
            msg = (event.message_str or "").strip()
            for p in ("mz详情", "/mz详情", "mzdetail"):
                if msg.lower().startswith(p.lower()):
                    sid = msg[len(p) :].strip()
                    break
        if not sid or not UUID_RE.match(sid):
            yield event.plain_result("用法: /mz详情 <UUID>")
            return
        try:
            data = await self._request("GET", f"/api/software/{sid}", need_auth=False)
            item = data.get("item") if isinstance(data.get("item"), dict) else data
            if not isinstance(item, dict) or not item:
                items = self._extract_items(data)
                item = items[0] if items else {}
            if not item:
                yield event.plain_result("未找到")
                return
            self._mark_cooldown(event)
            async for r in self._deliver(event, self._format_detail(item)):
                yield r
        except Exception as e:
            logger.exception("[mengzhai] detail error")
            yield event.plain_result(f"详情失败: {e}")

    @filter.command("mz下载")
    async def cmd_download(self, event: AstrMessageEvent, software_id: str = ""):
        if err := self._check_admin(event):
            yield event.plain_result(err)
            return
        if err := self._check_cooldown(event):
            yield event.plain_result(err)
            return
        if not self._has_credentials():
            yield event.plain_result("请先在 WebUI 填写萌宅账号密码")
            return
        sid = (software_id or "").strip()
        if not sid:
            msg = (event.message_str or "").strip()
            for p in ("mz下载", "/mz下载", "mzdownload"):
                if msg.lower().startswith(p.lower()):
                    sid = msg[len(p) :].strip()
                    break
        if not sid or not UUID_RE.match(sid):
            yield event.plain_result("用法: /mz下载 <UUID>")
            return
        try:
            data = await self._request(
                "POST", f"/api/software/{sid}/download", json_body={}, need_auth=True
            )
            payload = data
            for key in ("data", "item", "result"):
                if isinstance(data.get(key), dict):
                    payload = {**data, **data[key]}
                    break
            self._mark_cooldown(event)
            async for r in self._deliver(event, self._format_download(payload, sid)):
                yield r
        except Exception as e:
            logger.exception("[mengzhai] download error")
            yield event.plain_result(f"下载失败: {e}")