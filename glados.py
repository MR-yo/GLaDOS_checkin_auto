import json
import os
import sys
import time

import httpx


# =============================================================================
# Configuration
# =============================================================================

BASE_URL = "https://glados.rocks"

STATUS_URL = f"{BASE_URL}/api/user/status"
CHECKIN_URL = f"{BASE_URL}/api/user/checkin"

TIMEOUT = 20.0
MAX_RETRIES = 2

# HAR 中真实值
USER_AGENT = "M"

# HAR 中 POST body 的精确字节
CHECKIN_BODY = b'{"token":"glados.rocks"}'

assert len(CHECKIN_BODY) == 24


# =============================================================================
# HAR 中真实浏览器请求头
#
# 注意：
# HTTP/2 的
#
# :authority
# :method
# :path
# :scheme
#
# 由 httpx 自动生成，不能自己作为普通 Header 添加。
# =============================================================================

COMMON_HEADERS = [
    (
        "accept",
        "application/json, text/plain, */*",
    ),
    (
        "accept-encoding",
        "gzip, deflate, br, zstd",
    ),
    (
        "accept-language",
        "zh,zh-CN;q=0.9,en-US;q=0.8,en;q=0.7",
    ),
    (
        "cache-control",
        "no-cache",
    ),
    (
        "pragma",
        "no-cache",
    ),
    (
        "priority",
        "u=1, i",
    ),
    (
        "sec-ch-ua",
        '""',
    ),
    (
        "sec-ch-ua-mobile",
        "?0",
    ),
    (
        "sec-ch-ua-platform",
        '""',
    ),
    (
        "sec-fetch-dest",
        "empty",
    ),
    (
        "sec-fetch-mode",
        "cors",
    ),
    (
        "sec-fetch-site",
        "same-origin",
    ),
    (
        "user-agent",
        USER_AGENT,
    ),
]


CHECKIN_HEADERS = [
    (
        "accept",
        "application/json, text/plain, */*",
    ),
    (
        "accept-encoding",
        "gzip, deflate, br, zstd",
    ),
    (
        "accept-language",
        "zh,zh-CN;q=0.9,en-US;q=0.8,en;q=0.7",
    ),
    (
        "cache-control",
        "no-cache",
    ),

    # HAR 明确为 24
    (
        "content-length",
        "24",
    ),
    (
        "content-type",
        "application/json;charset=UTF-8",
    ),
    (
        "origin",
        BASE_URL,
    ),
    (
        "pragma",
        "no-cache",
    ),
    (
        "priority",
        "u=1, i",
    ),
    (
        "sec-ch-ua",
        '""',
    ),
    (
        "sec-ch-ua-mobile",
        "?0",
    ),
    (
        "sec-ch-ua-platform",
        '""',
    ),
    (
        "sec-fetch-dest",
        "empty",
    ),
    (
        "sec-fetch-mode",
        "cors",
    ),
    (
        "sec-fetch-site",
        "same-origin",
    ),
    (
        "user-agent",
        USER_AGENT,
    ),
]


# =============================================================================
# Log
# =============================================================================

def log(message):
    print(message, flush=True)


def github_error(title, message):
    message = str(message).replace("\n", " ")

    log(
        f"::error title={title}::{message}"
    )


def github_warning(title, message):
    message = str(message).replace("\n", " ")

    log(
        f"::warning title={title}::{message}"
    )


# =============================================================================
# Account
# =============================================================================

def get_accounts():
    raw = os.environ.get(
        "GLADOS_COOKIE",
        "",
    ).strip()

    return [
        cookie.strip()
        for cookie in raw.split("&")
        if cookie.strip()
    ]


# =============================================================================
# HTTP Client
# =============================================================================

def create_client():
    client = httpx.Client(
        http2=True,

        timeout=httpx.Timeout(
            TIMEOUT,
            connect=TIMEOUT,
        ),

        follow_redirects=False,

        # 避免运行环境里的 HTTP_PROXY /
        # HTTPS_PROXY 等变量干扰这次实验。
        trust_env=False,
    )

    # httpx 默认会自动加入：
    #
    # User-Agent: python-httpx
    # Accept: */*
    # Accept-Encoding: ...
    # Connection: keep-alive
    #
    # 全部清空，我们自己严格设置。
    client.headers.clear()

    return client


# =============================================================================
# Retry
# =============================================================================

