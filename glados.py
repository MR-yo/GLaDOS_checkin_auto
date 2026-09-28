import json
import os
import re
import sys
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

SAVE_DEBUG_SCREENSHOT = (
    os.environ.get("SAVE_DEBUG_SCREENSHOT", "0") == "1"
)

RISK_CONTROL_KEYWORDS = (
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
        github_warning(
            "PushPlus推送失败",
            str(e),
        )


# =============================================================================
# Cookie
# =============================================================================

def get_accounts():
    """
    多账号格式保持和你原来一致：

    GLADOS_COOKIE:
        cookie1&cookie2&cookie3
    """

    raw = os.environ.get("GLADOS_COOKIE", "").strip()

    if not raw:
        return []

    return [
        item.strip()
        for item in raw.split("&")
        if item.strip()
    ]


def parse_cookie_header(cookie_string):
    """
    把：

        koa:sess=xxx; koa:sess.sig=yyy

    转成 Playwright cookies。
    """

    cookies = []

    ignored_names = {
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

        if not name:
            continue

        if name.lower() in ignored_names:
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
# GLaDOS 页面状态
# =============================================================================

def get_status(page):
    """
    状态请求仍然通过浏览器页面执行。

    注意这里不是 requests 请求，而是：

        Chromium
            ↓
        glados.cloud 页面
            ↓
        window.fetch('/api/user/status')

    因此 Cookie、Origin、浏览器上下文都属于实际网页 Session。
    """

    try:
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
                    } catch (e) {
                    }

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

        return result

    except Exception as e:
        return {
            "ok": False,
            "status": 0,
            "data": None,
            "text": str(e),
        }


def parse_account_status(result):
    if not result:
        return None, "状态接口没有返回结果"

    status_code = result.get("status", 0)

    if not result.get("ok"):
        return (
            None,
            f"状态接口 HTTP {status_code}: "
            f"{result.get('text', '')}",
        )

    state = result.get("data")

    if not isinstance(state, dict):
        return None, "状态接口返回非 JSON Object"

    data = state.get("data")

    if not isinstance(data, dict):
        message = (
            state.get("message")
            or state.get("msg")
            or "接口未返回 data"
        )

        return None, message

    email = (
        data.get("email")
        or "未知账号"
    )

    left_days = data.get("leftDays")

    if left_days is None:
        left_days_text = "未知"
    else:
        try:
            left_days_text = str(left_days).split(".")[0]
        except Exception:
            left_days_text = str(left_days)

    return {
        "email": email,
        "left_days": left_days_text,
    }, None


# =============================================================================
# 签到按钮
# =============================================================================

def find_checkin_button(page):
    """
    多套 selector：

    1. button role + 签到
    2. button text
    3. Semantic UI class
    """

    candidates = [
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

    for locator in candidates:
        try:
            count = locator.count()

            for i in range(count):
                item = locator.nth(i)

                if item.is_visible():
                    return item

        except Exception:
            continue

    return None


def wait_for_checkin_button(page):
    """
    React 页面加载可能稍慢。
    最多等待约 20 秒。
    """

    for _ in range(20):
        button = find_checkin_button(page)

        if button is not None:
            return button

        page.wait_for_timeout(1000)

    return None


# =============================================================================
# 签到结果
# =============================================================================

def extract_message(data):
    if not isinstance(data, dict):
        return ""

    return str(
        data.get("message")
        or data.get("msg")
        or ""
    ).strip()


def is_risk_control(message):
    message = message.lower()

    return any(
        keyword in message
        for keyword in RISK_CONTROL_KEYWORDS
    )


def classify_checkin_result(data):
    """
    返回：

        success
        repeat
        risk
        error
    """

    if not isinstance(data, dict):
        return "error", "签到接口返回格式异常"

    message = extract_message(data)
    lower = message.lower()

    # -------------------------
    # 风控必须优先判断
    # -------------------------

    if is_risk_control(message):
        return "risk", message

    # -------------------------
    # 重复签到
    # -------------------------

    if any(
        keyword in lower
        for keyword in REPEAT_KEYWORDS
    ):
        return "repeat", message

    # -------------------------
    # 明确成功
    # -------------------------

    if any(
        keyword in lower
        for keyword in SUCCESS_KEYWORDS
    ):
        return "success", message

    # GLaDOS 历史上 code == 0 表示正常成功
    if data.get("code") == 0:
        return (
            "success",
            message or "签到成功",
        )

    # -------------------------
    # 未知结果
    # -------------------------

    return (
        "error",
        message
        or f"未知签到结果: {json.dumps(data, ensure_ascii=False)}",
    )


# =============================================================================
# 调试截图
# =============================================================================

def save_debug_screenshot(page, account_index):
    if not SAVE_DEBUG_SCREENSHOT:
        return

    try:
        directory = Path("debug")

        directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        path = (
            directory
            / f"account_{account_index}_failure.png"
        )

        page.screenshot(
            path=str(path),
            full_page=True,
        )

        log(
            f"已保存调试截图: {path}"
        )

    except Exception as e:
        github_warning(
            "调试截图失败",
            str(e),
        )


# =============================================================================
# 单账号
# =============================================================================

def process_account(
    browser,
    cookie_string,
    account_index,
):
    """
    返回：

        {
            "error": bool,
            "risk": bool,
            "text": str,
        }
    """

    context = browser.new_context()

    page = None

    try:
        # ---------------------------------------------------------------------
        # 注入 Cookie
        # ---------------------------------------------------------------------

        cookies = parse_cookie_header(
            cookie_string
        )

        if not cookies:
            return {
                "error": True,
                "risk": False,
                "text": (
                    f"账号{account_index}"
                    "----Cookie格式异常"
                ),
            }

        context.add_cookies(cookies)

        # ---------------------------------------------------------------------
        # 打开真实网页
        # ---------------------------------------------------------------------

        page = context.new_page()

        page.set_default_timeout(
            PAGE_TIMEOUT
        )

        log(
            f"账号 {account_index}: "
            "打开 GLaDOS 签到页面"
        )

        page.goto(
            CHECKIN_PAGE,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT,
        )

        # React 初始化
        try:
            page.wait_for_load_state(
                "networkidle",
                timeout=10_000,
            )
        except PlaywrightTimeoutError:
            # networkidle 不是必须条件
            pass

        page.wait_for_timeout(1000)

        # ---------------------------------------------------------------------
        # 先验证登录状态
        # ---------------------------------------------------------------------

        status_result = get_status(page)

        account, status_error = (
            parse_account_status(
                status_result
            )
        )

        if status_error:
            github_error(
                "GLaDOS登录状态异常",
                (
                    f"账号 {account_index}: "
                    f"{status_error}"
                ),
            )

            save_debug_screenshot(
                page,
                account_index,
            )

            return {
                "error": True,
                "risk": False,
                "text": (
                    f"账号{account_index}"
                    f"----登录状态异常"
                    f"----{status_error}"
                ),
            }

        email = account["email"]
        left_days = account["left_days"]

        log(
            f"{email}: 登录状态正常，"
            f"剩余 {left_days} 天"
        )

        # ---------------------------------------------------------------------
        # 查找签到按钮
        # ---------------------------------------------------------------------

        button = wait_for_checkin_button(
            page
        )

        if button is None:
            github_error(
                "未找到签到按钮",
                email,
            )

            save_debug_screenshot(
                page,
                account_index,
            )

            return {
                "error": True,
                "risk": False,
                "text": (
                    f"{email}"
                    "----未找到网页签到按钮"
                    f"----剩余({left_days})天"
                ),
            }

        log(
            f"{email}: 找到签到按钮，"
            "准备执行网页点击"
        )

        # ---------------------------------------------------------------------
        # 关键：
        #
        # 不直接 POST API。
        #
        # 而是：
        #
        # 浏览器 click()
        #       ↓
        # GLaDOS 前端 JS
        #       ↓
        # /api/user/checkin
        #
        # 同时监听页面产生的响应。
        # ---------------------------------------------------------------------

        try:
            with page.expect_response(
                lambda response:
                "/api/user/checkin"
                in response.url
                and response.request.method.upper()
                == "POST",
                timeout=CHECKIN_TIMEOUT,
            ) as response_info:

                button.click(
                    timeout=10_000
                )

            checkin_response = (
                response_info.value
            )

        except PlaywrightTimeoutError:
            github_error(
                "签到响应超时",
                (
                    f"{email}: "
                    "点击按钮后未检测到"
                    "签到接口响应"
                ),
            )

            save_debug_screenshot(
                page,
                account_index,
            )

            return {
                "error": True,
                "risk": False,
                "text": (
                    f"{email}"
                    "----签到响应超时"
                    f"----剩余({left_days})天"
                ),
            }

        # ---------------------------------------------------------------------
        # HTTP 状态
        # ---------------------------------------------------------------------

        if not checkin_response.ok:
            github_error(
                "GLaDOS签到HTTP异常",
                (
                    f"{email}: "
                    f"HTTP "
                    f"{checkin_response.status}"
                ),
            )

            save_debug_screenshot(
                page,
                account_index,
            )

            return {
                "error": True,
                "risk": False,
                "text": (
                    f"{email}"
                    "----签到HTTP异常"
                    f"----HTTP "
                    f"{checkin_response.status}"
                    f"----剩余({left_days})天"
                ),
            }

        # ---------------------------------------------------------------------
        # 读取网页签到产生的 JSON
        # ---------------------------------------------------------------------

        try:
            checkin_data = (
                checkin_response.json()
            )

        except Exception:
            try:
                text = (
                    checkin_response
                    .body()
                    .decode(
                        "utf-8",
                        errors="replace",
                    )
                )

            except Exception:
                text = ""

            github_error(
                "GLaDOS签到返回异常",
                (
                    f"{email}: "
                    "返回内容不是 JSON "
                    f"{text[:200]}"
                ),
            )

            save_debug_screenshot(
                page,
                account_index,
            )

            return {
                "error": True,
                "risk": False,
                "text": (
                    f"{email}"
                    "----签到接口返回异常"
                    f"----剩余({left_days})天"
                ),
            }

        # ---------------------------------------------------------------------
        # 判断签到结果
        # ---------------------------------------------------------------------

        result_type, message = (
            classify_checkin_result(
                checkin_data
            )
        )

        # ---------------------------------------------------------------------
        # 风控
        # ---------------------------------------------------------------------

        if result_type == "risk":
            github_error(
                "GLaDOS触发自动化检测",
                f"{email}: {message}",
            )

            save_debug_screenshot(
                page,
                account_index,
            )

            return {
                "error": True,
                "risk": True,
                "text": (
                    f"{email}"
                    "----签到失败"
                    f"----{message}"
                    f"----剩余({left_days})天"
                ),
            }

        # ---------------------------------------------------------------------
        # 未知异常
        # ---------------------------------------------------------------------

        if result_type == "error":
            github_error(
                "GLaDOS签到结果异常",
                f"{email}: {message}",
            )

            log(
                "签到接口返回: "
                + json.dumps(
                    checkin_data,
                    ensure_ascii=False,
                )[:500]
            )

            save_debug_screenshot(
                page,
                account_index,
            )

            return {
                "error": True,
                "risk": False,
                "text": (
                    f"{email}"
                    "----签到异常"
                    f"----{message}"
                    f"----剩余({left_days})天"
                ),
            }

        # ---------------------------------------------------------------------
        # 签到完成后重新读取状态
        # ---------------------------------------------------------------------

        page.wait_for_timeout(1000)

        new_status = get_status(page)

        new_account, new_status_error = (
            parse_account_status(
                new_status
            )
        )

        if new_account:
            left_days = (
                new_account["left_days"]
            )

        elif new_status_error:
            github_warning(
                "签到后状态刷新失败",
                (
                    f"{email}: "
                    f"{new_status_error}"
                ),
            )

        # ---------------------------------------------------------------------
        # 正常
        # ---------------------------------------------------------------------

        if result_type == "repeat":
            log(
                f"{email}: 今日已经签到"
            )

        else:
            log(
                f"{email}: 签到成功"
            )

        return {
            "error": False,
            "risk": False,
            "text": (
                f"{email}"
                f"----{message}"
                f"----剩余({left_days})天"
            ),
        }

    except PlaywrightTimeoutError as e:
        github_error(
            "Playwright超时",
            (
                f"账号 {account_index}: "
                f"{e}"
            ),
        )

        if page:
            save_debug_screenshot(
                page,
                account_index,
            )

        return {
            "error": True,
            "risk": False,
            "text": (
                f"账号{account_index}"
                "----Playwright超时"
            ),
        }

    except Exception as e:
        github_error(
            "GLaDOS签到脚本异常",
            (
                f"账号 {account_index}: "
                f"{type(e).__name__}: {e}"
            ),
        )

        if page:
            save_debug_screenshot(
                page,
                account_index,
            )

        return {
            "error": True,
            "risk": False,
            "text": (
                f"账号{account_index}"
                "----执行异常"
                f"----{type(e).__name__}: {e}"
            ),
        }

    finally:
        context.close()


# =============================================================================
# Main
# =============================================================================

def main():
    pushplus_token = os.environ.get(
        "PUSHPLUS_TOKEN",
        "",
    )

    accounts = get_accounts()

    if not accounts:
        github_error(
            "配置错误",
            "未获取到 GLADOS_COOKIE",
        )

        return 1

    has_error = False
    risk_control_triggered = False

    results = []

    log(
        f"共发现 {len(accounts)} 个账号"
    )

    # =========================================================================
    # Playwright
    # =========================================================================

    with sync_playwright() as playwright:

        browser = playwright.chromium.launch(
            headless=False,
            channel="chromium",
        )

        try:
            for index, cookie in enumerate(
                accounts,
                start=1,
            ):
                log("")
                log("=" * 60)
                log(
                    f"开始处理第 {index} 个账号"
                )
                log("=" * 60)

                # -------------------------------------------------------------
                # 如果前一个账号已经明确触发：
                #
                # Automated check-in detected
                #
                # 则本次 GitHub Runner 不继续签到剩余账号。
                #
                # 防止继续请求让同一个 Runner 出口产生更多风控事件。
                # -------------------------------------------------------------

                if risk_control_triggered:
                    message = (
                        f"账号{index}"
                        "----本次跳过"
                        "----前序账号已触发"
                        "自动化检测"
                    )

                    log(message)

                    results.append(message)

                    continue

                result = process_account(
                    browser,
                    cookie,
                    index,
                )

                results.append(
                    result["text"]
                )

                if result["error"]:
                    has_error = True

                if result["risk"]:
                    risk_control_triggered = True

        finally:
            browser.close()

    # =========================================================================
    # PushPlus
    # =========================================================================

    if results:

        if risk_control_triggered:
            title = (
                "GLaDOS 检测到自动签到"
            )

        elif has_error:
            title = (
                "GLaDOS 签到异常"
            )

        else:
            title = (
                "GLaDOS 签到成功"
            )

        pushplus(
            pushplus_token,
            title,
            "\n".join(results),
        )

    # =========================================================================
    # GitHub Actions Exit Code
    # =========================================================================

    log("")
    log("=" * 60)

    if has_error:
        log(
            "本次签到存在异常，"
            "GitHub Actions 将标记为失败"
        )

        log("=" * 60)

        return 1

    log(
        "所有账号执行正常"
    )

    log("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
