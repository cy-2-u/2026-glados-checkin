#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026 GLaDOS 自动签到：签到、积分查询、自动兑换、MeoW 推送。"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

DOMAINS = ("https://glados.cloud", "https://glados.rocks", "https://glados.network")
MEOW_NICKNAME = "第五个季节"
CHECKIN_TOKEN = "glados.cloud"
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 60
REQUEST_TIMEOUT = 10

# key 对应 GLADOS_EXCHANGE_PLAN 取值，value 为 (接口 planType, 所需积分, 兑换天数)
EXCHANGE_PLANS = {
    "100": ("plan100", 100, 10),
    "200": ("plan200", 200, 30),
    "500": ("plan500", 500, 100),
}

# 与生成 gld:sess 的登录浏览器（Edge）保持一致，规避新版设备校验
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
    ),
    "Sec-CH-UA": '"Chromium";v="120", "Microsoft Edge";v="120", "Not_A Brand";v="99"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
}

SUCCESS_MARKERS = (
    "checkin! got",
    "checkin repeats! please try tomorrow",
    "today's observation logged",
)
AUTH_FAIL_MARKERS = ("没有权限", "permission", "unauthorized", "automated check-in detected")

CURRENT_SESSION_COOKIES = ("gld:sess", "gld:sess.sig")
LEGACY_SESSION_COOKIES = ("koa:sess", "koa:sess.sig")


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")


def session_kind(cookie):
    names = {part.strip().partition("=")[0] for part in cookie.split(";") if "=" in part}
    if set(CURRENT_SESSION_COOKIES) <= names:
        return "gld"
    if set(LEGACY_SESSION_COOKIES) <= names:
        return "koa"
    return None


def parse_cookie(raw):
    """兼容请求头、Cookie-Editor JSON、旧版 token 三种 Cookie 输入。"""
    if not raw:
        return None
    raw = re.sub(r"^cookie\s*:\s*", "", raw.strip(), flags=re.IGNORECASE)
    if any(f"{name}=" in raw for name in CURRENT_SESSION_COOKIES + LEGACY_SESSION_COOKIES):
        return raw
    if raw.startswith(("{", "[")):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, list):
            pairs = [
                f"{item['name']}={item['value']}"
                for item in parsed
                if isinstance(item, dict) and item.get("name") and item.get("value") is not None
            ]
            cookie = "; ".join(pairs)
            return cookie if session_kind(cookie) else None
        if isinstance(parsed, dict) and parsed.get("token"):
            return f"koa:sess={parsed['token']}"
        return None
    if raw.count(".") == 2 and "=" not in raw and len(raw) > 50:
        return "koa:sess=" + raw
    return raw


def load_cookies():
    raw = os.environ.get("GLADOS_COOKIE", "").strip()
    if not raw:
        log("未配置 GLADOS_COOKIE")
        return []
    if raw.startswith(("{", "[")):
        cookie = parse_cookie(raw)
        return [cookie] if cookie else []
    sep = "\n" if "\n" in raw else "&"
    return [cookie for item in raw.split(sep) if (cookie := parse_cookie(item))]


def is_success(result):
    if not isinstance(result, dict):
        return False
    message = str(result.get("message", "")).lower()
    return result.get("code") == 0 or any(marker in message for marker in SUCCESS_MARKERS)


def is_auth_failure(result):
    """无权限、设备不匹配、风控拦截属于认证失败，重试无意义。"""
    if not isinstance(result, dict):
        return False
    message = str(result.get("message", "")).lower()
    reason = str(result.get("reason", "")).lower()
    return (
        result.get("code") == -2
        or reason == "device-mismatch"
        or any(marker in message for marker in AUTH_FAIL_MARKERS)
    )


