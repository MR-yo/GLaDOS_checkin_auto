import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import requests
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


# =============================================================================
# 配置
# =============================================================================

BASE_URL = "https://glados.cloud"
CHECKIN_PAGE = f"{BASE_URL}/console/checkin"

PAGE_TIMEOUT = 30_000
CHECKIN_TIMEOUT = 20_000

# 默认使用 headed 模式，保持你当前 GitHub macOS Runner 的行为。
# 如需 headless：
# HEADLESS=1 python glados.py
HEADLESS = os.environ.get("HEADLESS", "0") == "1"

SAVE_DEBUG_SCREENSHOT = (
    os.environ.get("SAVE_DEBUG_SCREENSHOT", "0") == "1"
)

RISK_KEYWORDS = (
    "automated check-in",
    "automated checkin",
    "sign in again",
    "login again",
    "log in again",
    "please login",
    "please log in",
    "session expired",
    "cookie expired",
    "invalid session",
    "unauthorized",
)

REPEAT_KEYWORDS = (
    "checkin repeats",
    "check-in repeats",
    "try tomorrow",
)

SUCCESS_KEYWORDS = (
    "checkin! got",
    "check-in! got",
    "today's observation logged",
)


# =============================================================================
# 类型
# =============================================================================

@dataclass
class AccountResult:
    text: str
    error: bool = False
    risk: bool = False


class CheckinError(Exception):
    pass


class RiskControlError(CheckinError):
    pass


# =============================================================================
# 日志
# =============================================================================

def log(message):
    print(message, flush=True)


def github_error(title, message):
    message = str(message).replace("\n", " ")
    log(f"::error title={title}::{message}")


def github_warning(title, message):
    message = str(message).replace("\n", " ")
    log(f"::warning title={title}::{message}")


# =============================================================================
# PushPlus
# =============================================================================

def pushplus(token, title, content):
    if not token:
        return

    try:
        response = requests.get(
            "https://www.pushplus.plus/send",
            params={
                "token": token,
                "title": title,
                "content": content,
            },
            timeout=10,
        )

        if not response.ok:
            github_warning(
                "PushPlus推送失败",
                f"HTTP {response.status_code}",
            )

    except requests.RequestException as e:
        github_warning("PushPlus推送失败", e)


# =============================================================================
# Cookie
# =============================================================================

def get_accounts():
    """
    GLADOS_COOKIE:

    单账号：
        cookie1

    多账号：
        cookie1&cookie2&cookie3
    """
    raw = os.environ.get("GLADOS_COOKIE", "").strip()

    if not raw:
        return []

    return [
        cookie.strip()
        for cookie in raw.split("&")
        if cookie.strip()
    ]


def parse_cookie(cookie_string):
    cookies = []

    ignored = {
        "path",
        "domain",
        "expires",
        "max-age",
        "secure",
        "httponly",
        "samesite",
    }

    for part in cookie_string.split(";"):
        part = part.strip()

        if not part or "=" not in part:
            continue

        name, value = part.split("=", 1)

        name = name.strip()
        value = value.strip()

        if not name or name.lower() in ignored:
            continue

        cookies.append({
            "name": name,
            "value": value,
            "domain": "glados.cloud",
            "path": "/",
            "secure": True,
        })

    return cookies


# =============================================================================
# 账号状态
# =============================================================================

def get_account_status(page):
    """
    在真实浏览器页面上下文中请求 /api/user/status。
    """

    result = page.evaluate(
        """
        async () => {
            try {
                const response = await fetch(
                    '/api/user/status',
                    {
                        method: 'GET',
                        credentials: 'include',
                        headers: {
                            'Accept': 'application/json, text/plain, */*'
                        }
                    }
                );

                const text = await response.text();

                let data = null;

                try {
                    data = JSON.parse(text);
                } catch (_) {}

                return {
                    ok: response.ok,
                    status: response.status,
                    data: data,
                    text: text.substring(0, 500)
                };

            } catch (e) {
                return {
                    ok: false,
                    status: 0,
                    data: null,
                    text: String(e)
                };
            }
        }
        """
    )

    if not result or not result.get("ok"):
        status = result.get("status", 0) if result else 0
        text = result.get("text", "") if result else ""

        raise CheckinError(
            f"状态接口异常 HTTP {status}: {text}"
        )

    state = result.get("data")

    if not isinstance(state, dict):
        raise CheckinError("状态接口返回格式异常")

    data = state.get("data")

    if not isinstance(data, dict):
        message = (
            state.get("message")
            or state.get("msg")
            or "接口未返回 data"
        )

        raise CheckinError(message)

    email = data.get("email") or "未知账号"

    left_days = data.get("leftDays")

    if left_days is None:
        left_days = "未知"
    else:
        left_days = str(left_days).split(".")[0]

    return email, left_days


