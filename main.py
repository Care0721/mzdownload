"""
AstrBot 插件：萌宅下载 (astrbot_plugin_mengzhai) —— 签名待补全版

【使用提示】
1. WebUI 填写 email / password（搜索、下载需要登录）。
2. 当前 _build_app_headers() 未实现正版 X-App 签名，请求会返回 403
   （缺少 X-App 签名头 / APP_ATTESTATION_FAILED）。
3. 请自行逆向正版 APK（libmz_guard.so 的 mzAttestationAuth）或对照抓包，
   补全签名逻辑后再使用。
4. 可从 GET /api/app/status 的 attestation 获取 signSecret / pkg / ver。
5. 指令：/mz搜索 /mz最新 /mz热门 /mz详情 /mz下载
6. 合并转发失败时会自动回退纯文本；协议端仍发不出时请关闭 send_as_forward。
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


@register(
    "astrbot_plugin_mengzhai",
    "grok",
    "萌宅下载：搜索/最新/热门/详情/下载（签名待补全）",
    "1.1.1-unsigned",
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
            n = int(self._cfg("list_limit", 10) or 10)
            return max(1, min(30, n))
        except Exception:
            return 10

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
        """拉取 /api/app/status 中的 signSecret / pkg / ver（供你自行签名时使用）。"""
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

        需要自行完成：
        - APK 证书 SHA256（X-App-Sig）
        - 拼串顺序与分隔符
        - HMAC-SHA256(key=signSecret, msg=...) 或你逆向得到的算法
        - ts / nonce 生成规则

        可用字段（_refresh_attestation 已写入）：
          self._att_secret  ← attestation.signSecret
          self._att_pkg     ← attestation.pkg
          self._att_ver     ← attestation.ver

        path 签名时一般不要带 ?query。
        """
        headers = {
            "User-Agent": f"{self._att_pkg}/3.62",
            "Accept": "application/json",
        }
        # ---- 签名占位：请自行实现后取消注释并改正确 ----
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
            raise RuntimeError("未配置萌宅账号或密码，请在 WebUI 插件配置中填写。")

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
            err = data.get("error") or "未授权，请检查账号密码"
            raise RuntimeError(f"未授权 (HTTP 401): {err}")

        if r.status_code == 403:
            err = data.get("error") or data.get("code") or r.text[:200]
            code = data.get("code") or ""
            if code == "APP_ATTESTATION_FAILED":
                self._att_secret = ""
                self._att_fetched_at = 0
                raise RuntimeError(
                    f"应用完整性校验失败（请先实现 _build_app_headers 签名）: {err}"
                )
            raise RuntimeError(f"拒绝访问 (HTTP 403): {err}")

        if isinstance(data, dict) and data.get("success") is False:
            err = data.get("error") or data.get("message") or str(data)
            code = data.get("code") or ""
            if code == "DOWNLOAD_RATE_LIMITED":
                retry = data.get("retryAfterSec") or data.get("retryAfter") or "?"
                raise RuntimeError(f"下载限流，请约 {retry} 秒后再试。")
            raise RuntimeError(f"接口错误 (HTTP {r.status_code}): {err}")

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

        seen = set()
        out: list[dict] = []
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
        if isinstance(tags, list):
            tag_str = " ".join(str(t) for t in tags).lower()
        else:
            tag_str = str(tags).lower()
        if kw in tag_str:
            return 2
        desc = str(
            item.get("description")
            or item.get("desc")
            or item.get("summary")
            or item.get("intro")
            or ""
        ).lower()
        if kw in desc:
            return 1
        return 0

    def _sort_search_items(self, items: list[dict], keyword: str) -> list[dict]:
        return sorted(
            items,
            key=lambda it: (-self._match_score(it, keyword), str(it.get("title") or "")),
        )

    def _format_list(self, items: list[dict], title: str, keyword: str = "") -> str:
        limit = self._list_limit()
        items = items[:limit]
        if not items:
            return "【" + title + "】\n暂无结果。"
        lines = ["【" + title + "】共 " + str(len(items)) + " 条（最多显示 " + str(limit) + "）\n"]
        for i, it in enumerate(items, 1):
            name = str(it.get("title") or it.get("name") or "未知").strip()
            sid = str(it.get("id") or "").strip()
            size = str(
                it.get("packageSize") or it.get("fileSize") or it.get("size") or ""
            ).strip()
            score = self._match_score(it, keyword) if keyword else 0
            mark = {3: "〔标题〕", 2: "〔标签〕", 1: "〔简介〕"}.get(score, "")
            line = f"{i}. {name}{mark}"
            if size:
                line += f"\n   大小: {size}"
            if sid:
                line += f"\n   ID: {sid}"
            lines.append(line)
        lines.append("\n下载: /mz下载 <软件ID>\n详情: /mz详情 <软件ID>")
        return "\n".join(lines)

    @staticmethod
    def _format_detail(item: dict) -> str:
        name = str(item.get("title") or item.get("name") or "未知").strip()
        sid = str(item.get("id") or "").strip()
        size = str(
            item.get("packageSize") or item.get("fileSize") or item.get("size") or "-"
        ).strip()
        cat = str(item.get("categoryName") or item.get("category") or "-").strip()
        tags = item.get("tags") or []
        if isinstance(tags, list):
            tag_s = "、".join(str(t) for t in tags) if tags else "-"
        else:
            tag_s = str(tags) or "-"
        desc = str(
            item.get("description")
            or item.get("desc")
            or item.get("summary")
            or item.get("intro")
            or ""
        ).strip()
        if len(desc) > 300:
            desc = desc[:300] + "…"
        lines = [
            "【萌宅 · 详情】",
            f"名称: {name}",
            f"ID: {sid}",
            f"分类: {cat}",
            f"大小: {size}",
            f"标签: {tag_s}",
        ]
        if desc:
            lines.append(f"简介: {desc}")
        if sid:
            lines.append(f"\n下载: /mz下载 {sid}")
        return "\n".join(lines)

    @staticmethod
    def _format_download(data: dict, software_id: str) -> str:
        url = (
            data.get("downloadUrl")
            or data.get("directUrl")
            or data.get("url")
            or ""
        )
        if isinstance(url, dict):
            url = url.get("url") or url.get("downloadUrl") or ""
        url = str(url).strip()
        name = str(data.get("fileName") or data.get("title") or software_id).strip()
        size = str(
            data.get("fileSize") or data.get("packageSize") or data.get("size") or "-"
        ).strip()
        is_member = data.get("isMember")
        lines = [
            "╔══════════════════",
            "║ 萌宅 · 下载链接",
            "╠══════════════════",
            f"║ 文件: {name}",
            f"║ 大小: {size}",
            f"║ ID: {software_id}",
        ]
        if is_member is not None:
            lines.append(f"║ 会员: {'是' if is_member else '否'}")
        lines.append("╠══════════════════")
        if url:
            lines.append("║ 链接:")
            lines.append(f"║ {url}")
        else:
            lines.append("║ （未返回下载地址，请稍后重试或检查会员权限）")
            for k in ("message", "tip", "hint"):
                if data.get(k):
                    lines.append(f"║ {data.get(k)}")
        lines.append("╚══════════════════")
        return "\n".join(lines)

    async def _build_result(
        self, event: AstrMessageEvent, text: str
    ) -> AsyncGenerator:
        """优先合并转发；uin 无效或构造失败时回退纯文本。
        若协议端在 yield 之后仍发送失败，请在 WebUI 关闭 send_as_forward。
        """
        if not self._send_as_forward():
            yield event.plain_result("\u200b" + text + "\u200b")
            return

        try:
            self_id = event.get_self_id()
            uin_raw = str(self_id or "").strip()
            if not uin_raw or uin_raw in ("0", "None"):
                logger.warning(
                    f"[mengzhai] invalid self_id={self_id!r}, fallback plain"
                )
                yield event.plain_result("\u200b" + text + "\u200b")
                return

            try:
                uin = int(uin_raw)
            except ValueError:
                uin = uin_raw

            max_len = 3500
            chunks = [text[i : i + max_len] for i in range(0, len(text), max_len)] or [
                text
            ]
            nodes = [
                Node(uin=uin, name="萌宅下载", content=[Plain(chunk)])
                for chunk in chunks
            ]

            try:
                from astrbot.api.message_components import Nodes

                yield event.chain_result([Nodes(nodes)])
            except Exception:
                yield event.chain_result(nodes)
        except Exception as e:
            logger.warning(f"[mengzhai] forward build failed, fallback plain: {e}")
            yield event.plain_result("\u200b" + text + "\u200b")

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
                yield event.plain_result("搜索需要登录，请先在 WebUI 填写萌宅账号和密码。")
                return
            data = await self._request(
                "GET",
                "/api/software",
                params={"keyword": kw},
                need_auth=True,
            )
            items = self._extract_items(data)
            items = self._sort_search_items(items, kw)
            text = self._format_list(items, f"搜索「{kw}」", keyword=kw)
            self._mark_cooldown(event)
            async for r in self._build_result(event, text):
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
            items = self._extract_items(data)
            text = self._format_list(items, "最新软件")
            self._mark_cooldown(event)
            async for r in self._build_result(event, text):
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
            items = self._extract_items(data)
            text = self._format_list(items, "热门软件")
            self._mark_cooldown(event)
            async for r in self._build_result(event, text):
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
            yield event.plain_result("用法: /mz详情 <软件UUID>")
            return

        try:
            data = await self._request("GET", f"/api/software/{sid}", need_auth=False)
            item = data.get("item") if isinstance(data.get("item"), dict) else data
            if not isinstance(item, dict) or not item:
                items = self._extract_items(data)
                item = items[0] if items else {}
            if not item:
                yield event.plain_result("未找到该软件。")
                return
            text = self._format_detail(item)
            self._mark_cooldown(event)
            async for r in self._build_result(event, text):
                yield r
        except Exception as e:
            logger.exception("[mengzhai] detail error")
            yield event.plain_result(f"获取详情失败: {e}")

    @filter.command("mz下载")
    async def cmd_download(self, event: AstrMessageEvent, software_id: str = ""):
        if err := self._check_admin(event):
            yield event.plain_result(err)
            return
        if err := self._check_cooldown(event):
            yield event.plain_result(err)
            return

        if not self._has_credentials():
            yield event.plain_result("下载需要登录，请先在 WebUI 填写萌宅账号和密码。")
            return

        sid = (software_id or "").strip()
        if not sid:
            msg = (event.message_str or "").strip()
            for p in ("mz下载", "/mz下载", "mzdownload"):
                if msg.lower().startswith(p.lower()):
                    sid = msg[len(p) :].strip()
                    break
        if not sid or not UUID_RE.match(sid):
            yield event.plain_result("用法: /mz下载 <软件UUID>")
            return

        try:
            data = await self._request(
                "POST",
                f"/api/software/{sid}/download",
                json_body={},
                need_auth=True,
            )
            payload = data
            for key in ("data", "item", "result"):
                if isinstance(data.get(key), dict):
                    payload = {**data, **data[key]}
                    break
            text = self._format_download(payload, sid)
            self._mark_cooldown(event)
            async for r in self._build_result(event, text):
                yield r
        except Exception as e:
            logger.exception("[mengzhai] download error")
            yield event.plain_result(f"获取下载链接失败: {e}")