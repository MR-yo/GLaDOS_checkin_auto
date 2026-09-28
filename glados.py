import json
import os
import sys

import requests


# -------------------------------------------------------------------------------------------
# 配置
# -------------------------------------------------------------------------------------------

CHECKIN_URL = "https://glados.cloud/api/user/checkin"
STATUS_URL = "https://glados.cloud/api/user/status"

REFERER = "https://glados.cloud/console/checkin"
ORIGIN = "https://glados.cloud"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/102.0.0.0 Safari/537.36"
)

HEADERS = {
    "referer": REFERER,
    "origin": ORIGIN,
    "user-agent": USER_AGENT,
}

PAYLOAD = {
    "token": "glados.cloud"
}


# -------------------------------------------------------------------------------------------
# 工具方法
# -------------------------------------------------------------------------------------------

def pushplus(token, title, content):
    """
    PushPlus 推送。
    推送失败只记录日志，不影响 GLaDOS 签到任务最终状态。
    """
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
            print(
                f"::warning title=PushPlus推送失败::"
                f"HTTP {response.status_code}"
            )

    except requests.RequestException as e:
        print(
            f"::warning title=PushPlus推送失败::{e}"
        )


def parse_json(response, api_name):
    """
    安全解析 JSON。

    返回：
        dict/list: 解析成功
        None: 解析失败
    """
    try:
        return response.json()

    except ValueError:
        text = response.text[:500].replace("\n", " ")

        print(
            f"::error title={api_name}返回异常::"
            f"HTTP {response.status_code}, 非 JSON 数据: {text}"
        )

        return None


def format_left_days(value):
    """
    leftDays 可能是：
        123.456
        "123.456"
        123

    统一转换成整数天显示。
    """
    if value is None:
        return None

    try:
        return str(value).split(".")[0]

    except Exception:
        return str(value)


# -------------------------------------------------------------------------------------------
# Main
# -------------------------------------------------------------------------------------------