# =============================================================================
# 签到按钮
# =============================================================================

def find_checkin_button(page):
    selectors = [
        page.get_by_role(
            "button",
            name=re.compile(
                r"^\s*(签到|Checkin|Check In)\s*$",
                re.IGNORECASE,
            ),
        ),

        page.locator("button").filter(
            has_text=re.compile(
                r"^\s*(签到|Checkin|Check In)\s*$",
                re.IGNORECASE,
            )
        ),

        page.locator(".ui.green.huge.button"),
        page.locator(".ui.positive.button"),
    ]

    for locator in selectors:
        try:
            for i in range(locator.count()):
                button = locator.nth(i)

                if button.is_visible():
                    return button

        except Exception:
            pass

    return None


def wait_for_checkin_button(page):
    for _ in range(20):
        button = find_checkin_button(page)

        if button:
            return button

        page.wait_for_timeout(1000)

    raise CheckinError("未找到网页签到按钮")


# =============================================================================
# 签到结果
# =============================================================================

def classify_checkin(data):
    if not isinstance(data, dict):
        raise CheckinError("签到接口返回格式异常")

    message = str(
        data.get("message")
        or data.get("msg")
        or ""
    ).strip()

    lower = message.lower()

    # 风控优先级最高
    if any(keyword in lower for keyword in RISK_KEYWORDS):
        raise RiskControlError(message or "检测到自动化签到")

    # 已签到属于正常结果
    if any(keyword in lower for keyword in REPEAT_KEYWORDS):
        return "repeat", message

    # 明确签到成功
    if any(keyword in lower for keyword in SUCCESS_KEYWORDS):
        return "success", message

    # 历史接口 code == 0 也视为成功
    if data.get("code") == 0:
        return "success", message or "签到成功"

    raise CheckinError(
        message
        or f"未知签到结果: {json.dumps(data, ensure_ascii=False)}"
    )


# =============================================================================
# 调试
# =============================================================================

def save_screenshot(page, account_index):
    if not SAVE_DEBUG_SCREENSHOT or page is None:
        return

    try:
        directory = Path("debug")
        directory.mkdir(exist_ok=True)

        path = directory / f"account_{account_index}_failure.png"

        page.screenshot(
            path=str(path),
            full_page=True,
        )

        log(f"调试截图: {path}")

    except Exception as e:
        github_warning("保存截图失败", e)


# =============================================================================
# 单账号签到
# =============================================================================

