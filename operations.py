#!/usr/bin/env python
# -*- coding: utf-8 -*-


def query(data):
    """
    query接口:无描述

    data字典数据:
        index: str 无描述

    返回数据中data字段必填并以约定格式返回data字典:
        value: str 无描述

    """
    # 方法具体代码逻辑

    # 以固定格式返回数据
    data = {
        'value': None
    }
    return {
        'msg': 'success',
        'data': data
    }

