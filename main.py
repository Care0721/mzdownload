import asyncio
import re
import time
from typing import Any, Optional

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Node, Plain
from astrbot.api.star import Context, Star, register

BASE_URL = "https://cn-api.mengzhai.club"
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@register(
    "astrbot_plugin_mzdownload",
    "Care",
    "萌宅社区下载插件：搜索、最新/热门、详情、获取下载链接",
    "1.9.6",
)
class MengzhaiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": "AstrBot-MengzhaiPlugin/1.0.4"},
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

    # ------------------------- Token 管理 -------------------------

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
                    "未配置萌宅账号或密码，请在 WebUI → 插件配置中填写 email / password，并重载插件。"
                )

            try:
                resp = await self.client.post(
                    "/api/auth/login",
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

    # ------------------------- 通用请求 -------------------------

    async def _request(
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

        try:
            resp = await self.client.request(
                method,
                path,
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
            return await self._request(
                method,
                path,
                need_auth=True,
                params=params,
                json_body=json_body,
                _retry_auth=False,
            )

        if resp.status_code >= 400:
            self._raise_api_error(
                resp.status_code, data, resp.text, need_auth=need_auth
            )

        if not isinstance(data, dict):
            raise RuntimeError("接口返回非 JSON 数据")

        if data.get("success") is False:
            self._raise_api_error(resp.status_code, data, "", need_auth=need_auth)

        return data

    def _raise_api_error(
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
                or data.get("error_description")
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
                    "接口需要登录，但未配置账号密码。请在 WebUI 填写 email/password 后重载插件。"
                )
            raise RuntimeError(
                f"未授权 (401)：{msg}。请检查账号密码是否正确，或重新保存配置后重载插件。"
            )

        raise RuntimeError(f"接口错误 (HTTP {status}): {msg}")

    # ------------------------- 工具方法 -------------------------

    def _is_valid_uuid(self, s: str) -> bool:
        return bool(s and UUID_RE.match(s.strip()))

    def _list_limit(self) -> int:
        try:
            n = int(self._cfg("list_limit", 10) or 10)
        except (TypeError, ValueError):
            n = 10
        return max(1, min(n, 50))

    def _match_score(self, item: dict, keyword: str) -> int:
        """
        匹配优先级（越大越靠前）：
          3 = 标题含关键词
          2 = 标签含关键词
          1 = 简介/描述含关键词
          0 = 其他
        """
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
            or item.get("categories")
            or item.get("category")
            or []
        )
        if contains(tags):
            return 2

        desc = (
            item.get("description")
            or item.get("desc")
            or item.get("summary")
            or item.get("intro")
            or item.get("content")
            or ""
        )
        if contains(desc):
            return 1

        return 0

    def _sort_search_items(self, items: Any, keyword: str) -> list:
        if not isinstance(items, list):
            return []
        indexed = list(enumerate(items))
        indexed.sort(
            key=lambda pair: (
                -self._match_score(
                    pair[1] if isinstance(pair[1], dict) else {}, keyword
                ),
                pair[0],
            )
        )
        return [it for _, it in indexed]

    def _format_list(self, items: Any, title: str, keyword: str = "") -> str:
        if not isinstance(items, list):
            items = []
        limit = self._list_limit()
        items = items[:limit]

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
        """根据配置返回纯文本或合并转发；失败时回退纯文本"""
        if not self._cfg("send_as_forward", False):
            return event.plain_result(text)
        try:
            self_id = event.get_self_id()
            uin: Any = str(self_id) if self_id is not None else "0"
            node = Node(uin=uin, name="萌宅下载", content=[Plain(text)])
            return event.chain_result([node])
        except Exception as e:
            logger.warning(f"[萌宅] 合并转发构建失败，回退纯文本: {e}")
            return event.plain_result(text)

    def _should_auth(self) -> bool:
        return self._has_credentials()

    # ------------------------- 指令 -------------------------

    @filter.command("mz搜索")
    async def cmd_search(self, event: AstrMessageEvent, keyword: str = ""):
        """搜索软件：/mz搜索 <关键词>"""
        if msg := self._check_admin_only(event):
            yield event.plain_result(msg)
            return
        if msg := self._check_cooldown(event):
            yield event.plain_result(msg)
            return

        keyword = (keyword or "").strip()
        if not keyword:
            yield event.plain_result("请输入关键词，例如：/mz搜索 爱情")
            return

        try:
            data = await self._request(
                "GET",
                "/api/software",
                params={"keyword": keyword},
                need_auth=self._should_auth(),
            )
            items = data.get("items") if isinstance(data, dict) else []
            items = self._sort_search_items(items, keyword)
            text = self._format_list(items, f"搜索「{keyword}」", keyword=keyword)
            self._record_cooldown(event)
            yield self._build_result(event, text)
        except Exception as e:
            logger.exception("[萌宅] 搜索失败")
            yield event.plain_result(f"搜索失败：{e}")

    @filter.command("mz最新")
    async def cmd_latest(self, event: AstrMessageEvent):
        """获取最新软件列表：/mz最新"""
        if msg := self._check_admin_only(event):
            yield event.plain_result(msg)
            return
        if msg := self._check_cooldown(event):
            yield event.plain_result(msg)
            return

        try:
            data = await self._request(
                "GET",
                "/api/software/home/latest",
                need_auth=self._should_auth(),
            )
            items = data.get("items") if isinstance(data, dict) else []
            text = self._format_list(items, "最新软件")
            self._record_cooldown(event)
            yield self._build_result(event, text)
        except Exception as e:
            logger.exception("[萌宅] 获取最新列表失败")
            yield event.plain_result(f"获取最新列表失败：{e}")

    @filter.command("mz热门")
    async def cmd_hot(self, event: AstrMessageEvent):
        """获取热门软件列表：/mz热门"""
        if msg := self._check_admin_only(event):
            yield event.plain_result(msg)
            return
        if msg := self._check_cooldown(event):
            yield event.plain_result(msg)
            return

        try:
            data = await self._request(
                "GET",
                "/api/software/home/most-liked",
                need_auth=self._should_auth(),
            )
            items = data.get("items") if isinstance(data, dict) else []
            text = self._format_list(items, "热门软件")
            self._record_cooldown(event)
            yield self._build_result(event, text)
        except Exception as e:
            logger.exception("[萌宅] 获取热门列表失败")
            yield event.plain_result(f"获取热门列表失败：{e}")

    @filter.command("mz详情")
    async def cmd_detail(self, event: AstrMessageEvent, software_id: str = ""):
        """查看软件详情：/mz详情 <软件ID>"""
        if msg := self._check_admin_only(event):
            yield event.plain_result(msg)
            return
        if msg := self._check_cooldown(event):
            yield event.plain_result(msg)
            return

        software_id = (software_id or "").strip()
        if not self._is_valid_uuid(software_id):
            yield event.plain_result(
                "请提供有效的软件 UUID，例如：\n/mz详情 xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
            )
            return

        try:
            data = await self._request(
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
            logger.exception("[萌宅] 获取详情失败")
            yield event.plain_result(f"获取详情失败：{e}")

    @filter.command("mz下载")
    async def cmd_download(self, event: AstrMessageEvent, software_id: str = ""):
        """获取下载链接：/mz下载 <软件ID>（需要登录）"""
        if msg := self._check_admin_only(event):
            yield event.plain_result(msg)
            return
        if msg := self._check_cooldown(event):
            yield event.plain_result(msg)
            return

        software_id = (software_id or "").strip()
        if not self._is_valid_uuid(software_id):
            yield event.plain_result(
                "请提供有效的软件 UUID，例如：\n/mz下载 xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
            )
            return

        try:
            data = await self._request(
                "POST",
                f"/api/software/{software_id}/download",
                need_auth=True,
                json_body={},
            )
            url, file_name, file_size, is_member = self._extract_download_info(data)

            if not url:
                yield event.plain_result(
                    f"未获取到下载链接，请稍后重试。\n原始返回摘要：{str(data)[:300]}"
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
            logger.exception("[萌宅] 获取下载链接失败")
            yield event.plain_result(f"获取下载链接失败：{e}")