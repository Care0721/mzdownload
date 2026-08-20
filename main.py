import asyncio
import re
import time
from typing import Any, Optional

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Node, Plain
from astrbot.api.star import Context, Star, register

MZ_BASE = "https://cn-api.mengzhai.club"
COPY_BASES = [
    "https://api.copy202602.com",
    "https://api.copy202601.com",
]
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
PATH_WORD_RE = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


@register(
    "astrbot_plugin_mzdownload",
    "YourName",
    "萌宅下载 + Copy 小说搜索/详情/分卷",
    "2.0.0",
)
class MengzhaiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": "AstrBot-MengzhaiPlugin/1.1.0"},
        )
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._login_lock = asyncio.Lock()
        self._cooldown: dict[str, float] = {}
        self._last_email: str = ""
        self._last_password: str = ""

    async def terminate(self):
        try:
            await self.client.aclose()
        except Exception as e:
            logger.warning(f"[萌宅] 关闭 httpx 客户端异常: {e}")

    # ------------------------- 配置 / 权限 / 冷却 -------------------------

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _has_credentials(self) -> bool:
        email = str(self._cfg("email", "") or "").strip()
        password = str(self._cfg("password", "") or "")
        return bool(email and password)

    def _check_admin_only(self, event: AstrMessageEvent) -> Optional[str]:
        if self._cfg("admin_only", False) and not event.is_admin():
            return "本插件已开启「仅管理员可用」，你没有权限。"
        return None

    def _check_cooldown(self, event: AstrMessageEvent) -> Optional[str]:
        if event.is_admin():
            return None
        try:
            cd = int(self._cfg("cooldown_seconds", 8) or 0)
        except (TypeError, ValueError):
            cd = 8
        if cd <= 0:
            return None
        uid = str(event.get_sender_id() or "")
        if not uid:
            return None
        now = time.time()
        next_ts = self._cooldown.get(uid, 0.0)
        if now < next_ts:
            remain = max(1, int(next_ts - now) + 1)
            return f"冷却中，请 {remain} 秒后再试。"
        return None

    def _record_cooldown(self, event: AstrMessageEvent) -> None:
        if event.is_admin():
            return
        try:
            cd = int(self._cfg("cooldown_seconds", 8) or 0)
        except (TypeError, ValueError):
            cd = 8
        if cd <= 0:
            return
        uid = str(event.get_sender_id() or "")
        if uid:
            self._cooldown[uid] = time.time() + cd

    def _novel_enabled(self) -> bool:
        return bool(self._cfg("novel_enabled", True))

    # ------------------------- 萌宅 Token -------------------------

    def _need_relogin(self) -> bool:
        email = str(self._cfg("email", "") or "").strip()
        password = str(self._cfg("password", "") or "")
        if email != self._last_email or password != self._last_password:
            return True
        if not self._token or time.time() >= (self._token_expires_at - 60):
            return True
        return False

    def _clear_token(self) -> None:
        self._token = None
        self._token_expires_at = 0.0

    async def _ensure_token(self) -> str:
        if not self._need_relogin():
            assert self._token is not None
            return self._token

        async with self._login_lock:
            if not self._need_relogin():
                assert self._token is not None
                return self._token

            email = str(self._cfg("email", "") or "").strip()
            password = str(self._cfg("password", "") or "")
            if not email or not password:
                raise RuntimeError(
                    "未配置萌宅账号或密码，请在 WebUI 填写 email / password 后重载插件。"
                )

            try:
                resp = await self.client.post(
                    f"{MZ_BASE}/api/auth/login",
                    json={"email": email, "password": password},
                )
            except httpx.TimeoutException as e:
                raise RuntimeError("登录请求超时，请稍后重试。") from e
            except httpx.RequestError as e:
                raise RuntimeError(f"登录网络异常: {e}") from e

            try:
                data = resp.json()
            except Exception:
                data = None

            if resp.status_code >= 400:
                msg = ""
                if isinstance(data, dict):
                    msg = (
                        data.get("message")
                        or data.get("msg")
                        or data.get("error")
                        or str(data)
                    )
                raise RuntimeError(
                    f"登录失败 (HTTP {resp.status_code})"
                    + (f": {msg}" if msg else "")
                )

            if not isinstance(data, dict) or not data.get("success"):
                msg = ""
                if isinstance(data, dict):
                    msg = (
                        data.get("message")
                        or data.get("msg")
                        or data.get("error")
                        or str(data)
                    )
                else:
                    msg = str(data)
                raise RuntimeError(f"登录失败: {msg or '未知错误'}")

            token = data.get("token")
            if not token:
                raise RuntimeError("登录成功但未返回 token。")

            self._token = str(token)
            expires_at_ms = data.get("expiresAt")
            try:
                if expires_at_ms is not None:
                    self._token_expires_at = float(expires_at_ms) / 1000.0
                else:
                    self._token_expires_at = time.time() + 86400.0
            except (TypeError, ValueError):
                self._token_expires_at = time.time() + 86400.0

            self._last_email = email
            self._last_password = password
            logger.info("[萌宅] 登录成功，token 已缓存")
            return self._token

    # ------------------------- 萌宅请求 -------------------------

    async def _mz_request(
        self,
        method: str,
        path: str,
        *,
        need_auth: bool = False,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        _retry_auth: bool = True,
    ) -> dict:
        headers: dict[str, str] = {}
        if need_auth:
            token = await self._ensure_token()
            headers["Authorization"] = f"Bearer {token}"

        url = path if path.startswith("http") else f"{MZ_BASE}{path}"
        try:
            resp = await self.client.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers or None,
            )
        except httpx.TimeoutException as e:
            raise RuntimeError("请求超时，请稍后重试。") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"网络请求异常: {e}") from e

        try:
            data = resp.json()
        except Exception:
            data = None

        if need_auth and _retry_auth and resp.status_code == 401:
            logger.warning("[萌宅] 收到 401，清空 token 并重试登录")
            self._clear_token()
            return await self._mz_request(
                method,
                path,
                need_auth=True,
                params=params,
                json_body=json_body,
                _retry_auth=False,
            )

        if resp.status_code >= 400:
            self._raise_mz_error(resp.status_code, data, resp.text, need_auth=need_auth)

        if not isinstance(data, dict):
            raise RuntimeError("接口返回非 JSON 数据")

        if data.get("success") is False:
            self._raise_mz_error(resp.status_code, data, "", need_auth=need_auth)

        return data

    def _raise_mz_error(
        self, status: int, data: Any, raw_text: str, *, need_auth: bool = False
    ) -> None:
        code = None
        msg = ""
        if isinstance(data, dict):
            code = data.get("code") or data.get("error")
            msg = (
                data.get("message")
                or data.get("msg")
                or data.get("error")
                or ""
            )
            if code == "DOWNLOAD_RATE_LIMITED":
                retry = data.get("retryAfterSec")
                extra = f"，请 {retry} 秒后再试" if retry is not None else ""
                raise RuntimeError(f"下载限流{extra}")

        if not msg:
            msg = (raw_text or "")[:300] or "未知错误"

        if status == 401:
            if not need_auth and not self._has_credentials():
                raise RuntimeError(
                    "接口需要登录，但未配置账号密码。请在 WebUI 填写后重载插件。"
                )
            raise RuntimeError(f"未授权 (401)：{msg}")

        raise RuntimeError(f"接口错误 (HTTP {status}): {msg}")

    def _should_auth(self) -> bool:
        return self._has_credentials()

    # ------------------------- Copy 小说请求 -------------------------

    def _copy_headers(self) -> dict[str, str]:
        ts = str(int(time.time()))
        # 设备信息随机化不是必须；固定占位即可
        return {
            "Accept": "application/json",
            "User-Agent": "COPY/3.0.9",
            "source": "copyApp",
            "deviceinfo": "1234567V-1234",
            "dt": time.strftime("%Y.%m.%d"),
            "platform": "3",
            "referer": "com.copymanga.app-3.0.9",
            "version": "3.0.9",
            "device": "AB1C.123456.789",
            "pseudoid": "abcdefghijklmnop",
            "region": "1",
            "authorization": "Token",
            "umstring": "b4c89ca4104ea9a97750314d791520ac",
            "x-auth-timestamp": ts,
            # APP 内为 native HMAC；实测错误签名仍可访问部分接口
            "x-auth-signature": "0",
        }

    async def _copy_get(self, path: str, params: Optional[dict] = None) -> dict:
        last_err: Optional[Exception] = None
        headers = self._copy_headers()
        p = dict(params or {})
        p.setdefault("platform", "3")

        for base in COPY_BASES:
            url = f"{base}{path}"
            try:
                resp = await self.client.get(url, params=p, headers=headers)
            except httpx.TimeoutException as e:
                last_err = RuntimeError("小说接口超时")
                continue
            except httpx.RequestError as e:
                last_err = RuntimeError(f"小说接口网络异常: {e}")
                continue

            text = resp.text or ""
            if text.lstrip().lower().startswith("<!doctype") or text.lstrip().lower().startswith(
                "<html"
            ):
                last_err = RuntimeError("小说接口维护中，请稍后重试")
                continue

            try:
                data = resp.json()
            except Exception:
                last_err = RuntimeError(f"小说接口非 JSON (HTTP {resp.status_code})")
                continue

            if resp.status_code >= 400:
                msg = ""
                if isinstance(data, dict):
                    msg = data.get("message") or data.get("msg") or str(data)
                last_err = RuntimeError(f"小说接口 HTTP {resp.status_code}: {msg}")
                continue

            if not isinstance(data, dict):
                last_err = RuntimeError("小说接口返回格式错误")
                continue

            code = data.get("code")
            if code is not None and int(code) != 200:
                msg = data.get("message") or data.get("msg") or f"code={code}"
                raise RuntimeError(f"小说接口失败: {msg}")

            return data

        raise last_err or RuntimeError("小说接口全部节点不可用")

    # ------------------------- 通用工具 -------------------------

    def _is_valid_uuid(self, s: str) -> bool:
        return bool(s and UUID_RE.match(s.strip()))

    def _is_valid_path_word(self, s: str) -> bool:
        return bool(s and PATH_WORD_RE.match(s.strip()))

    def _list_limit(self) -> int:
        try:
            n = int(self._cfg("list_limit", 10) or 10)
        except (TypeError, ValueError):
            n = 10
        return max(1, min(n, 50))

    def _extract_arg(self, event: AstrMessageEvent, arg: str, prefixes: tuple[str, ...]) -> str:
        text = (arg or "").strip()
        if text:
            return text
        msg = (event.message_str or "").strip()
        msg = re.sub(r"^\s*/?\s*", "", msg)
        for p in prefixes:
            if msg.lower().startswith(p.lower()):
                msg = msg[len(p) :].strip()
                break
        return re.sub(r"^/\s*", "", msg).strip()

    def _extract_items(self, data: dict) -> list:
        if not isinstance(data, dict):
            return []
        candidates = [
            data.get("items"),
            data.get("list"),
            data.get("data"),
            data.get("results"),
            data.get("softwares"),
        ]
        items = None
        for c in candidates:
            if isinstance(c, list):
                items = c
                break
            if isinstance(c, dict):
                for k in ("items", "list", "results", "records"):
                    if isinstance(c.get(k), list):
                        items = c.get(k)
                        break
            if items is not None:
                break
        if not isinstance(items, list):
            return []

        seen: set[str] = set()
        unique: list = []
        for it in items:
            if not isinstance(it, dict):
                continue
            sid = str(it.get("id") or it.get("path_word") or "").strip()
            if sid:
                if sid in seen:
                    continue
                seen.add(sid)
            unique.append(it)
        return unique

    def _match_score(self, item: dict, keyword: str) -> int:
        if not isinstance(item, dict) or not keyword:
            return 0
        kw = keyword.strip().lower()
        if not kw:
            return 0

        def contains(text: Any) -> bool:
            if text is None:
                return False
            if isinstance(text, (list, tuple, set)):
                return any(contains(x) for x in text)
            return kw in str(text).lower()

        title = item.get("title") or item.get("name") or ""
        if contains(title):
            return 3
        tags = (
            item.get("tags")
            or item.get("tag")
            or item.get("labels")
            or item.get("theme")
            or item.get("categories")
            or []
        )
        if contains(tags):
            return 2
        desc = (
            item.get("description")
            or item.get("desc")
            or item.get("summary")
            or item.get("brief")
            or ""
        )
        if contains(desc):
            return 1
        return 0

    def _sort_search_items(self, items: list, keyword: str) -> list:
        indexed = list(enumerate(items))
        indexed.sort(
            key=lambda pair: (
                -self._match_score(pair[1] if isinstance(pair[1], dict) else {}, keyword),
                pair[0],
            )
        )
        return [it for _, it in indexed]

    def _format_software_list(self, items: Any, title: str, keyword: str = "") -> str:
        if not isinstance(items, list):
            items = []
        items = items[: self._list_limit()]
        if not items:
            return f"【{title}】\n暂无数据"

        lines = [f"【{title}】共 {len(items)} 条", ""]
        for i, it in enumerate(items, 1):
            if not isinstance(it, dict):
                lines.append(f"{i}. （无效条目）")
                lines.append("")
                continue
            sid = it.get("id") or "未知ID"
            name = it.get("title") or it.get("name") or "未知标题"
            size = (
                it.get("packageSize")
                or it.get("fileSize")
                or it.get("size")
                or "未知大小"
            )
            mark = ""
            if keyword:
                score = self._match_score(it, keyword)
                if score == 3:
                    mark = " 【标题匹配】"
                elif score == 2:
                    mark = " 【标签匹配】"
                elif score == 1:
                    mark = " 【简介匹配】"
            lines.append(f"{i}. {name}{mark}")
            lines.append(f"   ID：{sid}")
            lines.append(f"   大小：{size}")
            lines.append("")
        lines.append("使用 /mz下载 <软件ID> 获取下载链接")
        return "\n".join(lines).rstrip()

    def _format_novel_list(self, items: list, title: str) -> str:
        items = items[: self._list_limit()]
        if not items:
            return f"【{title}】\n暂无数据"

        lines = [f"【{title}】共 {len(items)} 条", ""]
        for i, it in enumerate(items, 1):
            name = it.get("name") or "未知"
            pw = it.get("path_word") or ""
            authors = it.get("author") or []
            if isinstance(authors, list):
                author = " / ".join(
                    a.get("name") for a in authors if isinstance(a, dict) and a.get("name")
                ) or "未知"
            else:
                author = str(authors)
            popular = it.get("popular", "")
            lines.append(f"{i}. {name}")
            lines.append(f"   作者：{author}")
            lines.append(f"   path_word：{pw}")
            if popular != "":
                lines.append(f"   热度：{popular}")
            lines.append("")
        lines.append("使用 /mz小说详情 <path_word> 查看详情")
        lines.append("使用 /mz小说分卷 <path_word> 查看分卷")
        return "\n".join(lines).rstrip()

    def _format_download(
        self,
        *,
        software_id: str,
        url: str,
        file_name: str,
        file_size: str,
        is_member: Any,
    ) -> str:
        lines = [
            "【萌宅 · 下载准备成功】",
            "────────────────",
            f"软件 ID：{software_id}",
            f"文件名：{file_name}",
            f"大小：{file_size}",
        ]
        if is_member is not None:
            lines.append(f"会员资源：{'是' if is_member else '否'}")
        lines.append("────────────────")
        lines.append("下载链接：")
        lines.append(url)
        lines.append("────────────────")
        lines.append("提示：链接可能有有效期，请尽快下载。")
        return "\n".join(lines)

    def _extract_download_info(self, data: dict) -> tuple[str, str, str, Any]:
        nested = data.get("data") if isinstance(data.get("data"), dict) else {}
        sources = [data, nested]

        def pick(*keys: str) -> Any:
            for src in sources:
                for k in keys:
                    v = src.get(k)
                    if v is not None and v != "":
                        return v
            return None

        url = pick("downloadUrl", "directUrl", "url", "link")
        file_name = pick("fileName", "filename", "name") or "未知文件名"
        file_size = pick("fileSize", "size", "packageSize") or "未知"
        is_member = pick("isMember", "member")
        return str(url) if url else "", str(file_name), str(file_size), is_member

    def _build_result(self, event: AstrMessageEvent, text: str):
        if not self._cfg("send_as_forward", False):
            return event.plain_result(text)
        try:
            self_id = event.get_self_id()
            uin: Any = str(self_id) if self_id is not None else "0"
            node = Node(uin=uin, name="萌宅插件", content=[Plain(text)])
            return event.chain_result([node])
        except Exception as e:
            logger.warning(f"[萌宅] 合并转发失败，回退纯文本: {e}")
            return event.plain_result(text)

    # ------------------------- 软件指令 -------------------------

    @filter.command("mz搜索")
    async def cmd_search(self, event: AstrMessageEvent, keyword: str = ""):
        """搜索软件：/mz搜索 <关键词>"""
        if msg := self._check_admin_only(event):
            yield event.plain_result(msg)
            return
        if msg := self._check_cooldown(event):
            yield event.plain_result(msg)
            return

        keyword = self._extract_arg(event, keyword, ("mz搜索", "mzsearch"))
        if not keyword:
            yield event.plain_result("请输入关键词，例如：/mz搜索 爱情")
            return

        try:
            data = await self._mz_request(
                "GET",
                "/api/software",
                params={
                    "keyword": keyword,
                    "q": keyword,
                    "search": keyword,
                    "query": keyword,
                },
                need_auth=self._should_auth(),
            )
            items = self._extract_items(data)
            items = self._sort_search_items(items, keyword)
            text = self._format_software_list(items, f"搜索「{keyword}」", keyword)
            self._record_cooldown(event)
            yield self._build_result(event, text)
        except Exception as e:
            logger.exception("[萌宅] 搜索失败")
            yield event.plain_result(f"搜索失败：{e}")

    @filter.command("mz最新")
    async def cmd_latest(self, event: AstrMessageEvent):
        """最新软件：/mz最新"""
        if msg := self._check_admin_only(event):
            yield event.plain_result(msg)
            return
        if msg := self._check_cooldown(event):
            yield event.plain_result(msg)
            return
        try:
            data = await self._mz_request(
                "GET",
                "/api/software/home/latest",
                need_auth=self._should_auth(),
            )
            text = self._format_software_list(self._extract_items(data), "最新软件")
            self._record_cooldown(event)
            yield self._build_result(event, text)
        except Exception as e:
            logger.exception("[萌宅] 最新列表失败")
            yield event.plain_result(f"获取最新列表失败：{e}")

    @filter.command("mz热门")
    async def cmd_hot(self, event: AstrMessageEvent):
        """热门软件：/mz热门"""
        if msg := self._check_admin_only(event):
            yield event.plain_result(msg)
            return
        if msg := self._check_cooldown(event):
            yield event.plain_result(msg)
            return
        try:
            data = await self._mz_request(
                "GET",
                "/api/software/home/most-liked",
                need_auth=self._should_auth(),
            )
            text = self._format_software_list(self._extract_items(data), "热门软件")
            self._record_cooldown(event)
            yield self._build_result(event, text)
        except Exception as e:
            logger.exception("[萌宅] 热门列表失败")
            yield event.plain_result(f"获取热门列表失败：{e}")

    @filter.command("mz详情")
    async def cmd_detail(self, event: AstrMessageEvent, software_id: str = ""):
        """软件详情：/mz详情 <UUID>"""
        if msg := self._check_admin_only(event):
            yield event.plain_result(msg)
            return
        if msg := self._check_cooldown(event):
            yield event.plain_result(msg)
            return

        software_id = self._extract_arg(event, software_id, ("mz详情",))
        if not self._is_valid_uuid(software_id):
            yield event.plain_result("请提供有效软件 UUID")
            return

        try:
            data = await self._mz_request(
                "GET",
                f"/api/software/{software_id}",
                need_auth=self._should_auth(),
            )
            item = data.get("item") if isinstance(data.get("item"), dict) else data
            if not isinstance(item, dict):
                item = {}
            title = item.get("title") or item.get("name") or "未知"
            size = (
                item.get("packageSize")
                or item.get("fileSize")
                or item.get("size")
                or "未知"
            )
            desc = (
                item.get("description")
                or item.get("desc")
                or item.get("summary")
                or "无描述"
            )
            if isinstance(desc, str) and len(desc) > 500:
                desc = desc[:500] + "..."
            text = (
                f"【软件详情】\n"
                f"标题：{title}\n"
                f"ID：{software_id}\n"
                f"大小：{size}\n"
                f"描述：{desc}\n\n"
                f"使用 /mz下载 {software_id} 获取下载链接"
            )
            self._record_cooldown(event)
            yield self._build_result(event, text)
        except Exception as e:
            logger.exception("[萌宅] 详情失败")
            yield event.plain_result(f"获取详情失败：{e}")

    @filter.command("mz下载")
    async def cmd_download(self, event: AstrMessageEvent, software_id: str = ""):
        """软件下载：/mz下载 <UUID>"""
        if msg := self._check_admin_only(event):
            yield event.plain_result(msg)
            return
        if msg := self._check_cooldown(event):
            yield event.plain_result(msg)
            return

        software_id = self._extract_arg(event, software_id, ("mz下载",))
        if not self._is_valid_uuid(software_id):
            yield event.plain_result("请提供有效软件 UUID")
            return

        try:
            data = await self._mz_request(
                "POST",
                f"/api/software/{software_id}/download",
                need_auth=True,
                json_body={},
            )
            url, file_name, file_size, is_member = self._extract_download_info(data)
            if not url:
                yield event.plain_result(
                    f"未获取到下载链接。\n摘要：{str(data)[:300]}"
                )
                return
            text = self._format_download(
                software_id=software_id,
                url=url,
                file_name=file_name,
                file_size=file_size,
                is_member=is_member,
            )
            self._record_cooldown(event)
            yield self._build_result(event, text)
        except Exception as e:
            logger.exception("[萌宅] 下载失败")
            yield event.plain_result(f"获取下载链接失败：{e}")

    # ------------------------- 小说指令 -------------------------

    @filter.command("mz小说搜索")
    async def cmd_novel_search(self, event: AstrMessageEvent, keyword: str = ""):
        """搜索小说：/mz小说搜索 <关键词>"""
        if msg := self._check_admin_only(event):
            yield event.plain_result(msg)
            return
        if not self._novel_enabled():
            yield event.plain_result("小说功能已关闭（WebUI 中 novel_enabled）。")
            return
        if msg := self._check_cooldown(event):
            yield event.plain_result(msg)
            return

        keyword = self._extract_arg(event, keyword, ("mz小说搜索",))
        if not keyword:
            yield event.plain_result("请输入关键词，例如：/mz小说搜索 爱情")
            return

        try:
            limit = self._list_limit()
            data = await self._copy_get(
                "/api/v3/search/books",
                {"q": keyword, "limit": str(limit), "offset": "0"},
            )
            results = data.get("results") if isinstance(data.get("results"), dict) else {}
            items = results.get("list") if isinstance(results, dict) else []
            if not isinstance(items, list):
                items = []
            items = self._sort_search_items(items, keyword)
            total = results.get("total", len(items)) if isinstance(results, dict) else len(items)
            text = self._format_novel_list(items, f"小说搜索「{keyword}」· 约 {total} 条")
            self._record_cooldown(event)
            yield self._build_result(event, text)
        except Exception as e:
            logger.exception("[萌宅] 小说搜索失败")
            yield event.plain_result(f"小说搜索失败：{e}")

    @filter.command("mz小说详情")
    async def cmd_novel_detail(self, event: AstrMessageEvent, path_word: str = ""):
        """小说详情：/mz小说详情 <path_word>"""
        if msg := self._check_admin_only(event):
            yield event.plain_result(msg)
            return
        if not self._novel_enabled():
            yield event.plain_result("小说功能已关闭（WebUI 中 novel_enabled）。")
            return
        if msg := self._check_cooldown(event):
            yield event.plain_result(msg)
            return

        path_word = self._extract_arg(event, path_word, ("mz小说详情",))
        if not self._is_valid_path_word(path_word):
            yield event.plain_result(
                "请提供 path_word，例如：/mz小说详情 yuziaiqinggushi"
            )
            return

        try:
            data = await self._copy_get(f"/api/v3/book/{path_word}", {})
            results = data.get("results") if isinstance(data.get("results"), dict) else {}
            book = results.get("book") if isinstance(results, dict) else {}
            if not isinstance(book, dict):
                book = {}

            name = book.get("name") or "未知"
            brief = book.get("brief") or "无简介"
            if isinstance(brief, str) and len(brief) > 400:
                brief = brief[:400] + "..."
            authors = book.get("author") or []
            if isinstance(authors, list):
                author = " / ".join(
                    a.get("name") for a in authors if isinstance(a, dict) and a.get("name")
                ) or "未知"
            else:
                author = "未知"
            themes = book.get("theme") or []
            if isinstance(themes, list):
                theme = " / ".join(
                    t.get("name") for t in themes if isinstance(t, dict) and t.get("name")
                ) or "无"
            else:
                theme = "无"
            status = book.get("status")
            if isinstance(status, dict):
                status = status.get("display") or status.get("value") or "未知"
            last = book.get("last_chapter") or {}
            last_name = last.get("name") if isinstance(last, dict) else ""
            popular = book.get("popular", results.get("popular", ""))

            text = (
                f"【小说详情】\n"
                f"书名：{name}\n"
                f"path_word：{path_word}\n"
                f"作者：{author}\n"
                f"主题：{theme}\n"
                f"状态：{status}\n"
                f"热度：{popular}\n"
                f"最新：{last_name or '无'}\n"
                f"简介：{brief}\n\n"
                f"使用 /mz小说分卷 {path_word} 查看分卷与文本地址"
            )
            self._record_cooldown(event)
            yield self._build_result(event, text)
        except Exception as e:
            logger.exception("[萌宅] 小说详情失败")
            yield event.plain_result(f"小说详情失败：{e}")

    @filter.command("mz小说分卷")
    async def cmd_novel_volumes(self, event: AstrMessageEvent, path_word: str = ""):
        """小说分卷：/mz小说分卷 <path_word>"""
        if msg := self._check_admin_only(event):
            yield event.plain_result(msg)
            return
        if not self._novel_enabled():
            yield event.plain_result("小说功能已关闭（WebUI 中 novel_enabled）。")
            return
        if msg := self._check_cooldown(event):
            yield event.plain_result(msg)
            return

        path_word = self._extract_arg(event, path_word, ("mz小说分卷",))
        if not self._is_valid_path_word(path_word):
            yield event.plain_result(
                "请提供 path_word，例如：/mz小说分卷 yuziaiqinggushi"
            )
            return

        try:
            data = await self._copy_get(f"/api/v3/book/{path_word}/volumes", {})
            results = data.get("results") if isinstance(data.get("results"), dict) else {}
            vols = results.get("list") if isinstance(results, dict) else []
            if not isinstance(vols, list) or not vols:
                yield event.plain_result("暂无分卷数据")
                return

            lines = [f"【小说分卷】{path_word}", f"共 {len(vols)} 卷", ""]
            # 最多展示 15 卷，避免刷屏；并为前 3 卷尝试取 txt 链接
            show = vols[:15]
            for i, v in enumerate(show, 1):
                if not isinstance(v, dict):
                    continue
                vid = v.get("id") or ""
                vname = v.get("name") or f"第{i}卷"
                lines.append(f"{i}. {vname}")
                lines.append(f"   volume_id：{vid}")

            # 尝试给第一卷补 txt 直链
            first = show[0] if isinstance(show[0], dict) else {}
            first_id = str(first.get("id") or "")
            if first_id:
                try:
                    vol_data = await self._copy_get(
                        f"/api/v3/book/{path_word}/volume/{first_id}",
                        {"in_mainland": "true"},
                    )
                    vr = vol_data.get("results") if isinstance(vol_data.get("results"), dict) else {}
                    volume = vr.get("volume") if isinstance(vr, dict) else {}
                    if isinstance(volume, dict):
                        txt = volume.get("txt_addr") or ""
                        enc = volume.get("txt_encoding") or ""
                        if txt:
                            lines.append("")
                            lines.append(f"第1卷文本：{txt}")
                            if enc:
                                lines.append(f"编码：{enc}")
                except Exception as e:
                    logger.warning(f"[萌宅] 获取分卷文本失败: {e}")

            lines.append("")
            lines.append("提示：文本来自第三方源，请遵守版权与当地法律，仅供个人学习。")
            text = "\n".join(lines)
            self._record_cooldown(event)
            yield self._build_result(event, text)
        except Exception as e:
            logger.exception("[萌宅] 小说分卷失败")
            yield event.plain_result(f"小说分卷失败：{e}")

    @filter.command("mz小说主题")
    async def cmd_novel_themes(self, event: AstrMessageEvent):
        """小说主题：/mz小说主题"""
        if msg := self._check_admin_only(event):
            yield event.plain_result(msg)
            return
        if not self._novel_enabled():
            yield event.plain_result("小说功能已关闭（WebUI 中 novel_enabled）。")
            return
        if msg := self._check_cooldown(event):
            yield event.plain_result(msg)
            return

        try:
            data = await self._copy_get("/api/v3/theme/book/count", {})
            results = data.get("results") if isinstance(data.get("results"), dict) else {}
            items = results.get("list") if isinstance(results, dict) else []
            if not isinstance(items, list):
                items = []
            items = items[: self._list_limit()]
            if not items:
                yield event.plain_result("暂无主题数据")
                return
            lines = ["【小说主题】", ""]
            for i, it in enumerate(items, 1):
                if not isinstance(it, dict):
                    continue
                lines.append(
                    f"{i}. {it.get('name', '未知')}（{it.get('path_word', '')}）· {it.get('count', 0)}"
                )
            lines.append("")
            lines.append("可用 /mz小说搜索 <主题名或关键词> 搜索")
            text = "\n".join(lines)
            self._record_cooldown(event)
            yield self._build_result(event, text)
        except Exception as e:
            logger.exception("[萌宅] 小说主题失败")
            yield event.plain_result(f"小说主题失败：{e}")