def request(
    client,
    method,
    url,
    headers,
    content=None,
):
    last_error = None

    for attempt in range(
        MAX_RETRIES + 1
    ):
        try:
            response = client.request(
                method,
                url,
                headers=headers,
                content=content,
            )

        except httpx.HTTPError as e:
            last_error = e

            if attempt >= MAX_RETRIES:
                raise

            wait = 2 ** attempt

            github_warning(
                "网络请求异常",
                (
                    f"{type(e).__name__}: {e}; "
                    f"{wait}s 后重试"
                ),
            )

            time.sleep(wait)

            continue

        # -------------------------------------------------------------
        # 429
        # -------------------------------------------------------------

        if response.status_code == 429:

            if attempt >= MAX_RETRIES:
                return response

            wait = 5 * (attempt + 1)

            retry_after = response.headers.get(
                "retry-after"
            )

            if retry_after:
                try:
                    wait = int(retry_after)
                except ValueError:
                    pass

            github_warning(
                "GLaDOS限流",
                f"HTTP 429; {wait}s 后重试",
            )

            time.sleep(wait)

            continue

        # -------------------------------------------------------------
        # 5xx
        # -------------------------------------------------------------

        if 500 <= response.status_code < 600:

            if attempt >= MAX_RETRIES:
                return response

            wait = 2 ** (attempt + 1)

            github_warning(
                "GLaDOS服务异常",
                (
                    f"HTTP {response.status_code}; "
                    f"{wait}s 后重试"
                ),
            )

            time.sleep(wait)

            continue

        return response

    raise last_error or RuntimeError(
        "HTTP request failed"
    )


# =============================================================================
# JSON
# =============================================================================

def parse_json(response):
    try:
        return response.json()

    except ValueError:
        raise RuntimeError(
            (
                "接口返回非 JSON: "
                f"HTTP {response.status_code}, "
                f"{response.text[:500]}"
            )
        )


# =============================================================================
# Debug request
# =============================================================================

def print_request(response):
    """
    输出 httpx 最终实际构造的 HTTP Header。

    Cookie 脱敏。
    """

    request = response.request

    log(
        f"实际请求: "
        f"{request.method} {request.url}"
    )

    for name, value in request.headers.multi_items():

        if name.lower() == "cookie":
            value = "***"

        log(
            f"  {name}: {value}"
        )

    if request.content:
        try:
            body = request.content.decode()
        except Exception:
            body = "<binary>"

        log(
            f"  body: {body}"
        )


# =============================================================================
# Status
# =============================================================================

def get_status(
    client,
    cookie,
):
    headers = [
        *COMMON_HEADERS,

        # HAR 为安全原因没有导出 Cookie，
        # 实际认证请求必须携带。
        (
            "cookie",
            cookie,
        ),
    ]

    response = request(
        client,
        "GET",
        STATUS_URL,
        headers,
    )

    log(
        (
            f"状态接口: "
            f"{response.http_version} "
            f"HTTP {response.status_code}"
        )
    )

    if not response.is_success:
        raise RuntimeError(
            (
                f"状态接口 HTTP "
                f"{response.status_code}: "
                f"{response.text[:300]}"
            )
        )

    result = parse_json(response)

    data = result.get("data")

    if not isinstance(data, dict):

        message = (
            result.get("message")
            or result.get("reason")
            or "status 未返回 data"
        )

        raise RuntimeError(message)

    email = (
        data.get("email")
        or "未知账号"
    )

    left_days = data.get(
        "leftDays",
        "未知",
    )

    if left_days != "未知":
        left_days = str(
            left_days
        ).split(".")[0]

    return (
        email,
        left_days,
    )


# =============================================================================
# Checkin classification
# =============================================================================

def classify_checkin(data):
    code = data.get("code")

    reason = str(
        data.get("reason")
        or ""
    ).strip()

    message = str(
        data.get("message")
        or data.get("msg")
        or ""
    ).strip()

    lower = message.lower()

    # -------------------------------------------------------------
    # 风控
    # -------------------------------------------------------------

    if (
        reason == "device-mismatch"
        or "automated check-in" in lower
        or "sign in again" in lower
    ):
        return (
            "risk",
            message,
        )

    # -------------------------------------------------------------
    # 重复签到
    # -------------------------------------------------------------

    if (
        "checkin repeats" in lower
        or "check-in repeats" in lower
        or "try tomorrow" in lower
    ):
        return (
            "repeat",
            message,
        )

    # -------------------------------------------------------------
    # 正常成功
    # -------------------------------------------------------------

    if (
        code == 0
        or "checkin! got" in lower
        or "check-in! got" in lower
    ):
        return (
            "success",
            message or "签到成功",
        )

    return (
        "error",
        (
            message
            or json.dumps(
                data,
                ensure_ascii=False,
            )
        ),
    )


# =============================================================================
# Checkin
# =============================================================================

def do_checkin(
    client,
    cookie,
    email,
):
    headers = [
        *CHECKIN_HEADERS,

        # HAR 本身没有导出 Cookie，
        # 但接口显然依赖当前登录 Session。
        (
            "cookie",
            cookie,
        ),
    ]

    response = request(
        client,
        "POST",
        CHECKIN_URL,
        headers,
        content=CHECKIN_BODY,
    )

    # -------------------------------------------------------------
    # 第一次测试时很重要：
    # 打印最终由 httpx 构造出来的 Header。
    # -------------------------------------------------------------

    print_request(response)

    log(
        (
            f"{email}: 签到协议 = "
            f"{response.http_version}"
        )
    )

    data = parse_json(response)

    log(
        (
            f"{email}: 签到响应 "
            f"HTTP {response.status_code} = "
            f"{json.dumps(data, ensure_ascii=False)}"
        )
    )

    return (
        response,
        data,
    )


