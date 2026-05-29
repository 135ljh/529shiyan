#!/usr/bin/env python
# -*- coding: utf-8 -*-
import re
import threading
import time

import pymysql
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException


PLC_IP = "10.21.2.232"
PLC_PORT = 502
PLC_TIMEOUT = 3
DEFAULT_DEVICE_ID = 1
OPCUA_READ_BASE = 2000
LOOP_INTERVAL_SECONDS = 5
HISTORY_RETENTION_HOURS = 24
MYSQL_HOST = "127.0.0.1"
MYSQL_PORT = 3306
MYSQL_USER = "root"
MYSQL_PASSWORD = "123456"
MYSQL_DATABASE = "plc_panel"

POINTS = [
    {"code": "voltage", "name": "电压", "address": "D2000", "scale": 0.1, "unit": "V"},
    {"code": "current", "name": "电流", "address": "D2001", "scale": 0.01, "unit": "A"},
    {"code": "power", "name": "瞬时有功功率", "address": "D2002", "scale": 1, "unit": "W"},
    {"code": "frequency", "name": "频率", "address": "D2004", "scale": 0.01, "unit": "Hz"},
    {"code": "electricity", "name": "总有功电能", "address": "D2005", "scale": 0.01, "unit": "kW.h"},
    {"code": "water", "name": "总用水量", "address": "D2010", "scale": 0.01, "unit": "m3"},
    {"code": "humidity", "name": "湿度", "address": "D2020", "scale": 0.1, "unit": "%RH"},
    {"code": "temperature", "name": "温度", "address": "D2021", "scale": 0.1, "unit": "C"},
    {"code": "noise", "name": "噪音", "address": "D2030", "scale": 1, "unit": "dB"},
    {"code": "smoke", "name": "烟感状态", "address": "D2040", "scale": 1, "unit": "ppm"},
    {"code": "rope", "name": "拉绳长度", "address": "D2060", "scale": 0.1, "unit": "mm"},
]

POINT_BY_ADDRESS = {point["address"]: point for point in POINTS}
LOOP_THREAD = None
LOOP_STOP_EVENT = threading.Event()


def _empty_result(msg):
    return {
        "msg": msg,
        "data": {
            "value": None
        }
    }


def _mysql_conn(database=None):
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=database,
        charset="utf8mb4",
        autocommit=False
    )


def _init_mysql():
    conn = _mysql_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "CREATE DATABASE IF NOT EXISTS `{}` DEFAULT CHARACTER SET utf8mb4".format(MYSQL_DATABASE)
            )
        conn.commit()
    finally:
        conn.close()

    conn = _mysql_conn(MYSQL_DATABASE)
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS plc_realtime (
                    point_code VARCHAR(64) PRIMARY KEY,
                    point_name VARCHAR(128) NOT NULL,
                    address VARCHAR(32) NOT NULL,
                    raw_value BIGINT,
                    display_value DOUBLE,
                    unit VARCHAR(32),
                    updated_at DATETIME NOT NULL
                ) DEFAULT CHARSET=utf8mb4
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS plc_history (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    point_code VARCHAR(64) NOT NULL,
                    point_name VARCHAR(128) NOT NULL,
                    address VARCHAR(32) NOT NULL,
                    raw_value BIGINT,
                    display_value DOUBLE,
                    unit VARCHAR(32),
                    created_at DATETIME NOT NULL,
                    INDEX idx_point_time (point_code, created_at)
                ) DEFAULT CHARSET=utf8mb4
            """)
        conn.commit()
    finally:
        conn.close()


def _save_point(point, raw_value, ensure_schema=True):
    if ensure_schema:
        _init_mysql()
    display_value = raw_value * point["scale"] if raw_value is not None else None
    conn = _mysql_conn(MYSQL_DATABASE)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                REPLACE INTO plc_realtime
                (point_code, point_name, address, raw_value, display_value, unit, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                """,
                (
                    point["code"],
                    point["name"],
                    point["address"],
                    raw_value,
                    display_value,
                    point["unit"]
                )
            )
            cursor.execute(
                """
                INSERT INTO plc_history
                (point_code, point_name, address, raw_value, display_value, unit, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                """,
                (
                    point["code"],
                    point["name"],
                    point["address"],
                    raw_value,
                    display_value,
                    point["unit"]
                )
            )
        conn.commit()
    finally:
        conn.close()


def _cleanup_history(retention_hours=HISTORY_RETENTION_HOURS):
    conn = _mysql_conn(MYSQL_DATABASE)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM plc_history WHERE created_at < DATE_SUB(NOW(), INTERVAL %s HOUR)",
                (retention_hours,)
            )
        conn.commit()
    finally:
        conn.close()


def _pick_address(data):
    for key in ("addres", "address", "index"):
        value = data.get(key)
        if value is not None and str(value).strip() != "":
            return key, str(value).strip()
    return None, ""


def _parse_register_address(data):
    key, raw_address = _pick_address(data)
    if not raw_address:
        raise ValueError("addres/address/index 参数不能为空")

    opcua_match = re.match(r"^OPCUA_Read\[(\d+)\]$", raw_address, re.IGNORECASE)
    if opcua_match:
        return OPCUA_READ_BASE + int(opcua_match.group(1)), raw_address

    d_match = re.match(r"^[Dd](\d+)$", raw_address)
    if d_match:
        return int(d_match.group(1)), raw_address

    if raw_address.isdigit():
        address_number = int(raw_address)
        if key == "index" and address_number < OPCUA_READ_BASE:
            return OPCUA_READ_BASE + address_number, "D{}".format(OPCUA_READ_BASE + address_number)
        return address_number, raw_address

    digits = "".join(ch for ch in raw_address if ch.isdigit())
    if digits:
        return int(digits), raw_address

    raise ValueError('地址格式错误，需包含数字，例如 "D2000"、"2000" 或 "OPCUA_Read[0]"')