class GLaDOSClient:
    """API 客户端：复用连接，成功域名优先。"""

    def __init__(self, cookie):
        if session_kind(cookie) != "gld":
            log("Cookie 缺少 gld:sess / gld:sess.sig，新版接口可能返回没有权限")
        self.http = requests.Session()
        self.http.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "Cookie": cookie,
                **BROWSER_HEADERS,
            }
        )
        self.domain_pos = 0

    def request(self, method, path, payload=None, form=False):
        for offset in range(len(DOMAINS)):
            index = (self.domain_pos + offset) % len(DOMAINS)
            domain = DOMAINS[index]
            headers = {"Origin": domain, "Referer": f"{domain}/console/checkin"}
            if form:
                # 兑换接口要求表单编码，交给 requests 自动设置 Content-Type
                kwargs = {"data": payload}
            else:
                headers["Content-Type"] = "application/json;charset=UTF-8"
                kwargs = {"json": payload}
            try:
                resp = self.http.request(
                    method, domain + path, headers=headers, timeout=REQUEST_TIMEOUT, **kwargs
                )
            except requests.RequestException as err:
                log(f"{domain} 请求失败: {type(err).__name__}")
                continue
            if resp.status_code != 200:
                log(f"{domain} 返回 HTTP {resp.status_code}")
                continue
            self.domain_pos = index
            try:
                return resp.json()
            except ValueError:
                log(f"{domain} 响应解析失败")
        return None

    def checkin(self):
        return self.request("POST", "/api/user/checkin", {"token": CHECKIN_TOKEN})

    def status(self):
        return self.request("GET", "/api/user/status")

    def points(self):
        return self.request("GET", "/api/user/points")

    def exchange(self, plan_type):
        return self.request("POST", "/api/user/exchange", {"planType": plan_type}, form=True)


def checkin_with_retry(client, attempts=MAX_ATTEMPTS, delay=RETRY_DELAY_SECONDS):
    attempts = max(1, attempts)
    result = None
    for attempt in range(1, attempts + 1):
        result = client.checkin()
        if is_success(result):
            return result, True
        if is_auth_failure(result):
            if isinstance(result, dict) and result.get("reason") == "device-mismatch":
                message = "登录设备不匹配，请重新登录并更新完整 Cookie"
            else:
                message = result.get("message", "认证失败")
            log(f"签到认证失败，不再重试: {message}")
            return result, False
        if attempt < attempts:
            log(f"签到第 {attempt}/{attempts} 次失败，{delay} 秒后重试")
            time.sleep(delay)
    return result, False


def fetch_summary(client):
    """读取积分、剩余天数与兑换选项；base_points 供兑换判断使用。"""
    summary = {
        "email": "?",
        "left_days": "?",
        "points": "?",
        "change": "",
        "options": "",
        "base_points": None,
    }

    data = (client.status() or {}).get("data") or {}
    if data:
        summary["email"] = data.get("email", "?")
        summary["left_days"] = str(data.get("leftDays", "?")).split(".")[0]

    res = client.points()
    if not res or "points" not in res:
        return summary

    summary["points"] = str(res.get("points", "0")).split(".")[0]
    try:
        current = int(summary["points"])
    except ValueError:
        current = 0
    summary["base_points"] = current

    history = res.get("history") or []
    if history:
        change = str(history[0].get("change", "0")).split(".")[0]
        summary["change"] = change if change.startswith("-") else f"+{change}"

    lines = []
    for plan in (res.get("plans") or {}).values():
        need, days = plan.get("points", 0), plan.get("days", "?")
        if current >= need:
            lines.append(f"{need}分->{days}天 (可兑换)")
        else:
            lines.append(f"{need}分->{days}天 (差{need - current}分)")
    summary["options"] = "\n".join(lines)
    return summary


def try_exchange(client, plan_key, base_points):
    """积分达标时自动兑换，返回 (状态, 文本)，状态为 ok/skip/fail。"""
    plan_type, need, days = EXCHANGE_PLANS[plan_key]
    if base_points is None or base_points < need:
        shown = base_points if base_points is not None else "?"
        return "skip", f"积分不足({shown}/{need}分)"
    res = client.exchange(plan_type)
    if res and str(res.get("code")) == "0":
        return "ok", f"兑换成功 +{days}天"
    message = " ".join(str((res or {}).get("message", "未知错误")).split())[:120]
    return "fail", f"兑换失败: {message}"


