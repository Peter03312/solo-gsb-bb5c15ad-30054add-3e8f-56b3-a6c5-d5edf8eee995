#!/usr/bin/env python3
"""水源停运 / 恢复调度调用示例（仅依赖标准库）。

用法：
    python3 examples/source_switch_demo.py [base_url]

默认 base_url 为 http://localhost:8000。

示例管网（examples/source_switch_network.json）：

    S1(江北水厂) --P1[阀]-- A --P4[阀]-- D --P6(无阀)-- U3(东郊工业园)
                              |
    S2(江南水厂) --P2[阀]-- A --P3[阀]-- U1(幸福小区)

脚本依次演示：
  1. 导入管网（水源初始运行，版本 1）
  2. 预演停运冗余水源 S2：无新增失供（S1 仍供水）-> 只读，版本不变
  3. 确认停运 S2 -> 成功，版本 2
  4. 重复确认相同目标（全部已满足）-> changed=false，版本保持 2
  5. 预演停运最后一个水源 S1：U1/U3 将全部失供（允许全网无运行水源）
  6. 用过期版本确认 -> 409 VERSION_CONFLICT
  7. 用当前版本停运 S1 -> 成功，版本 3，运行水源为空
  8. 无运行水源时提交隔离（故障 P6）：两端本就不可达，空方案可隔离 -> open 事件，版本 4
  9. 直接恢复 S1：待修复 P6 的 D 端将重新进水 -> 预演 safe=false；确认 422 SOURCE_REWATER_RISK
 10. 编号非法（用水点 U1）/ 同批重复 -> 预演 issues；确认 422 SOURCE_SWITCH_INVALID
 11. 先复供 open 事件（无运行水源，安全）-> 版本 5
 12. 再恢复 S1：U1/U3 恢复供水，版本 6
 13. 读网确认水源运行状态（running 字段）
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "BASE_URL", "http://localhost:8000"
)
SAMPLE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "source_switch_network.json"
)


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def show(title: str, status: int, body: dict) -> None:
    print(f"\n=== {title} -> HTTP {status} ===")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def switch(source_id: str, running: bool, version: int) -> tuple[int, dict]:
    return call(
        "POST",
        "/api/sources/switch",
        {"switches": [{"source_id": source_id, "running": running}], "version": version},
    )


def switch_preview(source_id: str, running: bool, version: int) -> tuple[int, dict]:
    return call(
        "POST",
        "/api/sources/switch/preview",
        {"switches": [{"source_id": source_id, "running": running}], "version": version},
    )


def main() -> None:
    with open(SAMPLE, encoding="utf-8") as f:
        network = json.load(f)

    # 1. 导入管网：水源初始视为运行
    status, body = call("POST", "/api/network/import", network)
    show("1. 导入双水源管网（S1/S2 初始运行）", status, body)
    assert status == 200 and body["version"] == 1, body

    # 2. 预演停运冗余水源 S2（只读）
    status, body = switch_preview("S2", False, 1)
    show("2. 预演停运 S2（冗余水源，无新增失供）", status, body)
    assert status == 200 and body["safe"] is True, body
    assert body["changed"] is True and body["new_loss_sink_ids"] == [], body
    assert body["running_source_ids"] == ["S1"], body

    # 3. 确认停运 S2 -> 版本 2
    status, body = switch("S2", False, 1)
    show("3. 确认停运 S2（预期成功，版本 2）", status, body)
    assert status == 200 and body["version"] == 2, body

    # 4. 全部目标状态已满足 -> changed=false，不推进版本
    status, body = switch("S2", False, 2)
    show("4. 重复确认停运 S2（预期 changed=false，版本保持 2）", status, body)
    assert status == 200 and body["changed"] is False and body["version"] == 2, body

    # 5. 预演停运最后一个运行水源 S1：全网失供（允许）
    status, body = switch_preview("S1", False, 2)
    show("5. 预演停运最后水源 S1（U1/U3 全部失供，允许全网无水源）", status, body)
    assert status == 200 and body["safe"] is True, body
    assert body["new_loss_sink_ids"] == ["U1", "U3"], body
    assert body["running_source_ids"] == [], body

    # 6. 过期版本确认 -> 409
    status, body = switch("S1", False, 1)
    show("6. 过期版本停运 S1（预期 409 VERSION_CONFLICT）", status, body)
    assert status == 409 and body["error"]["code"] == "VERSION_CONFLICT", body

    # 7. 当前版本停运 S1 -> 版本 3，无运行水源
    status, body = switch("S1", False, 2)
    show("7. 确认停运 S1（预期成功，版本 3，运行水源为空）", status, body)
    assert status == 200 and body["version"] == 3, body
    assert body["running_source_ids"] == [], body

    # 8. 无运行水源时提交隔离：故障 P6 两端本就不可达，空方案即可隔离
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P6", "close_pipe_ids": [], "version": 3},
    )
    show("8. 无运行水源下隔离故障 P6（空方案可隔离，open 事件）", status, body)
    assert status == 200 and body["event_id"] == 1, body
    assert body["affected_sink_ids"] == [], body

    # 9a. 直接恢复 S1：待修复 P6 的 D 端将重新进水 -> 预演 safe=false
    status, body = switch_preview("S1", True, 4)
    show("9a. 预演恢复 S1（待修复 P6 的 D 端将重新进水，safe=false）", status, body)
    assert status == 200 and body["safe"] is False, body
    assert body["block_reasons"][0]["code"] == "FAULT_REWATER_RISK", body
    assert body["block_reasons"][0]["pipe_id"] == "P6", body
    assert body["block_reasons"][0]["reachable_endpoints"] == ["D"], body

    # 9b. 强行确认 -> 422 SOURCE_REWATER_RISK，无部分写入
    status, body = switch("S1", True, 4)
    show("9b. 确认恢复 S1（预期 422 SOURCE_REWATER_RISK）", status, body)
    assert status == 422 and body["error"]["code"] == "SOURCE_REWATER_RISK", body

    # 10a. 预演编号非法：用水点不是水源
    status, body = call(
        "POST",
        "/api/sources/switch/preview",
        {"switches": [{"source_id": "U1", "running": False}], "version": 4},
    )
    show("10a. 预演切换用水点 U1（预期 issues=SOURCE_NOT_FOUND）", status, body)
    assert status == 200 and body["issues"][0]["code"] == "SOURCE_NOT_FOUND", body

    # 10b. 确认编号非法与同批重复 -> 422 SOURCE_SWITCH_INVALID
    status, body = call(
        "POST",
        "/api/sources/switch",
        {"switches": [{"source_id": "A", "running": True}], "version": 4},
    )
    show("10b. 确认切换连接点 A（预期 422 SOURCE_SWITCH_INVALID）", status, body)
    assert status == 422 and body["error"]["code"] == "SOURCE_SWITCH_INVALID", body

    status, body = call(
        "POST",
        "/api/sources/switch",
        {
            "switches": [
                {"source_id": "S1", "running": True},
                {"source_id": "S1", "running": False},
            ],
            "version": 4,
        },
    )
    show("10c. 同批编号重复（预期 422 SOURCE_SWITCH_INVALID）", status, body)
    assert status == 422 and "SOURCE_SWITCH_DUPLICATE" in json.dumps(body, ensure_ascii=False), body

    # 11. 先复供 open 事件（无运行水源，重开阀门也无进水风险）-> 版本 5
    status, body = call(
        "POST", "/api/isolation/events/1/restore", {"version": 4}
    )
    show("11. 复供事件 1（无运行水源，安全，版本 5）", status, body)
    assert status == 200 and body["version"] == 5, body

    # 12. 再恢复 S1：无待修复故障，U1/U3 恢复，版本 6
    status, body = switch("S1", True, 5)
    show("12. 恢复 S1（预期成功，U1/U3 恢复，版本 6）", status, body)
    assert status == 200 and body["version"] == 6, body
    assert body["restored_sink_ids"] == ["U1", "U3"], body
    assert body["running_source_ids"] == ["S1"], body

    # 13. 读网确认 running 字段
    status, body = call("GET", "/api/network")
    running = {n["id"]: n["running"] for n in body["nodes"] if n["type"] == "source"}
    show("13. 读取管网（水源 running：" + str(running) + "）", status, body)
    assert running == {"S1": True, "S2": False}, running

    print("\n全部水源调度示例执行完成。")


if __name__ == "__main__":
    main()
