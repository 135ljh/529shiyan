#!/usr/bin/env python
# -*- coding: utf-8 -*-
import re

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException


PLC_IP = "10.21.2.232"
PLC_PORT = 502
PLC_TIMEOUT = 3
DEFAULT_DEVICE_ID = 1
OPCUA_READ_BASE = 2000


def _empty_result(msg):
    return {
        "msg": msg,
        "data": {
            "value": None,
            "out_value": None
        }
    }


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


def query(data):
    """
    query接口: 读取3号展板汇川PLC的Modbus TCP寄存器原始数据。

    data字典数据:
        addres/address: str PLC寄存器地址，如 "D2000" 或 "2000"
        index: str OPCUA_Read索引，如 "0" 表示 D2000；也兼容 "D2000"
        device_id: int Modbus从站ID，默认1
        count: int 读取寄存器数量，默认1

    返回数据:
        value/out_value: int|list 读取到的寄存器原始值
    """
    if data is None:
        data = {}

    try:
        register_addr, input_address = _parse_register_address(data)
        device_id = _parse_int(data, "device_id", DEFAULT_DEVICE_ID)
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

        return {
            "msg": "success",
            "data": {
                "value": value,
                "out_value": value
            }
        }

    except Exception as exc:
        return _empty_result("error: 读取过程异常：{}".format(exc))
    finally:
        client.close()
