import asyncio
import hashlib
import json
from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

QUERY_URL = "https://yjsfw.zjut.edu.cn/gsapp/sys/wdxwxxapp/modules/xsbdjcsq/queryPyjg.do"
AUTO_QUERY_TZ = ZoneInfo("Asia/Shanghai")
AUTO_QUERY_START = dt_time(hour=8, minute=0)
AUTO_QUERY_END = dt_time(hour=22, minute=0)
DEFAULT_COOKIE = (
    "_ht=person; EMAP_LANG=zh; THEME=indigo; GS_SESSIONID=2bf55f92585bd59958a3ff654d559bb6; "
    "_WEU=vAqiRIftAcC6m8Qm83PsfRK4zP9QphCmjyfD9BmYr89zCstjBVgC8H0AgR2O7wmF4LLzY0zov97N_VGDOjKnGSb5nsU4LI5*AuR2WA2Lj_2mx5KnjxdH9OF3KQ7hIc9GJHJxjIq3bprr65KbhDPrQZLdi9DFu7QxXYtiQx046UvdUWRKWjfQenvKjvedzhJQoagvasj1901IcOYanQfON6KSOKqE9biDOcD2Mj2FH_FTzMyofcVEjTf0KeFpLibimqDKSJKzPttYfkAI3Ylc0RAoDZI0eSAtNoPWEbA4UQqPz6nbDwoGisUPtLUb9FwrmEtu5esV45JAG*htgf7EexjGsdG8HnC8M43lY8DP1PVI266OM620so..; "
    "route=3448423386cc74f8e6f54d6a40c37c33; JSESSIONID=dSyz6SuFl-qLr3jfmTpo2bpLGeG1GTnKVcJBTVV9fhVhiHFyWRDB!-914309848"
)
DEFAULT_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "https://yjsfw.zjut.edu.cn",
    "Referer": "https://yjsfw.zjut.edu.cn/gsapp/sys/wdxwxxapp/*default/index.do?THEME=indigo&EMAP_LANG=zh",
    "User-Agent": "Mozilla/5.0",
    "X-Requested-With": "XMLHttpRequest",
}