# =============================================================================
# PushPlus
# =============================================================================

def pushplus(
    token,
    title,
    content,
):
    if not token:
        return

    try:
        with httpx.Client(
            timeout=10,
        ) as client:

            response = client.get(
                "https://www.pushplus.plus/send",
                params={
                    "token": token,
                    "title": title,
                    "content": content,
                },
            )

            if not response.is_success:

                github_warning(
                    "PushPlus推送失败",
                    (
                        f"HTTP "
                        f"{response.status_code}"
                    ),
                )

    except Exception as e:

        github_warning(
            "PushPlus推送失败",
            e,
        )


# =============================================================================
# Process account
# =============================================================================

def process_account(
    cookie,
    index,
):
    email = f"账号{index}"
    left_days = "未知"

    try:
        with create_client() as client:

            # ---------------------------------------------------------
            # Status
            # ---------------------------------------------------------

            email, left_days = get_status(
                client,
                cookie,
            )

            log(
                (
                    f"{email}: 登录状态正常，"
                    f"剩余 {left_days} 天"
                )
            )

            # ---------------------------------------------------------
            # Checkin
            # ---------------------------------------------------------

            response, data = do_checkin(
                client,
                cookie,
                email,
            )

            if not response.is_success:

                raise RuntimeError(
                    (
                        f"签到接口 HTTP "
                        f"{response.status_code}"
                    )
                )

            result_type, message = (
                classify_checkin(data)
            )

            # ---------------------------------------------------------
            # 风控诊断
            # ---------------------------------------------------------

            if result_type == "risk":

                reason = data.get(
                    "reason",
                    "未知",
                )

                login_device = data.get(
                    "loginDevice",
                    "未知",
                )

                current_device = data.get(
                    "currentDevice",
                    "未知",
                )

                github_error(
                    "GLaDOS签到被拒绝",
                    (
                        f"{email}: "
                        f"reason={reason}, "
                        f"loginDevice="
                        f"{login_device}, "
                        f"currentDevice="
                        f"{current_device}, "
                        f"message={message}"
                    ),
                )

                return (
                    True,
                    True,
                    (
                        f"{email}"
                        f"----{message}"
                        f"----reason={reason}"
                        f"----登录设备={login_device}"
                        f"----请求设备={current_device}"
                        f"----剩余({left_days})天"
                    ),
                )

            # ---------------------------------------------------------
            # 其他错误
            # ---------------------------------------------------------

            if result_type == "error":

                github_error(
                    "GLaDOS签到异常",
                    (
                        f"{email}: "
                        f"{message}"
                    ),
                )

                return (
                    True,
                    False,
                    (
                        f"{email}"
                        f"----签到异常"
                        f"----{message}"
                    ),
                )

            # ---------------------------------------------------------
            # Success / repeat
            # ---------------------------------------------------------

            if result_type == "repeat":

                log(
                    f"{email}: 今日已经签到"
                )

            else:

                log(
                    f"{email}: 签到成功"
                )

            # ---------------------------------------------------------
            # Refresh status
            # ---------------------------------------------------------

            try:
                _, left_days = get_status(
                    client,
                    cookie,
                )

            except Exception as e:

                github_warning(
                    "签到后状态刷新失败",
                    (
                        f"{email}: "
                        f"{type(e).__name__}: "
                        f"{e}"
                    ),
                )

            return (
                False,
                False,
                (
                    f"{email}"
                    f"----{message}"
                    f"----剩余({left_days})天"
                ),
            )

    except Exception as e:

        github_error(
            "GLaDOS执行异常",
            (
                f"{email}: "
                f"{type(e).__name__}: "
                f"{e}"
            ),
        )

        return (
            True,
            False,
            (
                f"{email}"
                f"----执行异常"
                f"----{type(e).__name__}: {e}"
            ),
        )


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

    push_token = os.environ.get(
        "PUSHPLUS_TOKEN",
        "",
    )

    log(
        f"接口地址: {BASE_URL}"
    )

    log(
        f"User-Agent: {USER_AGENT}"
    )

    log(
        "HTTP client: httpx + HTTP/2"
    )

    log(
        f"共发现 {len(accounts)} 个账号"
    )

    results = []

    has_error = False
    risk_triggered = False

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

        if risk_triggered:

            message = (
                f"账号{index}"
                "----跳过"
                "----前序账号已触发风控"
            )

            log(message)

            results.append(message)

            continue

        error, risk, result = (
            process_account(
                cookie,
                index,
            )
        )

        results.append(result)

        has_error |= error
        risk_triggered |= risk

    # -------------------------------------------------------------------------
    # Push
    # -------------------------------------------------------------------------

    if risk_triggered:
        title = "GLaDOS 签到被拒绝"

    elif has_error:
        title = "GLaDOS 签到异常"

    else:
        title = "GLaDOS 签到成功"

    pushplus(
        push_token,
        title,
        "\n".join(results),
    )

    # -------------------------------------------------------------------------
    # Exit
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

    log(
        "所有账号执行正常"
    )

    log("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
