"""Srun portal authentication client used by the plugin and autostart helper."""
from __future__ import annotations

import json
import random
import re
import socket
import time
from urllib.parse import urlparse

import requests

from network import select_portal
from srun_codec import password_md5, sha1_hex, srun_base64, xencode

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
)


class SrunError(RuntimeError):
    pass


def normalize_portal(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    return parsed.netloc or parsed.path.split("/", 1)[0]


def _callback() -> str:
    suffix = "".join(str(random.randint(0, 9)) for _ in range(16))
    return f"jQuery11240{suffix}_{int(time.time() * 1000)}"


def parse_response(text: str) -> dict:
    raw = str(text or "").strip()
    if not raw:
        raise SrunError("认证服务器返回空响应")
    match = re.search(r"^\s*[^(]*\((.*)\)\s*;?\s*$", raw, re.S)
    if match:
        raw = match.group(1).strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SrunError(f"认证服务器返回非 JSON 数据: {raw[:160]}") from exc
    if not isinstance(value, dict):
        raise SrunError("认证服务器响应格式错误")
    return value


def local_ip(portal: str, configured_ip: object = "") -> str:
    explicit = str(configured_ip or "").strip()
    if explicit:
        return explicit
    host = portal.split(":", 1)[0]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, 80))
        return sock.getsockname()[0]
    except OSError as exc:
        raise SrunError(f"无法确定本机 IP: {exc}") from exc
    finally:
        sock.close()


def _base_url(portal: str) -> str:
    return f"http://{portal}"


def online_status(session: requests.Session, portal: str,
                  timeout: float) -> tuple[bool, str]:
    callback = _callback()
    try:
        response = session.get(
            _base_url(portal) + "/cgi-bin/rad_user_info",
            params={"callback": callback, "_": int(time.time() * 1000)},
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
        response.raise_for_status()
        body = parse_response(response.text)
    except (requests.RequestException, SrunError) as exc:
        return False, str(exc)
    error = str(body.get("error") or "")
    if error.lower() in {"", "ok"}:
        return True, "online"
    if error.lower() == "not_online":
        return False, error
    return False, error


def authenticate(config: dict, log=None, force_login: bool = False) -> dict:
    username = str(config.get("username") or "").strip()
    password = str(config.get("password") or "")
    if not username or not password:
        raise SrunError("未配置校园网账号或密码")
    portal, connection = select_portal(config)
    portal = normalize_portal(portal)
    if not portal:
        raise SrunError("未配置有线或无线认证地址")
    timeout = max(2.0, float(config.get("request_timeout") or 10))
    session = requests.Session()
    try:
        detail = ""
        if not force_login:
            online, detail = online_status(session, portal, timeout)
            if online:
                return {
                    "ok": True,
                    "already_online": True,
                    "portal": portal,
                    "connection": connection,
                    "message": "校园网已在线",
                }

        ip = local_ip(portal, config.get("ip"))
        callback = _callback()
        ac_id = str(config.get("ac_id") or "1").strip()
        login_type = str(config.get("login_type") or "1").strip()
        headers = {"User-Agent": USER_AGENT}
        challenge_response = session.get(
            _base_url(portal) + "/cgi-bin/get_challenge",
            params={
                "callback": callback,
                "username": username,
                "ip": ip,
                "_": int(time.time() * 1000),
            },
            headers=headers,
            timeout=timeout,
        )
        challenge_response.raise_for_status()
        challenge_body = parse_response(challenge_response.text)
        token = str(challenge_body.get("challenge") or "")
        if not token:
            raise SrunError(
                f"认证服务器未返回 challenge: {detail or '未知错误'}")

        info_value = json.dumps({
            "username": username,
            "password": password,
            "ip": ip,
            "acid": ac_id,
            "enc_ver": "srun_bx1",
        }, ensure_ascii=False, separators=(",", ":"))
        encoded_info = "{SRBX1}" + srun_base64(xencode(info_value, token))
        password_hash = password_md5(password, token)
        checksum_source = "".join((
            token, username,
            token, password_hash,
            token, ac_id,
            token, ip,
            token, "200",
            token, login_type,
            token, encoded_info,
        ))
        checksum = sha1_hex(checksum_source)
        login_response = session.get(
            _base_url(portal) + "/cgi-bin/srun_portal",
            params={
                "callback": callback,
                "action": "login",
                "username": username,
                "password": "{MD5}" + password_hash,
                "ac_id": ac_id,
                "ip": ip,
                "chksum": checksum,
                "info": encoded_info,
                "n": "200",
                "type": login_type,
                "os": "windows+10",
                "name": "windows",
                "double_stack": "0",
                "_": int(time.time() * 1000),
            },
            headers=headers,
            timeout=timeout,
        )
        login_response.raise_for_status()
        login_body = parse_response(login_response.text)
        error = str(login_body.get("error") or "")
        if error.lower() not in {"", "ok"}:
            message = (
                login_body.get("suc_msg") or login_body.get("res")
                or login_body.get("error_msg") or error
            )
            raise SrunError(f"校园网认证失败: {message}")

        online, verify_detail = online_status(session, portal, timeout)
        if not online:
            raise SrunError(
                f"认证请求已发送，但在线状态校验失败: {verify_detail}")
        return {
            "ok": True,
            "already_online": False,
            "portal": portal,
            "connection": connection,
            "ip": ip,
            "message": "校园网认证成功",
        }
    except requests.RequestException as exc:
        raise SrunError(f"连接认证服务器失败: {exc}") from exc
    finally:
        session.close()


def connect_with_retries(config: dict, log=None, status=None) -> dict:
    username = str(config.get("username") or "").strip()
    password = str(config.get("password") or "")
    if not username or not password:
        raise SrunError("未配置校园网账号或密码")
    if not (str(config.get("wired_portal") or "").strip()
            or str(config.get("wireless_portal") or "").strip()):
        raise SrunError("未配置有线或无线认证地址")
    attempts = max(1, int(config.get("max_attempts") or 1))
    interval = max(1.0, float(config.get("retry_interval") or 10))
    last_error = ""
    for attempt in range(1, attempts + 1):
        if status:
            status(f"校园网认证尝试 {attempt}/{attempts}")
        try:
            result = authenticate(config)
            if log:
                log(
                    f"{result['message']}（{result.get('portal', '')}，"
                    f"{result.get('connection', '')}）")
            return result
        except SrunError as exc:
            last_error = str(exc)
            if log:
                log(f"第 {attempt} 次认证失败: {last_error}")
            if attempt < attempts:
                time.sleep(interval)
    raise SrunError(
        f"校园网认证在 {attempts} 次尝试后仍未成功: {last_error}")