if __name__ == "__main__":
    pushplus_token = os.environ.get("PUSHPLUS_TOKEN", "")

    cookies = [
        cookie.strip()
        for cookie in os.environ.get("GLADOS_COOKIE", "").split("&")
        if cookie.strip()
    ]

    # 没有 Cookie 本身就是任务配置异常
    if not cookies:
        print("::error title=配置错误::未获取到 GLADOS_COOKIE")
        sys.exit(1)

    # 最终是否让 GitHub Actions 判定失败
    has_error = False

    # 汇总推送内容
    send_content = []

    for index, cookie in enumerate(cookies, start=1):
        print()
        print("=" * 60)
        print(f"开始处理第 {index} 个账号")
        print("=" * 60)

        headers = {
            **HEADERS,
            "cookie": cookie,
        }

        # -------------------------------------------------------------------------------
        # 1. 调用签到接口
        # -------------------------------------------------------------------------------

        try:
            checkin_response = requests.post(
                CHECKIN_URL,
                headers={
                    **headers,
                    "content-type": "application/json;charset=UTF-8",
                },
                json=PAYLOAD,
                timeout=15,
            )

        except requests.RequestException as e:
            has_error = True

            message = f"账号{index}----签到请求失败----{e}"

            print(
                f"::error title=GLaDOS签到请求失败::"
                f"账号 {index}: {e}"
            )

            send_content.append(message)
            continue

        # HTTP 4xx / 5xx
        if not checkin_response.ok:
            has_error = True

            message = (
                f"账号{index}----签到接口异常"
                f"----HTTP {checkin_response.status_code}"
            )

            print(
                f"::error title=GLaDOS签到接口异常::"
                f"账号 {index}: HTTP {checkin_response.status_code}"
            )

            send_content.append(message)
            continue

        # -------------------------------------------------------------------------------
        # 2. 查询账户状态
        # -------------------------------------------------------------------------------

        try:
            state_response = requests.get(
                STATUS_URL,
                headers=headers,
                timeout=15,
            )

        except requests.RequestException as e:
            has_error = True

            message = f"账号{index}----状态请求失败----{e}"

            print(
                f"::error title=GLaDOS状态请求失败::"
                f"账号 {index}: {e}"
            )

            send_content.append(message)
            continue

        if not state_response.ok:
            has_error = True

            message = (
                f"账号{index}----状态接口异常"
                f"----HTTP {state_response.status_code}"
            )

            print(
                f"::error title=GLaDOS状态接口异常::"
                f"账号 {index}: HTTP {state_response.status_code}"
            )

            send_content.append(message)
            continue

        # -------------------------------------------------------------------------------
        # 3. JSON 解析
        # -------------------------------------------------------------------------------

        checkin_data = parse_json(
            checkin_response,
            "GLaDOS签到接口",
        )

        state_data = parse_json(
            state_response,
            "GLaDOS状态接口",
        )

        if checkin_data is None:
            has_error = True

            send_content.append(
                f"账号{index}----签到接口返回非JSON数据"
            )

            continue

        if state_data is None:
            has_error = True

            send_content.append(
                f"账号{index}----状态接口返回非JSON数据"
            )

            continue

        # -------------------------------------------------------------------------------
        # 4. 校验状态接口数据
        # -------------------------------------------------------------------------------

        if not isinstance(state_data, dict):
            has_error = True

            print(
                f"::error title=GLaDOS状态数据异常::"
                f"账号 {index}: 返回结果不是对象"
            )

            send_content.append(
                f"账号{index}----状态数据格式异常"
            )

            continue

        data = state_data.get("data")

        # 这里就是原来 KeyError: 'data' 的位置
        if not isinstance(data, dict):
            has_error = True

            error_message = (
                state_data.get("message")
                or state_data.get("msg")
                or "接口未返回 data"
            )

            print(
                f"::error title=GLaDOS账号状态异常::"
                f"账号 {index}: {error_message}"
            )

            # 输出接口响应方便以后排查，但不会泄露 Cookie
            try:
                response_text = json.dumps(
                    state_data,
                    ensure_ascii=False,
                )
            except Exception:
                response_text = str(state_data)

            print(
                f"状态接口返回: {response_text[:500]}"
            )

            send_content.append(
                f"账号{index}----状态获取失败----{error_message}"
            )

            continue

        # -------------------------------------------------------------------------------
        # 5. 获取账号信息
        # -------------------------------------------------------------------------------

        email = data.get("email") or f"账号{index}"

        left_days = format_left_days(
            data.get("leftDays")
        )

        if left_days is None:
            has_error = True

            print(
                f"::error title=GLaDOS状态数据异常::"
                f"{email}: 缺少 leftDays"
            )

            send_content.append(
                f"{email}----无法获取剩余天数"
            )

            continue

        # -------------------------------------------------------------------------------
        # 6. 分析签到结果
        # -------------------------------------------------------------------------------

        if not isinstance(checkin_data, dict):
            has_error = True

            message = "签到接口数据格式异常"

            print(
                f"::error title=GLaDOS签到数据异常::"
                f"{email}: 返回结果不是对象"
            )

        elif "message" in checkin_data:
            # GLaDOS 正常签到以及重复签到通常都会返回 message
            message = str(
                checkin_data.get("message") or "签到完成"
            )

        else:
            # 没有正常的 message，认为接口格式发生变化或签到异常
            has_error = True

            message = (
                checkin_data.get("msg")
                or "签到接口返回未知结果"
            )

            print(
                f"::error title=GLaDOS签到结果异常::"
                f"{email}: {message}"
            )

            try:
                response_text = json.dumps(
                    checkin_data,
                    ensure_ascii=False,
                )
            except Exception:
                response_text = str(checkin_data)

            print(
                f"签到接口返回: {response_text[:500]}"
            )

        # -------------------------------------------------------------------------------
        # 7. 输出账号结果
        # -------------------------------------------------------------------------------

        result = (
            f"{email}"
            f"----结果--{message}"
            f"----剩余({left_days})天"
        )

        print(result)

        send_content.append(
            f"{email}"
            f"----{message}"
            f"----剩余({left_days})天"
        )

    # -----------------------------------------------------------------------------------
    # PushPlus 汇总通知
    # -----------------------------------------------------------------------------------

    if send_content:
        title = (
            "GLaDOS 签到异常"
            if has_error
            else "GLaDOS 签到成功"
        )

        pushplus(
            pushplus_token,
            title,
            "\n".join(send_content),
        )

    # -----------------------------------------------------------------------------------
    # 最终返回 GitHub Actions 状态
    # -----------------------------------------------------------------------------------

    print()
    print("=" * 60)

    if has_error:
        print("本次签到存在异常，GitHub Actions 将标记为失败")
        print("=" * 60)

        # GitHub Actions -> Failure
        # 如果开启了 GitHub Actions 邮件通知，会收到失败邮件
        sys.exit(1)

    print("所有账号执行正常")
    print("=" * 60)

    # GitHub Actions -> Success
    sys.exit(0)