@register("astrbot_plugin_zjgydxjwxt", "YourName", "浙江工业大学教务系统结果查询插件", "v1.6.0")
class ZjutResultQueryPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.config = config or {}
        self._bindings_path = Path(__file__).resolve().parent / "datas_bindings.json"
        self._bindings_lock = asyncio.Lock()
        self._bindings_cache: dict[str, dict[str, Any]] | None = None
        self._auto_query_task: asyncio.Task | None = None
        self._auto_query_stop_event = asyncio.Event()

    async def initialize(self):
        auto_enabled = bool(self.config.get("auto_query_enabled", True))
        interval_minutes = self._get_auto_query_interval_minutes()
        window_start, window_end = self._get_auto_query_window()
        logger.info(
            "自动查询初始化: enabled=%s, interval_minutes=%s, window=%s-%s",
            auto_enabled,
            interval_minutes,
            window_start.strftime("%H:%M"),
            window_end.strftime("%H:%M"),
        )
        if auto_enabled:
            self._auto_query_stop_event.clear()
            self._auto_query_task = asyncio.create_task(self._auto_query_loop(), name="zjut_auto_query_loop")
            logger.info("自动查询后台任务已启动")

    async def terminate(self):
        if self._auto_query_task:
            logger.info("正在停止自动查询后台任务")
            self._auto_query_stop_event.set()
            self._auto_query_task.cancel()
            try:
                await self._auto_query_task
            except asyncio.CancelledError:
                pass
            self._auto_query_task = None
            logger.info("自动查询后台任务已停止")

    @filter.command("绑定datas")
    async def bind_datas_command(self, event: AstrMessageEvent):
        datas = self._extract_datas_arg(event.message_str)
        if not datas:
            yield event.plain_result("请使用：/绑定datas <datas>")
            return

        user_key = self._get_user_key(event)
        session_id = self._extract_session_id(event)
        logger.info("收到绑定datas指令: user=%s, has_session=%s", user_key, bool(session_id))
        await self._set_bound_datas(user_key, datas, session_id=session_id)
        yield event.plain_result("datas 绑定成功。")

    @filter.command("结果查询")
    async def query_result_command(self, event: AstrMessageEvent):
        datas = self._extract_datas_arg(event.message_str)
        user_key = self._get_user_key(event)
        session_id = self._extract_session_id(event)
        if session_id:
            await self._set_last_session_id(user_key, session_id)

        if not datas:
            datas = await self._get_bound_datas(user_key)
            if not datas:
                yield event.plain_result("未找到已绑定的 datas，请先使用：/绑定datas <datas>")
                return

        logger.info("开始执行手动结果查询: user=%s, datas_len=%s", user_key, len(datas))
        cookie = str(self.config.get("cookie") or DEFAULT_COOKIE).strip()
        if not cookie:
            yield event.plain_result("未配置 cookie，请先在插件配置中设置 cookie。")
            return

        headers_override = self.config.get("base_headers") or {}
        if not isinstance(headers_override, dict):
            headers_override = {}

        timeout = self.config.get("timeout", 15)
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            timeout = 15
        timeout = max(3, timeout)

        try:
            payload = await self._query_result(datas, cookie, headers_override, timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning("结果查询失败: %s", exc)
            yield event.plain_result(f"查询失败：{exc}")
            return

        if self._is_empty_result(payload):
            logger.info("手动结果查询完成: user=%s, result=empty", user_key)
            yield event.plain_result("暂无结果。")
            return

        logger.info("手动结果查询完成: user=%s, result=non_empty", user_key)
        yield event.plain_result(self._format_payload(payload))

    @filter.command("自动查询")
    async def auto_query_command(self, event: AstrMessageEvent):
        user_key = self._get_user_key(event)
        session_id = self._extract_session_id(event)
        if session_id:
            await self._set_last_session_id(user_key, session_id)
        logger.info("收到自动查询指令: user=%s, has_session=%s", user_key, bool(session_id))

        checked, pushed, skipped_empty, skipped_duplicate = await self._run_auto_query_once(trigger="manual_command")
        enabled = bool(self.config.get("auto_query_enabled", True))
        interval = self._get_auto_query_interval_minutes()
        yield event.plain_result(
            f"自动查询状态：{'开启' if enabled else '关闭'}，间隔 {interval} 分钟。\n"
            f"本轮统计：检查 {checked} 个，推送 {pushed} 个，空结果 {skipped_empty} 个，重复跳过 {skipped_duplicate} 个。"
        )

    async def _query_result(
        self,
        datas: str,
        cookie: str,
        headers_override: dict[str, Any],
        timeout: int,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._query_result_sync, datas, cookie, headers_override, timeout)

    def _query_result_sync(
        self,
        datas: str,
        cookie: str,
        headers_override: dict[str, Any],
        timeout: int,
    ) -> dict[str, Any]:
        headers = dict(DEFAULT_HEADERS)
        headers["Cookie"] = cookie
        for key, value in headers_override.items():
            headers[str(key)] = str(value)

        body = parse.urlencode({"datas": datas}).encode("utf-8")
        req = request.Request(QUERY_URL, data=body, headers=headers, method="POST")

        try:
            with request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            raise RuntimeError(f"接口返回 HTTP {exc.code}") from exc
        except error.URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            raise RuntimeError(f"网络异常：{reason}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("接口返回非 JSON 数据") from exc

        if not isinstance(data, dict):
            raise RuntimeError("接口返回格式异常")

        return data

    @staticmethod
    def _extract_datas_arg(message_str: str) -> str:
        if not message_str:
            return ""
        parts = message_str.strip().split(maxsplit=1)
        if len(parts) < 2:
            return ""
        return parts[1].strip()

    @staticmethod
    def _is_empty_result(payload: dict[str, Any]) -> bool:
        if payload.get("success") is not True:
            return False
        fwpjg = payload.get("fwpjgList")
        pyjg = payload.get("pyjgList")
        return isinstance(fwpjg, list) and isinstance(pyjg, list) and not fwpjg and not pyjg

    @staticmethod
    def _format_payload(payload: dict[str, Any]) -> str:
        if payload.get("success") is False:
            message = payload.get("msg") or payload.get("message") or "接口返回失败。"
            return f"查询失败：{message}"

        fwpjg = payload.get("fwpjgList") if isinstance(payload.get("fwpjgList"), list) else []
        pyjg = payload.get("pyjgList") if isinstance(payload.get("pyjgList"), list) else []

        summary = f"查询成功：fwpjgList {len(fwpjg)} 条，pyjgList {len(pyjg)} 条。"
        details = json.dumps({"fwpjgList": fwpjg, "pyjgList": pyjg}, ensure_ascii=False, indent=2)
        return f"{summary}\n{details}"

    def _get_user_key(self, event: AstrMessageEvent) -> str:
        sender_id = str(event.get_sender_id() or "").strip()
        if sender_id:
            return sender_id

        sender_name = str(event.get_sender_name() or "").strip()
        if sender_name:
            logger.warning("sender_id 不可用，回退使用 sender_name 作为绑定键: %s", sender_name)
            return f"name:{sender_name}"

        return "unknown"

    @staticmethod
    def _extract_session_id(event: AstrMessageEvent) -> str:
        session_id = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if session_id:
            return session_id
        return str(getattr(event, "session_id", "") or "").strip()

    def _get_auto_query_interval_minutes(self) -> int:
        value = self.config.get("auto_query_interval_minutes", 10)
        try:
            interval = int(value)
        except (TypeError, ValueError):
            interval = 10
        return max(1, interval)

    async def _get_bound_datas(self, user_key: str) -> str:
        bindings = await self._load_bindings()
        user_data = bindings.get(user_key)
        if not isinstance(user_data, dict):
            return ""
        return str(user_data.get("datas") or "").strip()

    async def _set_bound_datas(self, user_key: str, datas: str, session_id: str = "") -> None:
        async with self._bindings_lock:
            bindings = await self._load_bindings_unlocked()
            existed = bindings.get(user_key) if isinstance(bindings.get(user_key), dict) else {}
            bindings[user_key] = {
                "datas": datas,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "last_session_id": session_id or str(existed.get("last_session_id") or "").strip(),
                "last_push_fingerprint": str(existed.get("last_push_fingerprint") or "").strip(),
                "last_checked_at": str(existed.get("last_checked_at") or "").strip(),
                "last_push_at": str(existed.get("last_push_at") or "").strip(),
            }
            await asyncio.to_thread(self._save_bindings_sync, bindings)
            self._bindings_cache = bindings

    async def _set_last_session_id(self, user_key: str, session_id: str) -> None:
        if not session_id:
            return
        async with self._bindings_lock:
            bindings = await self._load_bindings_unlocked()
            existed = bindings.get(user_key) if isinstance(bindings.get(user_key), dict) else {}
            if not existed:
                return
            bindings[user_key] = {
                "datas": str(existed.get("datas") or "").strip(),
                "updated_at": str(existed.get("updated_at") or "").strip(),
                "last_session_id": session_id,
                "last_push_fingerprint": str(existed.get("last_push_fingerprint") or "").strip(),
                "last_checked_at": str(existed.get("last_checked_at") or "").strip(),
                "last_push_at": str(existed.get("last_push_at") or "").strip(),
            }
            await asyncio.to_thread(self._save_bindings_sync, bindings)
            self._bindings_cache = bindings

    async def _load_bindings(self) -> dict[str, dict[str, Any]]:
        async with self._bindings_lock:
            return await self._load_bindings_unlocked()

    async def _load_bindings_unlocked(self) -> dict[str, dict[str, Any]]:
        if self._bindings_cache is not None:
            return dict(self._bindings_cache)
        bindings = await asyncio.to_thread(self._load_bindings_sync)
        self._bindings_cache = bindings
        return dict(bindings)

    def _load_bindings_sync(self) -> dict[str, dict[str, Any]]:
        if not self._bindings_path.exists():
            return {}
        try:
            raw = self._bindings_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取 datas 绑定文件失败，将重置为空: %s", exc)
            return {}
        if not isinstance(data, dict):
            return {}
        normalized: dict[str, dict[str, Any]] = {}
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            datas = str(value.get("datas") or "").strip()
            updated_at = str(value.get("updated_at") or "").strip()
            last_session_id = str(value.get("last_session_id") or "").strip()
            last_push_fingerprint = str(value.get("last_push_fingerprint") or "").strip()
            last_checked_at = str(value.get("last_checked_at") or "").strip()
            last_push_at = str(value.get("last_push_at") or "").strip()
            if not datas:
                continue
            normalized[str(key)] = {
                "datas": datas,
                "updated_at": updated_at,
                "last_session_id": last_session_id,
                "last_push_fingerprint": last_push_fingerprint,
                "last_checked_at": last_checked_at,
                "last_push_at": last_push_at,
            }
        return normalized

    def _save_bindings_sync(self, bindings: dict[str, dict[str, Any]]) -> None:
        self._bindings_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._bindings_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(bindings, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self._bindings_path)

    async def _auto_query_loop(self):
        interval_seconds = self._get_auto_query_interval_minutes() * 60
        logger.info("自动查询循环已启动: interval_seconds=%s", interval_seconds)
        while not self._auto_query_stop_event.is_set():
            try:
                checked, pushed, skipped_empty, skipped_duplicate = await self._run_auto_query_once(trigger="scheduler")
                logger.info(
                    "自动查询轮次结束: checked=%s, pushed=%s, empty=%s, duplicate=%s",
                    checked,
                    pushed,
                    skipped_empty,
                    skipped_duplicate,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("自动查询轮次异常: %s", exc)

            try:
                await asyncio.wait_for(self._auto_query_stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                continue
        logger.info("自动查询循环已退出")

    async def _run_auto_query_once(self, trigger: str) -> tuple[int, int, int, int]:
        window_start, window_end = self._get_auto_query_window()
        if not self._is_in_auto_query_window(window_start, window_end):
            now_cn = datetime.now(AUTO_QUERY_TZ).strftime("%Y-%m-%d %H:%M:%S")
            logger.info(
                "自动查询跳过整轮: trigger=%s, now=%s, reason=out_of_window(%s-%s)",
                trigger,
                now_cn,
                window_start.strftime("%H:%M"),
                window_end.strftime("%H:%M"),
            )
            return 0, 0, 0, 0

        logger.info("开始执行自动查询轮次: trigger=%s", trigger)
        bindings = await self._load_bindings()
        cookie = str(self.config.get("cookie") or DEFAULT_COOKIE).strip()
        headers_override = self.config.get("base_headers") or {}
        if not isinstance(headers_override, dict):
            headers_override = {}
        timeout = self.config.get("timeout", 15)
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            timeout = 15
        timeout = max(3, timeout)

        checked = 0
        pushed = 0
        skipped_empty = 0
        skipped_duplicate = 0
        for user_key, user_data in bindings.items():
            datas = str((user_data or {}).get("datas") or "").strip()
            session_id = str((user_data or {}).get("last_session_id") or "").strip()
            if not datas:
                logger.info("自动查询跳过用户: user=%s, reason=missing_datas", user_key)
                continue
            if not session_id:
                logger.info("自动查询跳过用户: user=%s, reason=missing_session_id", user_key)
                continue

            checked += 1
            logger.info("自动查询用户开始: user=%s, datas_len=%s", user_key, len(datas))
            try:
                payload = await self._query_result(datas, cookie, headers_override, timeout)
            except Exception as exc:  # noqa: BLE001
                logger.info("自动查询用户失败: user=%s, error=%s", user_key, exc)
                await self._touch_checked_at(user_key)
                continue

            await self._touch_checked_at(user_key)
            if self._is_empty_result(payload):
                skipped_empty += 1
                logger.info("自动查询用户完成: user=%s, result=empty", user_key)
                continue

            message = self._format_payload(payload)
            fingerprint = hashlib.sha256(message.encode("utf-8")).hexdigest()
            old_fingerprint = str((user_data or {}).get("last_push_fingerprint") or "").strip()
            if fingerprint == old_fingerprint:
                skipped_duplicate += 1
                logger.info("自动查询用户跳过推送: user=%s, reason=duplicate", user_key)
                continue

            await self.context.send_message(session_id, MessageChain().message(message))
            pushed += 1
            logger.info("自动查询用户推送成功: user=%s, session=%s", user_key, session_id)
            await self._update_push_state(user_key, fingerprint)

        return checked, pushed, skipped_empty, skipped_duplicate

    def _get_auto_query_window(self) -> tuple[dt_time, dt_time]:
        start = self._parse_hhmm(str(self.config.get("auto_query_window_start", "08:00") or "08:00"), AUTO_QUERY_START)
        end = self._parse_hhmm(str(self.config.get("auto_query_window_end", "22:00") or "22:00"), AUTO_QUERY_END)
        return start, end

    @staticmethod
    def _parse_hhmm(value: str, default_value: dt_time) -> dt_time:
        raw = value.strip()
        try:
            hour, minute = raw.split(":", 1)
            h = int(hour)
            m = int(minute)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return dt_time(hour=h, minute=m)
        except (ValueError, TypeError):
            pass
        logger.warning("自动查询时间格式无效，回退默认值: %s", raw)
        return default_value

    @staticmethod
    def _is_in_auto_query_window(start: dt_time, end: dt_time) -> bool:
        now_time = datetime.now(AUTO_QUERY_TZ).time()
        if start <= end:
            return start <= now_time <= end
        return now_time >= start or now_time <= end

    async def _touch_checked_at(self, user_key: str) -> None:
        async with self._bindings_lock:
            bindings = await self._load_bindings_unlocked()
            existed = bindings.get(user_key)
            if not isinstance(existed, dict):
                return
            existed["last_checked_at"] = datetime.now(timezone.utc).isoformat()
            bindings[user_key] = existed
            await asyncio.to_thread(self._save_bindings_sync, bindings)
            self._bindings_cache = bindings

    async def _update_push_state(self, user_key: str, fingerprint: str) -> None:
        async with self._bindings_lock:
            bindings = await self._load_bindings_unlocked()
            existed = bindings.get(user_key)
            if not isinstance(existed, dict):
                return
            now = datetime.now(timezone.utc).isoformat()
            existed["last_push_fingerprint"] = fingerprint
            existed["last_push_at"] = now
            existed["last_checked_at"] = now
            bindings[user_key] = existed
            await asyncio.to_thread(self._save_bindings_sync, bindings)
            self._bindings_cache = bindings
