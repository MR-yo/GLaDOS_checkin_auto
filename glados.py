import json
import os
import sys
import time

import requests


BASE_URL = "https://glados.rocks"
CHECKIN_URL = f"{BASE_URL}/api/user/checkin"
STATUS_URL = f"{BASE_URL}/api/user/status"

TIMEOUT = 15
MAX_RETRIES = 2

USER_AGENT = (
    "M"
)

BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": f"{BASE_URL}/console/checkin",
    "Origin": BASE_URL,
    "User-Agent": USER_AGENT,
}

PAYLOAD = {
    "token": "glados.rocks"
}

RISK_KEYWORDS = (
    "automated check-in",
    "automated checkin",
    "sign in again",
    "login again",
    "log in again",
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


def log(message):
    print(message, flush=True)


def github_error(title, message):
    message = str(message).replace("\n", " ")
    log(f"::error title={title}::{message}")


def github_warning(title, message):
    message = str(message).replace("\n", " ")
    log(f"::warning title={title}::{message}")


def get_accounts():
    raw = os.environ.get("GLADOS_COOKIE", "").strip()

    return [
        cookie.strip()
        for cookie in raw.split("&")
        if cookie.strip()
    ]


def request(session, method, url, **kwargs):
    """
    网络异常、429、5xx 做有限重试。
    业务错误不自动重试。
    """

    last_error = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = session.request(
                method,
                url,
                timeout=TIMEOUT,
                **kwargs,
            )

        except requests.RequestException as e:
            last_error = e

            if attempt >= MAX_RETRIES:
                raise

            wait = 2 ** attempt

            github_warning(
                "网络请求失败",
                f"{e}，{wait}s 后重试",
            )

            time.sleep(wait)
            continue

        if response.status_code == 429:
            if attempt >= MAX_RETRIES:
                return response

            retry_after = response.headers.get("Retry-After")

            try:
                wait = int(retry_after)
            except (TypeError, ValueError):
                wait = 5 * (attempt + 1)

            github_warning(
                "接口限流",
                f"HTTP 429，{wait}s 后重试",
            )

            time.sleep(wait)
            continue

        if 500 <= response.status_code < 600:
            if attempt >= MAX_RETRIES:
                return response

            wait = 2 ** (attempt + 1)

            github_warning(
                "服务端异常",
                f"HTTP {response.status_code}，{wait}s 后重试",
            )

            time.sleep(wait)
            continue

        return response

    raise last_error or RuntimeError("请求失败")


def parse_json(response, name):
    try:
        return response.json()

    except ValueError:
        github_error(
            f"{name}返回异常",
            (
                f"HTTP {response.status_code}, "
                f"非 JSON: {response.text[:500]}"
            ),
        )

        return None


def get_status(session):
    response = request(
        session,
        "GET",
        STATUS_URL,
    )

    if not response.ok:
        raise RuntimeError(
            f"状态接口 HTTP {response.status_code}"
        )

    data = parse_json(
        response,
        "状态接口",
    )

    if not isinstance(data, dict):
        raise RuntimeError("状态接口数据格式异常")

    account = data.get("data")

    if not isinstance(account, dict):
        message = (
            data.get("message")
            or data.get("msg")
            or "状态接口未返回 data"
        )

        raise RuntimeError(message)

    email = (
        account.get("email")
        or "未知账号"
    )

    left_days = account.get("leftDays")

    if left_days is None:
        left_days = "未知"
    else:
        left_days = str(left_days).split(".")[0]

    return email, left_days


def classify_checkin(data):
    if not isinstance(data, dict):
        return "error", "签到接口数据格式异常"

    message = str(
        data.get("message")
        or data.get("msg")
        or ""
    ).strip()

    lower = message.lower()

    if any(keyword in lower for keyword in RISK_KEYWORDS):
        return "risk", message

    if any(keyword in lower for keyword in REPEAT_KEYWORDS):
        return "repeat", message

    if any(keyword in lower for keyword in SUCCESS_KEYWORDS):
        return "success", message

    if data.get("code") == 0:
        return "success", message or "签到成功"

    return (
        "error",
        message
        or f"未知签到结果: {json.dumps(data, ensure_ascii=False)}",
    )


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
            e,
        )