def _parse_int(data, key, default_value):
    value = data.get(key, default_value)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("{} 必须是整数".format(key))


def _read_register(client, register_addr, device_id):
    response = client.read_holding_registers(
        address=register_addr,
        count=1,
        device_id=device_id
    )

    if isinstance(response, ModbusException):
        raise RuntimeError("读取寄存器失败：{}".format(response))

    if response.isError():
        error_code = getattr(response, "exception_code", response)
        raise RuntimeError("PLC返回错误，地址：D{}，错误码：{}".format(register_addr, error_code))

    return response.registers[0]


def _collect_all(device_id):
    _init_mysql()
    client = ModbusTcpClient(host=PLC_IP, port=PLC_PORT, timeout=PLC_TIMEOUT)
    try:
        if not client.connect():
            return _empty_result("error: 无法连接到PLC，IP：{}，端口：{}".format(PLC_IP, PLC_PORT))

        values = {}
        for point in POINTS:
            register_addr = int(point["address"][1:])
            raw_value = _read_register(client, register_addr, device_id)
            _save_point(point, raw_value, ensure_schema=False)
            values[point["code"]] = raw_value * point["scale"]
            time.sleep(0.02)
        _cleanup_history()

        return {
            "msg": "success",
            "data": {
                "value": values
            }
        }
    except Exception as exc:
        return _empty_result("error: 批量采集异常：{}".format(exc))
    finally:
        client.close()


def _collect_loop_worker(device_id, interval_seconds):
    round_no = 0
    print("PLC循环采集启动，间隔{}秒".format(interval_seconds))
    while not LOOP_STOP_EVENT.is_set():
        round_no += 1
        result = _collect_all(device_id)
        print("PLC循环采集第{}轮: {}".format(round_no, result.get("msg")))
        LOOP_STOP_EVENT.wait(interval_seconds)
    print("PLC循环采集停止")


def _start_collect_loop(device_id, interval_seconds):
    global LOOP_THREAD
    if LOOP_THREAD is not None and LOOP_THREAD.is_alive():
        return {
            "msg": "success",
            "data": {
                "value": "loop already running"
            }
        }

    LOOP_STOP_EVENT.clear()
    LOOP_THREAD = threading.Thread(
        target=_collect_loop_worker,
        args=(device_id, interval_seconds),
        daemon=True
    )
    LOOP_THREAD.start()
    return {
        "msg": "success",
        "data": {
            "value": "loop started"
        }
    }


def _stop_collect_loop():
    LOOP_STOP_EVENT.set()
    return {
        "msg": "success",
        "data": {
            "value": "loop stopping"
        }
    }


def query(data):
    """
    query接口: 读取3号展板汇川PLC的Modbus TCP寄存器原始数据。

    data字典数据:
        addres/address: str PLC寄存器地址，如 "D2000" 或 "2000"
        index: str OPCUA_Read索引，如 "0" 表示 D2000；也兼容 "D2000"
               传 "all" 表示采集一轮全部点位；传 "loop" 表示后台循环采集全部点位
               传 "stop" 表示停止后台循环采集
        device_id: int Modbus从站ID，默认1
        interval: int 循环采集间隔秒数，默认5秒
        count: int 读取寄存器数量，默认1

    返回数据:
        value: int|list 读取到的寄存器原始值
    """
    if data is None:
        data = {}

    try:
        _, raw_address = _pick_address(data)
        device_id = _parse_int(data, "device_id", DEFAULT_DEVICE_ID)
        mode = raw_address.strip().lower()
        if mode in ("loop", "repeat", "auto"):
            interval_seconds = _parse_int(data, "interval", LOOP_INTERVAL_SECONDS)
            if interval_seconds <= 0:
                raise ValueError("interval 必须大于0")
            return _start_collect_loop(device_id, interval_seconds)
        if mode in ("stop", "halt"):
            return _stop_collect_loop()
        if mode in ("all", "*"):
            return _collect_all(device_id)

        register_addr, input_address = _parse_register_address(data)
        count = _parse_int(data, "count", 1)
        if count <= 0:
            raise ValueError("count 必须大于0")
    except ValueError as exc:
        return _empty_result("error: {}".format(exc))

    client = ModbusTcpClient(host=PLC_IP, port=PLC_PORT, timeout=PLC_TIMEOUT)

    try:
        if not client.connect():
            return _empty_result(
                "error: 无法连接到PLC，IP：{}，端口：{}".format(PLC_IP, PLC_PORT)
            )

        response = client.read_holding_registers(
            address=register_addr,
            count=count,
            device_id=device_id
        )

        if isinstance(response, ModbusException):
            return _empty_result("error: 读取寄存器失败：{}".format(response))

        if response.isError():
            error_code = getattr(response, "exception_code", response)
            return _empty_result(
                "error: PLC返回错误，地址：{}，错误码：{}".format(input_address, error_code)
            )

        registers = response.registers
        value = registers[0] if count == 1 else registers
        point = POINT_BY_ADDRESS.get("D{}".format(register_addr))
        if point is not None and count == 1:
            _save_point(point, value)
            _cleanup_history()

        return {
            "msg": "success",
            "data": {
                "value": value
            }
        }

    except Exception as exc:
        return _empty_result("error: 读取过程异常：{}".format(exc))
    finally:
        client.close()