def process_account(browser, cookie_string, index):
    context = browser.new_context()
    page = None

    email = f"账号{index}"
    left_days = "未知"

    try:
        cookies = parse_cookie(cookie_string)

        if not cookies:
            raise CheckinError("Cookie 格式异常")

        context.add_cookies(cookies)

        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)

        log(f"{email}: 打开 GLaDOS 签到页面")

        page.goto(
            CHECKIN_PAGE,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT,
        )

        try:
            page.wait_for_load_state(
                "networkidle",
                timeout=10_000,
            )
        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(1000)

        # ---------------------------------------------------------------------
        # 登录状态
        # ---------------------------------------------------------------------

        email, left_days = get_account_status(page)

        log(
            f"{email}: 登录状态正常，"
            f"剩余 {left_days} 天"
        )

        # ---------------------------------------------------------------------
        # 找签到按钮
        # ---------------------------------------------------------------------

        button = wait_for_checkin_button(page)

        log(
            f"{email}: 找到签到按钮，准备执行网页点击"
        )

        # ---------------------------------------------------------------------
        # 点击真实网页按钮，并监听网页自己产生的签到请求
        # ---------------------------------------------------------------------

        try:
            with page.expect_response(
                lambda response:
                    "/api/user/checkin" in response.url
                    and response.request.method.upper() == "POST",
                timeout=CHECKIN_TIMEOUT,
            ) as response_info:

                button.click(timeout=10_000)

            response = response_info.value

        except PlaywrightTimeoutError:
            raise CheckinError(
                "点击按钮后未检测到签到接口响应"
            )

        # ---------------------------------------------------------------------
        # HTTP
        # ---------------------------------------------------------------------

        if not response.ok:
            raise CheckinError(
                f"签到接口 HTTP {response.status}"
            )

        # ---------------------------------------------------------------------
        # JSON
        # ---------------------------------------------------------------------

        try:
            checkin_data = response.json()

        except Exception:
            try:
                body = response.body().decode(
                    "utf-8",
                    errors="replace",
                )
            except Exception:
                body = ""

            raise CheckinError(
                f"签到接口返回非 JSON 数据: {body[:300]}"
            )

        # ---------------------------------------------------------------------
        # 关键新增：
        # 无论成功、重复签到还是风控，都打印服务端完整响应
        # ---------------------------------------------------------------------

        log(
            f"{email}: 签到响应 "
            f"HTTP {response.status} = "
            f"{json.dumps(checkin_data, ensure_ascii=False)}"
        )

        # ---------------------------------------------------------------------
        # 判断业务结果
        # ---------------------------------------------------------------------

        result_type, message = classify_checkin(
            checkin_data
        )

        if result_type == "repeat":
            log(f"{email}: 今日已经签到")
        else:
            log(f"{email}: 签到成功")

        # ---------------------------------------------------------------------
        # 签到后重新获取剩余天数
        # ---------------------------------------------------------------------

        try:
            page.wait_for_timeout(1000)

            _, new_left_days = get_account_status(page)

            left_days = new_left_days

        except CheckinError as e:
            github_warning(
                "签到后状态刷新失败",
                f"{email}: {e}",
            )

        return AccountResult(
            text=(
                f"{email}"
                f"----{message}"
                f"----剩余({left_days})天"
            )
        )

    except RiskControlError as e:
        message = str(e)

        github_error(
            "GLaDOS触发自动化检测",
            f"{email}: {message}",
        )

        save_screenshot(page, index)

        return AccountResult(
            text=(
                f"{email}"
                f"----签到失败"
                f"----{message}"
                f"----剩余({left_days})天"
            ),
            error=True,
            risk=True,
        )

    except CheckinError as e:
        github_error(
            "GLaDOS签到异常",
            f"{email}: {e}",
        )

        save_screenshot(page, index)

        return AccountResult(
            text=(
                f"{email}"
                f"----签到异常"
                f"----{e}"
                f"----剩余({left_days})天"
            ),
            error=True,
        )

    except Exception as e:
        github_error(
            "GLaDOS脚本异常",
            f"{email}: {type(e).__name__}: {e}",
        )

        save_screenshot(page, index)

        return AccountResult(
            text=(
                f"{email}"
                f"----执行异常"
                f"----{type(e).__name__}: {e}"
            ),
            error=True,
        )

    finally:
        context.close()


# =============================================================================
# Main
# =============================================================================

def main():
    accounts = get_accounts()

    if not accounts:
        github_error(
            "配置错误",
            "未获取到 GLADOS_COOKIE",
        )
        return 1

    pushplus_token = os.environ.get(
        "PUSHPLUS_TOKEN",
        "",
    )

    log(f"共发现 {len(accounts)} 个账号")

    results = []

    has_error = False
    risk_triggered = False

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=HEADLESS,
            channel="chromium",
        )

        try:
            for index, cookie in enumerate(
                accounts,
                start=1,
            ):
                log("")
                log("=" * 60)
                log(f"开始处理第 {index} 个账号")
                log("=" * 60)

                # 前一个账号已触发明确风控，
                # 当前 Runner 不继续发送签到请求。
                if risk_triggered:
                    message = (
                        f"账号{index}"
                        "----本次跳过"
                        "----前序账号已触发自动化检测"
                    )

                    log(message)
                    results.append(message)

                    continue

                result = process_account(
                    browser,
                    cookie,
                    index,
                )

                results.append(result.text)

                has_error |= result.error
                risk_triggered |= result.risk

        finally:
            browser.close()

    # -------------------------------------------------------------------------
    # PushPlus
    # -------------------------------------------------------------------------

    if risk_triggered:
        title = "GLaDOS 检测到自动签到"
    elif has_error:
        title = "GLaDOS 签到异常"
    else:
        title = "GLaDOS 签到成功"

    if results:
        pushplus(
            pushplus_token,
            title,
            "\n".join(results),
        )

    # -------------------------------------------------------------------------
    # GitHub Actions 返回值
    # -------------------------------------------------------------------------

    log("")
    log("=" * 60)

    if has_error:
        log(
            "本次签到存在异常，"
            "GitHub Actions 将标记为失败"
        )

        log("=" * 60)
        return 1

    log("所有账号执行正常")
    log("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())