def process_account(cookie, index):
    session = requests.Session()

    session.headers.update({
        **BASE_HEADERS,
        "Cookie": cookie,
    })

    email = f"账号{index}"
    left_days = "未知"

    try:
        # -------------------------------------------------------------
        # 先确认登录状态
        # -------------------------------------------------------------

        email, left_days = get_status(session)

        log(
            f"{email}: 登录状态正常，"
            f"剩余 {left_days} 天"
        )

        # -------------------------------------------------------------
        # 签到
        # -------------------------------------------------------------

        response = request(
            session,
            "POST",
            CHECKIN_URL,
            headers={
                "Content-Type":
                    "application/json;charset=UTF-8",
            },
            json=PAYLOAD,
        )

        checkin_data = parse_json(
            response,
            "签到接口",
        )

        # 完整输出服务端响应
        if checkin_data is not None:
            log(
                f"{email}: 签到响应 "
                f"HTTP {response.status_code} = "
                f"{json.dumps(checkin_data, ensure_ascii=False)}"
            )
        else:
            return (
                True,
                False,
                f"{email}----签到接口返回异常",
            )

        if not response.ok:
            return (
                True,
                False,
                (
                    f"{email}"
                    f"----签到接口 HTTP "
                    f"{response.status_code}"
                ),
            )

        result_type, message = classify_checkin(
            checkin_data
        )

        # -------------------------------------------------------------
        # 风控
        # -------------------------------------------------------------

        if result_type == "risk":
            github_error(
                "GLaDOS触发自动化检测",
                f"{email}: {message}",
            )

            return (
                True,
                True,
                (
                    f"{email}"
                    f"----签到失败"
                    f"----{message}"
                    f"----剩余({left_days})天"
                ),
            )

        # -------------------------------------------------------------
        # 未知异常
        # -------------------------------------------------------------

        if result_type == "error":
            github_error(
                "GLaDOS签到异常",
                f"{email}: {message}",
            )

            return (
                True,
                False,
                (
                    f"{email}"
                    f"----签到异常"
                    f"----{message}"
                    f"----剩余({left_days})天"
                ),
            )

        # -------------------------------------------------------------
        # 成功 / 重复签到
        # -------------------------------------------------------------

        if result_type == "repeat":
            log(f"{email}: 今日已经签到")
        else:
            log(f"{email}: 签到成功")

        # 再查一次最新剩余天数
        try:
            _, left_days = get_status(session)
        except Exception as e:
            github_warning(
                "签到后状态刷新失败",
                f"{email}: {e}",
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
            "GLaDOS签到异常",
            f"{email}: {type(e).__name__}: {e}",
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

    finally:
        session.close()


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

    log(f"接口地址: {BASE_URL}")
    log(f"共发现 {len(accounts)} 个账号")

    results = []

    has_error = False
    risk_triggered = False

    for index, cookie in enumerate(
        accounts,
        start=1,
    ):
        log("")
        log("=" * 60)
        log(f"开始处理第 {index} 个账号")
        log("=" * 60)

        if risk_triggered:
            message = (
                f"账号{index}"
                "----本次跳过"
                "----前序账号已触发自动化检测"
            )

            log(message)

            results.append(message)

            continue

        error, risk, message = (
            process_account(
                cookie,
                index,
            )
        )

        results.append(message)

        has_error |= error
        risk_triggered |= risk

    # -------------------------------------------------------------
    # PushPlus
    # -------------------------------------------------------------

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

    # -------------------------------------------------------------
    # GitHub Actions Exit Code
    # -------------------------------------------------------------

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
