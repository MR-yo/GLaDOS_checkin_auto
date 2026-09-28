import requests
import json
import os


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


def pushplus(token, title, content):
    if not token:
        return

    try:
        requests.get(
            "https://www.pushplus.plus/send",
            params={
                "token": token,
                "title": title,
                "content": content,
            },
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"PushPlus 推送失败: {e}")


def parse_json(response, name):
    """
    安全解析接口 JSON。
    返回失败时输出实际响应，方便排查。
    """
    try:
        return response.json()
    except ValueError:
        print(
            f"{name} 返回非 JSON 数据: "
            f"HTTP {response.status_code}, "
            f"response={response.text[:500]}"
        )
        return None


if __name__ == "__main__":
    sckey = os.environ.get("PUSHPLUS_TOKEN", "")

    cookies = [
        cookie.strip()
        for cookie in os.environ.get("GLADOS_COOKIE", "").split("&")
        if cookie.strip()
    ]

    if not cookies:
        print("未获取到 COOKIE 变量")
        exit(0)

    send_content = []

    for index, cookie in enumerate(cookies, start=1):
        print(f"开始处理第 {index} 个账号")

        headers = {
            **HEADERS,
            "cookie": cookie,
        }

        try:
            # 签到
            checkin_response = requests.post(
                CHECKIN_URL,
                headers={
                    **headers,
                    "content-type": "application/json;charset=UTF-8",
                },
                json=PAYLOAD,
                timeout=15,
            )

            # 查询账户状态
            state_response = requests.get(
                STATUS_URL,
                headers=headers,
                timeout=15,
            )

        except requests.RequestException as e:
            message = f"第 {index} 个账号请求失败: {e}"
            print(message)
            send_content.append(message)
            continue

        checkin_data = parse_json(checkin_response, "签到接口")
        state_data = parse_json(state_response, "状态接口")

        # 状态接口本身无法解析
        if state_data is None:
            message = f"第 {index} 个账号状态接口返回异常"
            print(message)
            send_content.append(message)
            continue

        # 关键修改：
        # 不再直接 state_data['data']，先检查 data 是否存在
        data = state_data.get("data")

        if not isinstance(data, dict):
            error_message = (
                state_data.get("message")
                or state_data.get("msg")
                or "未知错误"
            )

            print(
                f"第 {index} 个账号状态异常: {error_message}, "
                f"response={state_data}"
            )

            send_content.append(
                f"账号{index}----状态获取失败----{error_message}"
            )

            continue

        # 即使 data 存在，也不要假设字段一定存在
        email = data.get("email", f"账号{index}")

        left_days = data.get("leftDays")

        if left_days is None:
            left_days_text = "未知"
        else:
            try:
                left_days_text = str(left_days).split(".")[0]
            except Exception:
                left_days_text = str(left_days)

        # 处理签到结果
        if checkin_data is None:
            message = "签到接口返回异常"

        elif "message" in checkin_data:
            message = checkin_data.get("message", "签到完成")

        else:
            message = (
                checkin_data.get("msg")
                or f"签到返回异常: {checkin_data}"
            )

        log = (
            f"{email}----结果--{message}"
            f"----剩余({left_days_text})天"
        )

        print(log)

        send_content.append(
            f"{email}----{message}----剩余({left_days_text})天"
        )

    # 所有账号执行完成后统一推送
    if send_content:
        pushplus(
            sckey,
            "GLaDOS 签到结果",
            "\n".join(send_content),
        )
