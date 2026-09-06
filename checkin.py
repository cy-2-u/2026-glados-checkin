#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2026 GLaDOS 自动签到 (积分增强版)

功能：
- 全自动签到
- 精准获取当前积分 (Points)
- MeoW Webhook 推送（包含积分、剩余天数、签到结果）
- 智能多域名切换 (优先 glados.cloud)
- 支持 Cookie-Editor 导出格式
"""

import requests
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

# Fix Windows Unicode Output
if sys.platform.startswith('win'):
    sys.stdout.reconfigure(encoding='utf-8')

# ================= 配置 =================

# 域名优先级：Cloud 第一
DOMAINS = [
    "https://glados.cloud",
    "https://glados.rocks", 
    "https://glados.network",
]

# 积分兑换档位：key 为环境变量 GLADOS_EXCHANGE_PLAN 的取值
# （100 分→10 天 / 200 分→30 天 / 500 分→100 天）
EXCHANGE_PLANS = {
    "100": {"type": "plan100", "points": 100, "days": 10},
    "200": {"type": "plan200", "points": 200, "days": 30},
    "500": {"type": "plan500", "points": 500, "days": 100},
}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Content-Type': 'application/json;charset=UTF-8',
    'Accept': 'application/json, text/plain, */*',
}

# ================= 工具函数 =================

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")

def extract_cookie(raw: str):
    """提取 Cookie，支持 Cookie-Editor 冒号格式"""
    if not raw: return None
    raw = raw.strip()
    
    # Cookie-Editor 格式 (koa:sess=xxx; koa:sess.sig=yyy)
    if 'koa:sess=' in raw or 'koa:sess.sig=' in raw:
        return raw
        
    # JSON
    if raw.startswith('{'):
        try:
            return 'koa.sess=' + json.loads(raw).get('token')
        except: pass
        
    # JWT Token
    if raw.count('.') == 2 and '=' not in raw and len(raw) > 50:
        return 'koa:sess=' + raw
        
    # Standard
    return raw

def get_cookies():
    raw = os.environ.get("GLADOS_COOKIE", "")
    if not raw:
        log("❌ 未配置 GLADOS_COOKIE")
        return []
    
    # Split by enter or &
    sep = '\n' if '\n' in raw else '&'
    return [extract_cookie(c) for c in raw.split(sep) if c.strip()]

# ================= 核心逻辑 =================

class GLaDOS:
    def __init__(self, cookie):
        self.cookie = cookie
        self.domain = DOMAINS[0]
        self.email = "?"
        self.left_days = "?"
        self.points = "?"
        self.points_change = "?"
        self.exchange_info = ""
        self.exchanged_plan = None  # 本次已成功兑换的档位 (plan100/plan200/plan500)
        self.exchange_failure = None  # (plan_type, reason)，仅保留本次运行状态
        self.compare_points = None  # 兑换选项比较用积分；兑换成功后刷新为扣完后的余额
        
    def req(self, method, path, data=None, form=False):
        """带自动域名切换的请求"""
        for d in DOMAINS:
            try:
                url = f"{d}{path}"
                h = HEADERS.copy()
                h['Cookie'] = self.cookie
                h['Origin'] = d
                h['Referer'] = f"{d}/console/checkin"
                
                if method == 'GET':
                    resp = requests.get(url, headers=h, timeout=10)
                elif form:
                    h.pop('Content-Type', None)
                    resp = requests.post(url, headers=h, data=data, timeout=10)
                else:
                    resp = requests.post(url, headers=h, json=data, timeout=10)
                
                if resp.status_code == 200:
                    self.domain = d # Remember working domain
                    return resp.json()
            except Exception as e:
                log(f"⚠️ {d} 请求失败：{e}")
                continue
        return None

    def get_status(self):
        """获取状态：天数、邮箱"""
        res = self.req('GET', '/api/user/status')
        if res and 'data' in res:
            d = res['data']
            self.email = d.get('email', 'Unknown')
            self.left_days = str(d.get('leftDays', '?')).split('.')[0]
            return True
        return False

    def get_points(self):
        """获取积分、变化历史、兑换计划"""
        res = self.req('GET', '/api/user/points')
        if res and 'points' in res:
            # 当前积分
            self.points = str(res.get('points', '0')).split('.')[0]
            try:
                current_pts = int(self.points)
            except (TypeError, ValueError):
                current_pts = 0
            if self.compare_points is None:
                self.compare_points = current_pts
            
            # 最近一次积分变化
            history = res.get('history', [])
            if history:
                last = history[0]
                change = str(last.get('change', '0')).split('.')[0]
                if not change.startswith('-'):
                    change = '+' + change
                self.points_change = change
            
            # 兑换计划：用 compare_points，避免和当前余额混用
            plans = res.get('plans', {})
            pts = self.compare_points
            exchange_lines = []
            for plan_id, plan_data in plans.items():
                need = plan_data['points']
                days = plan_data['days']
                if self.exchanged_plan and plan_id == self.exchanged_plan:
                    exchange_lines.append(f"✅ {need}分→{days}天 (已兑换)")
                elif pts >= need:
                    exchange_lines.append(f"✅ {need}分→{days}天 (可兑换)")
                else:
                    exchange_lines.append(f"❌ {need}分→{days}天 (差{need-pts}分)")
                if self.exchange_failure and plan_id == self.exchange_failure[0]:
                    exchange_lines.append(f"   兑换失败: {self.exchange_failure[1]}")
            self.exchange_info = "\n".join(exchange_lines)
            return True
        return False

    def checkin(self):
        """执行签到"""
        return self.req('POST', '/api/user/checkin', {'token': 'glados.cloud'})

    def exchange(self, plan_type):
        """执行积分兑换，plan_type 为 plan100/plan200/plan500"""
        return self.req('POST', '/api/user/exchange', {'planType': plan_type}, form=True)

    def do_exchange(self, plan):
        """
        按档位自动兑换：积分充足则兑换并刷新积分/天数。
        返回 (success: bool, msg: str, need_exchange: bool)。
        need_exchange 为 True 表示"确实尝试了兑换"（非积分不足跳过）。
        """
        need = plan['points']
        days = plan['days']
        try:
            pts = int(float(self.points))
        except (TypeError, ValueError):
            pts = 0
        if pts < need:
            return False, f"积分不足({pts}/{need}分)", False
        try:
            res = self.exchange(plan['type'])
            if res and str(res.get('code')) == '0':
                self.exchanged_plan = plan['type']
                self.compare_points = None
                self.get_points()
                self.get_status()
                return True, f"兑换成功 +{days}天", True
            msg = self._brief_reason((res or {}).get('message', '未知错误'))
            self.exchange_failure = (plan['type'], msg)
            return False, f"兑换失败: {msg}", True
        except Exception as e:
            msg = self._brief_reason(f"{type(e).__name__} {e}")
            self.exchange_failure = (plan['type'], msg)
            return False, f"兑换失败: {msg}", True

    @staticmethod
    def _brief_reason(reason):
        """压缩兑换失败原因，避免换行或过长文本破坏推送格式。"""
        reason = " ".join(str(reason).split())
        return reason[:120] + "..." if len(reason) > 120 else reason

# ================= 主程序 =================

def webhook_push(title, msg):
    """使用 MeoW webhook API 推送文本消息"""
    nickname = "第五个季节"
    if not nickname: 
        log("⏭️ 未配置昵称，跳过推送")
        return
    try:
        from urllib.parse import quote
        # 标题不能包含特殊字符（冒号、斜杠等）
        safe_title = title.replace("：", "-").replace(":", "-").replace("/", "-")
        encoded_title = quote(safe_title)
        # 消息内容需要 URL 编码
        encoded_msg = quote(msg, safe='')
        url = f"https://api.chuckfang.com/{nickname}/{encoded_title}/{encoded_msg}"
        log(f"推送 URL: {url}")
        resp = requests.get(url, params={'msgType': 'text'}, timeout=5)
        log(f"响应状态码：{resp.status_code}, 内容：{resp.text}")
        result = resp.json()
        if result.get('status') == 200 or result.get('data') == True:
            log("✅ 推送成功")
        else:
            log(f"❌ 推送失败：{result.get('msg', result)}")
    except Exception as e:
        log(f"❌ 推送失败：{e}")

def main():
    log("🚀 2026 GLaDOS Checkin Starting...")
    cookies = get_cookies()
    if not cookies: sys.exit(1)

    # 自动兑换档位（可选）：GLADOS_EXCHANGE_PLAN=100/200/500，
    # 积分达到该档要求后签到时自动兑换；默认关闭不影响现有逻辑
    raw_plan = os.environ.get("GLADOS_EXCHANGE_PLAN", "").strip()
    if raw_plan:
        if raw_plan in EXCHANGE_PLANS:
            exchange_plan = raw_plan
            log(
                f"⚙️ 已启用自动兑换: {raw_plan} 积分 → "
                f"{EXCHANGE_PLANS[raw_plan]['days']} 天"
            )
        else:
            exchange_plan = None
            log(f"⚠️ GLADOS_EXCHANGE_PLAN 值 '{raw_plan}' 无效，可选 100/200/500，本次跳过兑换")
    else:
        exchange_plan = None

    results = []
    success_cnt = 0
    
    for i, cookie in enumerate(cookies, 1):
        g = GLaDOS(cookie)
        
        # 1. Checkin
        res = g.checkin()
        msg = res.get('message', 'Failure') if res else "Network Error"
        
        # 2. Get Info (Refresh data)
        g.get_status()
        g.get_points()

        # 3. Auto Exchange (可选，不影响签到结果)
        #    成功/跳过/失败均记录原因；跳过只在日志中记录，失败原因写入目标档位下方
        if exchange_plan:
            ok, ex_msg, need_exchange = g.do_exchange(EXCHANGE_PLANS[exchange_plan])
            if ok:
                log(f"用户：{g.email} | 兑换成功：{ex_msg}")
            elif not need_exchange:
                log(f"用户：{g.email} | 积分不足，跳过兑换：{ex_msg}")
            else:
                log(f"用户：{g.email} | {ex_msg}")
                # 兑换失败信息需在现有兑换选项区域内展示，不额外发送通知
                g.get_points()

        # 4. Log
        log(f"用户：{g.email} | 积分：{g.points} | 天数：{g.left_days} | 结果：{msg}")
        
        if "Checkin" in msg: success_cnt += 1
        
        # 5. Result Formatting (保持原有推送格式)
        result_text = f"""
👤 {g.email}
当前积分：{g.points} ({g.points_change})
剩余天数：{g.left_days} 天
签到结果：{msg}
🎁 兑换选项:
{g.exchange_info}
"""
        results.append(result_text)

    # Push
    push_level = os.environ.get("PUSH_LEVEL", "all").lower()
    beijing_time = datetime.now(timezone.utc) + timedelta(hours=8)
    ts = beijing_time.strftime('%Y-%m-%d %H:%M:%S')

    if push_level == "fail_only" and success_cnt == len(cookies):
        log("⏭️ 根据 PUSH_LEVEL=fail_only 设置，所有账号签到成功，跳过推送")
        return

    nickname = os.environ.get("MEOW_NICKNAME", "第五个季节")

    if nickname:
        title = f"GLaDOS 签到：成功{success_cnt}/{len(cookies)}"
        content = "\n".join(results)
        content += f"\n时间：{ts} (北京时间)"

        webhook_push(title, content)

if __name__ == '__main__':
    main()