def push_meow(title, content):
    safe_title = quote(title.replace("：", "-").replace(":", "-").replace("/", "-"))
    url = f"https://api.chuckfang.com/{MEOW_NICKNAME}/{safe_title}/{quote(content, safe='')}"
    try:
        resp = requests.get(url, params={"msgType": "text"}, timeout=5)
        result = resp.json()
        if result.get("status") == 200 or result.get("data") is True:
            log(f"推送成功 (HTTP {resp.status_code})")
        else:
            log("推送失败")
    except (requests.RequestException, ValueError) as err:
        log(f"推送失败: {type(err).__name__}")


def diagnose(cookies):
    log("只读诊断：仅查询登录状态，不签到、不兑换、不推送")
    passed = 0
    for i, cookie in enumerate(cookies, 1):
        result = GLaDOSClient(cookie).status()
        data = (result or {}).get("data")
        ok = (
            isinstance(result, dict)
            and result.get("code") == 0
            and isinstance(data, dict)
            and bool(data)
        )
        if ok:
            passed += 1
            log(f"账号 {i}: 登录状态查询成功")
        elif is_auth_failure(result):
            log(f"账号 {i}: 会话认证被拒绝，请重新登录并更新完整 Cookie")
        else:
            log(f"账号 {i}: 登录状态查询失败，请检查网络并重新登录更新 Cookie")
    return 0 if passed == len(cookies) else 1


def main():
    log("2026 GLaDOS Checkin Starting...")
    cookies = load_cookies()
    if not cookies:
        return 1

    if os.environ.get("CHECKIN_DIAGNOSTIC_ONLY", "").strip().lower() in ("1", "true", "yes"):
        return diagnose(cookies)

    plan_key = os.environ.get("GLADOS_EXCHANGE_PLAN", "").strip()
    if plan_key and plan_key not in EXCHANGE_PLANS:
        log("GLADOS_EXCHANGE_PLAN 值无效，可选 100/200/500，本次跳过兑换")
        plan_key = None
    if plan_key:
        _, need, days = EXCHANGE_PLANS[plan_key]
        log(f"已启用自动兑换: {need} 积分 -> {days} 天")

    blocks = []
    success_count = 0

    for i, cookie in enumerate(cookies, 1):
        client = GLaDOSClient(cookie)
        result, ok = checkin_with_retry(client)
        message = result.get("message", "Failure") if result else "Network Error"
        summary = fetch_summary(client)

        if plan_key:
            state, text = try_exchange(client, plan_key, summary["base_points"])
            if state == "ok":
                log(f"账号 {i} | {text}")
                summary = fetch_summary(client)
            elif state == "fail":
                log(f"账号 {i} | {text}")
                summary["options"] = f"{summary['options']}\n{text}".strip()
            else:
                log(f"账号 {i} | 积分不足，跳过兑换：{text}")

        log(f"账号 {i} | 积分：{summary['points']} | 天数：{summary['left_days']} | 结果：{message}")
        if ok:
            success_count += 1

        blocks.append(
            f"账号 {summary['email']}\n"
            f"当前积分：{summary['points']} ({summary['change']})\n"
            f"剩余天数：{summary['left_days']} 天\n"
            f"签到结果：{message}\n"
            f"兑换选项:\n{summary['options']}"
        )

    if os.environ.get("PUSH_LEVEL", "all").lower() == "fail_only" and success_count == len(cookies):
        log("所有账号签到成功，按 PUSH_LEVEL=fail_only 跳过推送")
        return 0

    beijing = datetime.now(timezone.utc) + timedelta(hours=8)
    title = f"GLaDOS 签到：成功{success_count}/{len(cookies)}"
    content = "\n\n".join(blocks) + f"\n时间：{beijing:%Y-%m-%d %H:%M:%S} (北京时间)"
    push_meow(title, content)

    return 0 if success_count == len(cookies) else 1


if __name__ == "__main__":
    sys.exit(main())
