"""API 路由层集成测试。

本机无 PostgreSQL，使用内存假连接替身替换 app.db 的数据访问函数，
覆盖 FastAPI 路由、请求校验、事务与错误码的完整行为。
运行：python3 -m unittest tests.test_api -v
（需要 fastapi/httpx 依赖）
"""
import json
import os
import sys
import unittest
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from app.planning import Node, Pipe  # noqa: E402


class FakeStore:
    def __init__(self):
        self.version = 0
        self.updated_at = None
        self.nodes: dict[str, Node] = {}
        self.pipes: dict[str, Pipe] = {}
        self.events = []
        self.next_event_id = 1
        self.fail_commit = False


STORE = FakeStore()


@contextmanager
def fake_get_conn():
    yield "FAKE_CONN"


@contextmanager
def fake_transaction():
    conn = "FAKE_TX"
    # 简单快照：提交时把暂存区落盘
    snapshot = (
        STORE.version,
        dict(STORE.nodes),
        {k: Pipe(**v.__dict__) for k, v in STORE.pipes.items()},
    )
    tmp = {
        "version": STORE.version,
        "nodes": dict(STORE.nodes),
        "pipes": dict(STORE.pipes),
        "events": list(STORE.events),
        "next_id": STORE.next_event_id,
    }
    try:
        yield conn
        if STORE.fail_commit:
            raise RuntimeError("simulated commit failure")
    except Exception:
        STORE.version = tmp["version"]
        STORE.nodes = tmp["nodes"]
        STORE.pipes = tmp["pipes"]
        STORE.events = tmp["events"]
        STORE.next_event_id = tmp["next_id"]
        raise


def fake_init_db():
    return None


def fake_load_snapshot(conn, for_update=False):
    return STORE.version, STORE.updated_at, dict(STORE.nodes), dict(STORE.pipes)


def fake_replace_network(conn, nodes, pipes):
    STORE.version = 1
    STORE.nodes = {n.id: n for n in nodes}
    STORE.pipes = {p.id: p for p in pipes}
    STORE.events = []
    STORE.next_event_id = 1
    return 1


def fake_submit_isolation(conn, expected_version, fault_pipe_id, close_pipe_ids, result):
    # 复制 db.submit_isolation 的关键语义：锁行版本比对 -> 更新阀位 -> +1 -> 事件
    if STORE.version != expected_version:
        raise db.VersionConflict(STORE.version, expected_version)
    for pid in close_pipe_ids:
        p = STORE.pipes[pid]
        STORE.pipes[pid] = Pipe(
            p.id, p.from_node, p.to_node, p.has_valve, True, p.name
        )
    before = STORE.version
    STORE.version += 1
    eid = STORE.next_event_id
    STORE.next_event_id += 1
    import datetime

    event = {
        "id": eid,
        "version_before": before,
        "version_after": STORE.version,
        "fault_pipe_id": fault_pipe_id,
        "closed_pipe_ids": list(close_pipe_ids),
        "affected_sink_ids": list(result.affected_sink_ids),
        "status": "open",
        "restored_at": None,
        "created_at": datetime.datetime(2026, 9, 30, 12, 0, 0),
    }
    STORE.events.insert(0, event)
    return {"version": STORE.version, "event_id": eid, "created_at": event["created_at"]}


def fake_list_events(conn, limit):
    return STORE.events[:limit]


def fake_get_event(conn, event_id, for_update=False):
    for e in STORE.events:
        if e["id"] == event_id:
            return e
    return None


def fake_list_open_events(conn, exclude_event_id=None, for_update=False):
    return [
        e
        for e in STORE.events
        if e.get("status", "open") == "open" and e["id"] != exclude_event_id
    ]


def fake_confirm_restore(
    conn, event_id, expected_version, reopened_pipe_ids, restored_sink_ids
):
    if STORE.version != expected_version:
        raise db.VersionConflict(STORE.version, expected_version)
    event = next((e for e in STORE.events if e["id"] == event_id), None)
    if event is None or event.get("status", "open") != "open":
        raise RuntimeError("event not open")
    for pid in reopened_pipe_ids:
        p = STORE.pipes[pid]
        STORE.pipes[pid] = Pipe(
            p.id, p.from_node, p.to_node, p.has_valve, False, p.name
        )
    before = STORE.version
    STORE.version += 1
    import datetime

    restored_at = datetime.datetime(2026, 9, 30, 13, 0, 0)
    event["status"] = "restored"
    event["restored_at"] = restored_at
    return {"version": STORE.version, "restored_at": restored_at}


# 替换数据访问层
db.get_conn = fake_get_conn
db.transaction = fake_transaction
db.init_db = fake_init_db
db.load_snapshot = fake_load_snapshot
db.replace_network = fake_replace_network
db.submit_isolation = fake_submit_isolation
db.list_events = fake_list_events
db.get_event = fake_get_event
db.list_open_events = fake_list_open_events
db.confirm_restore = fake_confirm_restore


def fake_switch_sources(conn, expected_version, changed_targets):
    # 复制 db.switch_sources 的关键语义：锁行版本比对 -> 更新水源状态 -> 有变化才 +1
    if STORE.version != expected_version:
        raise db.VersionConflict(STORE.version, expected_version)
    for source_id, running in changed_targets.items():
        n = STORE.nodes.get(source_id)
        if n is None or n.type != "source":
            raise RuntimeError(f"水源节点不存在或已变化：{source_id}")
        STORE.nodes[source_id] = Node(
            id=n.id, type=n.type, name=n.name, running=running
        )
    if changed_targets:
        STORE.version += 1
    return STORE.version


db.switch_sources = fake_switch_sources


VALID_NETWORK = {
    "nodes": [
        {"id": "S", "type": "source", "name": "一水厂"},
        {"id": "A", "type": "junction"},
        {"id": "B", "type": "junction"},
        {"id": "D", "type": "junction"},
        {"id": "U1", "type": "sink"},
        {"id": "U2", "type": "sink"},
        {"id": "U3", "type": "sink"},
    ],
    "pipes": [
        {"id": "P1", "from": "S", "to": "A", "has_valve": True},
        {"id": "P2", "from": "A", "to": "B", "has_valve": True},
        {"id": "P3", "from": "A", "to": "U1", "has_valve": True},
        {"id": "P4", "from": "B", "to": "D", "has_valve": True},
        {"id": "P5", "from": "B", "to": "U2", "has_valve": True},
        {"id": "P6", "from": "D", "to": "U3", "has_valve": False},
    ],
}


RESTORE_NETWORK = {
    "nodes": [
        {"id": "S", "type": "source"},
        {"id": "A", "type": "junction"},
        {"id": "B", "type": "junction"},
        {"id": "D", "type": "junction"},
        {"id": "E", "type": "junction"},
        {"id": "U1", "type": "sink"},
        {"id": "U2", "type": "sink"},
        {"id": "U3", "type": "sink"},
        {"id": "U4", "type": "sink"},
    ],
    "pipes": [
        {"id": "P1", "from": "S", "to": "A", "has_valve": True},
        {"id": "P2", "from": "A", "to": "B", "has_valve": True},
        {"id": "P3", "from": "A", "to": "U1", "has_valve": True},
        {"id": "P5", "from": "B", "to": "U2", "has_valve": True},
        {"id": "P4", "from": "B", "to": "D", "has_valve": True},
        {"id": "P6", "from": "D", "to": "U3", "has_valve": False},
        # P7 为已隔离区内的无阀管段（E2 故障管段），自身无阀可关
        {"id": "P7", "from": "D", "to": "E", "has_valve": False},
        {"id": "P8", "from": "E", "to": "U4", "has_valve": True},
    ],
}


SOURCE_NETWORK = {
    # 双水源管网：
    # S1 --P1-- A --P4-- D --P6(无阀)-- U3
    # S2 --P2-- A --P3-- U1
    "nodes": [
        {"id": "S1", "type": "source"},
        {"id": "S2", "type": "source"},
        {"id": "A", "type": "junction"},
        {"id": "D", "type": "junction"},
        {"id": "U1", "type": "sink"},
        {"id": "U3", "type": "sink"},
    ],
    "pipes": [
        {"id": "P1", "from": "S1", "to": "A", "has_valve": True},
        {"id": "P2", "from": "S2", "to": "A", "has_valve": True},
        {"id": "P3", "from": "A", "to": "U1", "has_valve": True},
        {"id": "P4", "from": "A", "to": "D", "has_valve": True},
        {"id": "P6", "from": "D", "to": "U3", "has_valve": False},
    ],
}


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # lifespan 会调用 init_db（已替换为 fake）
        cls.client = TestClient(app)

    def setUp(self):
        STORE.version = 0
        STORE.nodes = {}
        STORE.pipes = {}
        STORE.events = []
        STORE.next_event_id = 1
        STORE.fail_commit = False

    def _import(self):
        r = self.client.post("/api/network/import", json=VALID_NETWORK)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

    def test_full_happy_path(self):
        info = self._import()
        self.assertEqual(info["version"], 1)

        r = self.client.get("/api/network")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["version"], 1)
        self.assertEqual(len(body["nodes"]), 7)
        self.assertEqual(len(body["pipes"]), 6)
        # 序列化别名：from/to
        p1 = next(p for p in body["pipes"] if p["id"] == "P1")
        self.assertEqual(p1["from"], "S")
        self.assertEqual(p1["to"], "A")
        # 无阀管段 valve_closed 序列化为 null
        p6 = next(p for p in body["pipes"] if p["id"] == "P6")
        self.assertIsNone(p6["valve_closed"])

        # 不可行预演
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P5"], "version": 1},
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["isolatable"])

        # 可行预演
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        body = r.json()
        self.assertTrue(body["isolatable"])
        self.assertEqual(body["affected_sink_ids"], ["U3"])
        self.assertTrue(body["version_match"])

        # 提交成功
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 2)
        self.assertEqual(body["affected_sink_ids"], ["U3"])
        self.assertEqual(body["closed_pipe_ids"], ["P4"])
        self.assertEqual(body["event_id"], 1)

        # 阀位确实落库
        r = self.client.get("/api/network")
        p4 = next(p for p in r.json()["pipes"] if p["id"] == "P4")
        self.assertTrue(p4["valve_closed"])

        # 历史
        r = self.client.get("/api/isolation/events")
        self.assertEqual(r.status_code, 200)
        events = r.json()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["fault_pipe_id"], "P6")
        self.assertEqual(events[0]["closed_pipe_ids"], ["P4"])

    def test_import_rejects_dangling_self_loop_duplicate(self):
        bad = {
            "nodes": [
                {"id": "S", "type": "source"},
                {"id": "S", "type": "junction"},
            ],
            "pipes": [
                {"id": "P1", "from": "S", "to": "X"},
                {"id": "P1", "from": "S", "to": "S"},
            ],
        }
        r = self.client.post("/api/network/import", json=bad)
        self.assertEqual(r.status_code, 422)
        codes = {i["code"] for i in r.json()["error"]["issues"]}
        self.assertIn("DUPLICATE_NODE_ID", codes)
        self.assertIn("DUPLICATE_PIPE_ID", codes)
        self.assertIn("PIPE_ENDPOINT_UNKNOWN", codes)
        self.assertIn("PIPE_SELF_LOOP", codes)
        # 拒绝后状态不变（空网版本 0）
        r = self.client.get("/api/network")
        self.assertEqual(r.json()["version"], 0)

    def test_non_valve_pipe_initial_closed_rejected(self):
        bad = dict(VALID_NETWORK)
        bad["pipes"] = [
            dict(p, valve_closed=True) if p["id"] == "P6" else p
            for p in VALID_NETWORK["pipes"]
        ]
        r = self.client.post("/api/network/import", json=bad)
        self.assertEqual(r.status_code, 422)
        self.assertIn("NON_VALVE_PIPE_CLOSED", r.text)

    def test_submit_version_conflict(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 99},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "VERSION_CONFLICT")
        # 状态不变
        self.assertEqual(STORE.version, 1)

    def test_submit_no_valve_pipe(self):
        self._import()
        # 关闭无阀管段 P6（以 P5 为故障管段）-> 422 CLOSE_PIPE_NO_VALVE
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P5", "close_pipe_ids": ["P6"], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("CLOSE_PIPE_NO_VALVE", r.text)
        self.assertEqual(STORE.version, 1)

    def test_submit_duplicate_and_already_closed(self):
        self._import()
        # 列表内重复
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4", "P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("CLOSE_PIPE_DUPLICATE", r.text)

        # 先成功关 P4
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200)

        # 版本 2 下再次关已关闭的 P4
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 2},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("CLOSE_PIPE_DUPLICATE", r.text)

    def test_submit_not_isolatable(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P5"], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "NOT_ISOLATABLE")
        self.assertEqual(STORE.version, 1)

    def test_submit_fault_pipe_as_valve(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P6"], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("CLOSE_PIPE_IS_FAULT", r.text)

    def test_preview_stale_version_flag(self):
        self._import()
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 5},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["version_match"])
        self.assertEqual(body["version"], 1)

    def test_request_body_validation_422(self):
        self._import()
        # version 不允许负数
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": [], "version": -1},
        )
        self.assertEqual(r.status_code, 422)

    def test_submit_against_empty_network_is_version_conflict(self):
        # 空网版本为 0：基于版本 1 提交应得到 409，而非参数校验错误
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "VERSION_CONFLICT")

    def test_reimport_resets_version_and_events(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(STORE.version, 2)
        # 重新导入 -> 版本重置为 1、历史清空
        r = self.client.post("/api/network/import", json=VALID_NETWORK)
        self.assertEqual(r.json()["version"], 1)
        self.assertEqual(STORE.events, [])

    def test_failed_submit_rolls_back(self):
        self._import()
        STORE.fail_commit = True
        # TestClient 默认抛出服务端异常；真实 HTTP 下客户端会收到 500。
        # 关键断言：事务回滚后版本与阀位均不变。
        with self.assertRaises(RuntimeError):
            self.client.post(
                "/api/isolation/submit",
                json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
            )
        self.assertEqual(STORE.version, 1)
        self.assertFalse(STORE.pipes["P4"].valve_closed)

    # ---------------------------------------------------------- 复供

    def _submit_two_faults(self):
        # 导入复供演示管网
        r = self.client.post("/api/network/import", json=RESTORE_NETWORK)
        self.assertEqual(r.status_code, 200, r.text)
        # 事件 1：故障 P6（D-U3），关 P4（B-D）隔离整个 D 片区
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["event_id"], 1)
        # 事件 2：故障 P7（D-E，无阀）。D 片区已被 P4 隔离，
        # 故无需再关阀门（P8 阀保持开启），提交空方案即可隔离
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P7", "close_pipe_ids": [], "version": 2},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["event_id"], 2)
        self.assertEqual(STORE.version, 3)

    def test_restore_preview_readonly_and_safe_for_inner_fault(self):
        self._submit_two_faults()
        # 复供事件 2（片区内故障 P7，未关任何阀）：重开集合为空，
        # 仍关闭的 P4 继续阻断，其余故障 P6 两端 D/U3 均不可达 -> 安全
        r = self.client.post(
            "/api/isolation/events/2/restore/preview", json={"version": 3}
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["version_match"])
        self.assertEqual(body["status"], "open")
        self.assertEqual(body["fault_pipe_id"], "P7")
        self.assertEqual(body["closed_pipe_ids"], [])
        self.assertTrue(body["safe"])
        self.assertEqual(body["restored_sink_ids"], [])
        self.assertEqual(body["block_reasons"], [])
        # 预演不改状态
        self.assertEqual(STORE.version, 3)
        self.assertTrue(STORE.pipes["P4"].valve_closed)
        self.assertFalse(STORE.pipes["P8"].valve_closed)
        self.assertEqual(STORE.events[0]["status"], "open")

    def test_restore_preview_flags_rewater_risk_for_outer_fault(self):
        self._submit_two_faults()
        # 复供事件 1（外层故障 P6，关 P4）：重开 P4 后 D 区进水，
        # 仍待修复的 P7 的 D 端将重新从水源到达（仅一端进水即有风险）-> 禁止复供。
        # U3 恢复；U4 因故障管段 P7 本身仍阻断，暂不恢复。
        r = self.client.post(
            "/api/isolation/events/1/restore/preview", json={"version": 3}
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["safe"])
        self.assertEqual(body["restored_sink_ids"], ["U3"])
        self.assertEqual(len(body["block_reasons"]), 1)
        block = body["block_reasons"][0]
        self.assertEqual(block["code"], "FAULT_REWATER_RISK")
        self.assertEqual(block["pipe_id"], "P7")
        self.assertEqual(block["reachable_endpoints"], ["D"])

    def test_restore_confirm_rejected_when_unsafe(self):
        self._submit_two_faults()
        r = self.client.post(
            "/api/isolation/events/1/restore", json={"version": 3}
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "RESTORE_UNSAFE")
        self.assertIn("FAULT_REWATER_RISK", r.text)
        # 无部分变更：阀位、版本、事件状态均不变
        self.assertEqual(STORE.version, 3)
        self.assertTrue(STORE.pipes["P4"].valve_closed)
        self.assertEqual(STORE.events[-1]["status"], "open")
        self.assertEqual(STORE.events[0]["status"], "open")

    def test_restore_confirm_safe_then_outer_becomes_restorable(self):
        self._submit_two_faults()
        # 先安全复供区内事件 2（没有阀门被重开，不恢复用水点）
        r = self.client.post("/api/isolation/events/2/restore", json={"version": 3})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 4)
        self.assertEqual(body["status"], "restored")
        self.assertEqual(body["reopened_pipe_ids"], [])
        self.assertEqual(body["restored_sink_ids"], [])
        # P4 仍关闭
        self.assertTrue(STORE.pipes["P4"].valve_closed)

        # 其余待修复故障只剩 P6，复供事件 1 变安全，U3/U4 恢复
        r = self.client.post(
            "/api/isolation/events/1/restore/preview", json={"version": 4}
        )
        body = r.json()
        self.assertTrue(body["safe"])
        self.assertEqual(body["restored_sink_ids"], ["U3", "U4"])

        r = self.client.post("/api/isolation/events/1/restore", json={"version": 4})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["version"], 5)
        self.assertEqual(r.json()["restored_sink_ids"], ["U3", "U4"])
        self.assertFalse(STORE.pipes["P4"].valve_closed)

    def test_restore_event_not_found_and_already_restored(self):
        self._submit_two_faults()
        # 不存在的事件 -> 404
        r = self.client.post(
            "/api/isolation/events/999/restore/preview", json={"version": 3}
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "EVENT_NOT_FOUND")
        r = self.client.post(
            "/api/isolation/events/999/restore", json={"version": 3}
        )
        self.assertEqual(r.status_code, 404)

        # 成功复供事件 2 后重复复供 -> 409
        r = self.client.post("/api/isolation/events/2/restore", json={"version": 3})
        self.assertEqual(r.status_code, 200)
        r = self.client.post(
            "/api/isolation/events/2/restore/preview", json={"version": 4}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "EVENT_ALREADY_RESTORED")
        r = self.client.post(
            "/api/isolation/events/2/restore", json={"version": 4}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(STORE.version, 4)

    def test_restore_version_conflict(self):
        self._submit_two_faults()
        r = self.client.post(
            "/api/isolation/events/2/restore", json={"version": 99}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "VERSION_CONFLICT")
        # 状态不变
        self.assertEqual(STORE.version, 3)
        self.assertTrue(STORE.pipes["P4"].valve_closed)
        self.assertEqual(STORE.events[0]["status"], "open")
        # 预演不拒绝过期版本，只标记 version_match=false
        r = self.client.post(
            "/api/isolation/events/2/restore/preview", json={"version": 99}
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["version_match"])

    def test_events_history_contains_status_fields(self):
        self._submit_two_faults()
        r = self.client.get("/api/isolation/events")
        events = r.json()
        self.assertEqual([e["id"] for e in events], [2, 1])
        for e in events:
            self.assertEqual(e["status"], "open")
            self.assertIsNone(e["restored_at"])
        # 复供后历史反映新状态
        self.client.post("/api/isolation/events/2/restore", json={"version": 3})
        events = self.client.get("/api/isolation/events").json()
        e2 = next(e for e in events if e["id"] == 2)
        self.assertEqual(e2["status"], "restored")
        self.assertIsNotNone(e2["restored_at"])

    def test_preview_and_submit_count_pending_faults(self):
        self._submit_two_faults()
        # 两条待修复事件后：D 片区（U3/U4）已因关 P4 与故障 P7 失供。
        # 对 P5（B-U2）做隔离预演（关 P2 隔离 B 侧）：通水计算排除待修复故障
        # 管段 P6/P7，U3/U4 在方案前已失供不重复计入；新增失供为 U2。
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P5", "close_pipe_ids": ["P2"], "version": 3},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["isolatable"])
        self.assertEqual(body["affected_sink_ids"], ["U2"])

    # ---------------------------------------------------------- 关阀建议

    def test_recommend_basic_and_submittable(self):
        self._import()
        r = self.client.post(
            "/api/isolation/recommend",
            json={"fault_pipe_id": "P6", "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 1)
        self.assertTrue(body["version_match"])
        self.assertEqual(body["fault_pipe_id"], "P6")
        self.assertTrue(body["feasible"])
        self.assertEqual(body["recommended_close_pipe_ids"], ["P4"])
        self.assertEqual(body["affected_sink_ids"], ["U3"])
        self.assertIsNone(body["reason"])
        # 只读：版本、阀位、事件均未变化
        self.assertEqual(STORE.version, 1)
        self.assertFalse(STORE.pipes["P4"].valve_closed)
        self.assertEqual(STORE.events, [])
        # 建议可直接提交：同一版本提交成功
        r = self.client.post(
            "/api/isolation/submit",
            json={
                "fault_pipe_id": "P6",
                "close_pipe_ids": body["recommended_close_pipe_ids"],
                "version": body["version"],
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["closed_pipe_ids"], ["P4"])
        self.assertEqual(r.json()["affected_sink_ids"], ["U3"])

    def test_recommend_stale_version_flag(self):
        self._import()
        r = self.client.post(
            "/api/isolation/recommend",
            json={"fault_pipe_id": "P6", "version": 5},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["version_match"])
        self.assertEqual(body["version"], 1)
        self.assertTrue(body["feasible"])

    def test_recommend_fault_pipe_not_found(self):
        self._import()
        r = self.client.post(
            "/api/isolation/recommend",
            json={"fault_pipe_id": "PX", "version": 1},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["feasible"])
        self.assertEqual(body["recommended_close_pipe_ids"], [])
        self.assertEqual(body["affected_sink_ids"], [])
        self.assertEqual(body["reason"]["code"], "FAULT_PIPE_NOT_FOUND")
        # 不改状态
        self.assertEqual(STORE.version, 1)

    def test_recommend_no_feasible_plan(self):
        self._import()
        # 故障管段 P1 直连水源：S 端恒可达，无可行方案
        r = self.client.post(
            "/api/isolation/recommend",
            json={"fault_pipe_id": "P1", "version": 1},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["feasible"])
        self.assertEqual(body["reason"]["code"], "NO_FEASIBLE_PLAN")
        self.assertEqual(STORE.version, 1)

    def test_recommend_excludes_closed_valves_and_counts_pending_faults(self):
        self._import()
        # 先提交事件：故障 P6，关 P4（版本变为 2）
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        # 新故障 P5（B-U2）：建议不得包含已关闭的 P4，
        # 待修复故障 P6 不计通行，U3 已失供不重复计入
        r = self.client.post(
            "/api/isolation/recommend",
            json={"fault_pipe_id": "P5", "version": 2},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["feasible"])
        self.assertEqual(body["recommended_close_pipe_ids"], ["P2"])
        self.assertEqual(body["affected_sink_ids"], ["U2"])
        # 只读：建议后版本与阀位不变
        self.assertEqual(STORE.version, 2)
        # 按建议提交成功（版本 3）
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P5", "close_pipe_ids": ["P2"], "version": 2},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["version"], 3)

    def test_recommend_empty_plan_when_fault_already_isolated(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        # 故障管段两端已与水源断开：建议空方案
        r = self.client.post(
            "/api/isolation/recommend",
            json={"fault_pipe_id": "P6", "version": 2},
        )
        body = r.json()
        self.assertTrue(body["feasible"])
        self.assertEqual(body["recommended_close_pipe_ids"], [])
        self.assertEqual(body["affected_sink_ids"], [])

    def test_recommend_submit_stale_version_conflict(self):
        self._import()
        r = self.client.post(
            "/api/isolation/recommend",
            json={"fault_pipe_id": "P6", "version": 1},
        )
        rec = r.json()
        # 期间发生另一次提交，版本变为 2：按旧版本提交建议 -> 409（沿用现有冲突处理）
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P5", "close_pipe_ids": ["P2"], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post(
            "/api/isolation/submit",
            json={
                "fault_pipe_id": "P6",
                "close_pipe_ids": rec["recommended_close_pipe_ids"],
                "version": rec["version"],
            },
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "VERSION_CONFLICT")
        # 重新按最新版本建议并提交 -> 成功
        r = self.client.post(
            "/api/isolation/recommend",
            json={"fault_pipe_id": "P6", "version": 2},
        )
        rec = r.json()
        self.assertTrue(rec["version_match"])
        r = self.client.post(
            "/api/isolation/submit",
            json={
                "fault_pipe_id": "P6",
                "close_pipe_ids": rec["recommended_close_pipe_ids"],
                "version": rec["version"],
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["version"], 3)

    def test_recommend_request_body_validation_422(self):
        self._import()
        r = self.client.post(
            "/api/isolation/recommend",
            json={"fault_pipe_id": "P6", "version": -1},
        )
        self.assertEqual(r.status_code, 422)
        r = self.client.post("/api/isolation/recommend", json={"version": 1})
        self.assertEqual(r.status_code, 422)

    def test_reimport_invalidates_old_events_for_restore(self):
        self._submit_two_faults()
        r = self.client.post("/api/network/import", json=RESTORE_NETWORK)
        self.assertEqual(r.status_code, 200)
        # 整网替换后旧事件失效（历史清空）：复供旧事件编号 -> 404
        r = self.client.post(
            "/api/isolation/events/1/restore", json={"version": 1}
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "EVENT_NOT_FOUND")

    # ---------------------------------------------------------- 水源调度

    def _import_sources(self):
        r = self.client.post("/api/network/import", json=SOURCE_NETWORK)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_network_includes_source_running_flag(self):
        self._import()
        body = self.client.get("/api/network").json()
        src = next(n for n in body["nodes"] if n["id"] == "S")
        self.assertIn("running", src)
        self.assertTrue(src["running"])
        # 连接点/用水点也带 running=true（仅水源可切换）
        self.assertTrue(all("running" in n for n in body["nodes"]))

    def test_source_switch_preview_readonly_stop_redundant(self):
        self._import_sources()
        r = self.client.post(
            "/api/sources/switch/preview",
            json={"switches": [{"source_id": "S2", "running": False}], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 1)
        self.assertTrue(body["version_match"])
        self.assertEqual(body["switches"], [{"source_id": "S2", "running": False}])
        self.assertTrue(body["changed"])
        self.assertTrue(body["safe"])
        self.assertEqual(body["new_loss_sink_ids"], [])
        self.assertEqual(body["restored_sink_ids"], [])
        self.assertEqual(body["running_source_ids"], ["S1"])
        self.assertEqual(body["block_reasons"], [])
        self.assertEqual(body["issues"], [])
        # 只读：版本与节点状态不变
        self.assertEqual(STORE.version, 1)
        self.assertTrue(STORE.nodes["S2"].running)

    def test_source_switch_preview_stop_last_source_all_sinks_lost(self):
        self._import_sources()
        # 先停运 S2（版本 2）
        r = self.client.post(
            "/api/sources/switch",
            json={"switches": [{"source_id": "S2", "running": False}], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        # 预演停运最后一个运行水源 S1：允许，U1/U3 全部新增失供
        r = self.client.post(
            "/api/sources/switch/preview",
            json={"switches": [{"source_id": "S1", "running": False}], "version": 2},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["safe"])
        self.assertEqual(body["new_loss_sink_ids"], ["U1", "U3"])
        self.assertEqual(body["running_source_ids"], [])

    def test_source_switch_confirm_bumps_version_and_persists(self):
        self._import_sources()
        r = self.client.post(
            "/api/sources/switch",
            json={"switches": [{"source_id": "S2", "running": False}], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 2)
        self.assertTrue(body["changed"])
        self.assertEqual(body["running_source_ids"], ["S1"])
        self.assertFalse(STORE.nodes["S2"].running)
        self.assertTrue(STORE.nodes["S1"].running)
        # 读网反映状态
        net = self.client.get("/api/network").json()
        s2 = next(n for n in net["nodes"] if n["id"] == "S2")
        self.assertFalse(s2["running"])

    def test_source_switch_noop_does_not_advance_version(self):
        self._import_sources()
        # 全部目标状态已满足 -> changed=false，不推进版本
        r = self.client.post(
            "/api/sources/switch",
            json={
                "switches": [
                    {"source_id": "S1", "running": True},
                    {"source_id": "S2", "running": True},
                ],
                "version": 1,
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["changed"])
        self.assertEqual(body["version"], 1)
        self.assertEqual(STORE.version, 1)

        # 混合：一个无变化、一个有变化 -> 推进版本
        r = self.client.post(
            "/api/sources/switch",
            json={
                "switches": [
                    {"source_id": "S1", "running": True},
                    {"source_id": "S2", "running": False},
                ],
                "version": 1,
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["changed"])
        self.assertEqual(r.json()["version"], 2)

    def test_source_switch_restore_returns_sinks_preview(self):
        self._import_sources()
        # 停运 S2 -> 停运 S1（双停，版本 3）
        self.client.post(
            "/api/sources/switch",
            json={"switches": [{"source_id": "S2", "running": False}], "version": 1},
        )
        self.client.post(
            "/api/sources/switch",
            json={"switches": [{"source_id": "S1", "running": False}], "version": 2},
        )
        self.assertEqual(STORE.version, 3)
        # 预演恢复 S1：U1/U3 恢复供水
        r = self.client.post(
            "/api/sources/switch/preview",
            json={"switches": [{"source_id": "S1", "running": True}], "version": 3},
        )
        body = r.json()
        self.assertTrue(body["safe"])
        self.assertEqual(body["restored_sink_ids"], ["U1", "U3"])
        self.assertEqual(body["running_source_ids"], ["S1"])
        # 只读
        self.assertEqual(STORE.version, 3)

    def test_source_switch_restore_blocked_by_pending_fault(self):
        # 场景 A：待修复故障 P6 + 隔离阀 P4 已关闭 -> 恢复水源安全
        self._import_sources()
        # 提交隔离事件：故障 P6（D-U3），关 P4 隔离 D 区 -> 版本 2
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        # 双源全部停运 -> 版本 3、4
        for v in (2, 3):
            r = self.client.post(
                "/api/sources/switch",
                json={
                    "switches": [{"source_id": "S2" if v == 2 else "S1",
                                  "running": False}],
                    "version": v,
                },
            )
            self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(STORE.version, 4)

        # P4 仍关闭：恢复 S1 后 D 端仍被已关阀门阻断 -> 安全，仅 U1 恢复
        r = self.client.post(
            "/api/sources/switch/preview",
            json={"switches": [{"source_id": "S1", "running": True}], "version": 4},
        )
        body = r.json()
        self.assertTrue(body["safe"], body)
        self.assertEqual(body["restored_sink_ids"], ["U1"])
        self.assertEqual(body["block_reasons"], [])

        # 复供事件（重开 P4）：此时无运行水源，P6 两端均不可达 -> 安全，版本 5
        r = self.client.post(
            "/api/isolation/events/1/restore", json={"version": 4}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(STORE.pipes["P4"].valve_closed)

        # 场景 B：待修复故障 + 隔离阀开启（无阀可关的空方案隔离）。
        # 无运行水源时 P6 两端均不可达，空方案即可隔离 -> 新 open 事件，版本 6
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": [], "version": 5},
        )
        self.assertEqual(r.status_code, 200, r.text)

        # 恢复 S1：P4 开启，待修复 P6 的 D 端重新进水 -> 风险，预演 safe=false
        r = self.client.post(
            "/api/sources/switch/preview",
            json={"switches": [{"source_id": "S1", "running": True}], "version": 6},
        )
        body = r.json()
        self.assertFalse(body["safe"])
        self.assertEqual(len(body["block_reasons"]), 1)
        block = body["block_reasons"][0]
        self.assertEqual(block["code"], "FAULT_REWATER_RISK")
        self.assertEqual(block["pipe_id"], "P6")
        self.assertEqual(block["reachable_endpoints"], ["D"])

        # 确认 -> 422 SOURCE_REWATER_RISK，无任何部分写入
        r = self.client.post(
            "/api/sources/switch",
            json={"switches": [{"source_id": "S1", "running": True}], "version": 6},
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "SOURCE_REWATER_RISK")
        self.assertIn("FAULT_REWATER_RISK", r.text)
        self.assertEqual(STORE.version, 6)
        self.assertFalse(STORE.nodes["S1"].running)

        # 先复供 open 事件（无运行水源，安全，版本 7），再恢复 S1 即安全
        r = self.client.post(
            "/api/isolation/events/2/restore", json={"version": 6}
        )
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post(
            "/api/sources/switch",
            json={"switches": [{"source_id": "S1", "running": True}], "version": 7},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["changed"])
        self.assertEqual(body["version"], 8)
        self.assertEqual(body["restored_sink_ids"], ["U1", "U3"])
        self.assertEqual(body["running_source_ids"], ["S1"])

    def test_source_switch_confirm_rejects_invalid_ids_and_duplicates(self):
        self._import_sources()
        # 非水源节点
        r = self.client.post(
            "/api/sources/switch",
            json={"switches": [{"source_id": "A", "running": True}], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "SOURCE_SWITCH_INVALID")
        self.assertIn("SOURCE_NOT_FOUND", r.text)
        # 不存在
        r = self.client.post(
            "/api/sources/switch",
            json={"switches": [{"source_id": "SX", "running": False}], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("SOURCE_NOT_FOUND", r.text)
        # 同批重复
        r = self.client.post(
            "/api/sources/switch",
            json={
                "switches": [
                    {"source_id": "S1", "running": False},
                    {"source_id": "S1", "running": True},
                ],
                "version": 1,
            },
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("SOURCE_SWITCH_DUPLICATE", r.text)
        # 不部分写入
        self.assertEqual(STORE.version, 1)
        self.assertTrue(STORE.nodes["S1"].running)

    def test_source_switch_preview_reports_structural_issues(self):
        self._import_sources()
        # 预演对结构问题不拒绝，返回 200 + issues（与隔离预演一致）
        r = self.client.post(
            "/api/sources/switch/preview",
            json={"switches": [{"source_id": "U1", "running": False}], "version": 1},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["issues"][0]["code"], "SOURCE_NOT_FOUND")
        self.assertFalse(body["safe"])
        self.assertFalse(body["changed"])

    def test_source_switch_version_conflict_and_stale_preview_flag(self):
        self._import_sources()
        # 过期版本确认 -> 409，不写入
        r = self.client.post(
            "/api/sources/switch",
            json={"switches": [{"source_id": "S2", "running": False}], "version": 99},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "VERSION_CONFLICT")
        self.assertEqual(STORE.version, 1)
        self.assertTrue(STORE.nodes["S2"].running)
        # 预演不拒绝过期版本，只标记 version_match=false
        r = self.client.post(
            "/api/sources/switch/preview",
            json={"switches": [{"source_id": "S2", "running": False}], "version": 99},
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["version_match"])
        self.assertEqual(r.json()["version"], 1)

    def test_source_switch_request_body_validation(self):
        self._import_sources()
        # switches 不能为空
        r = self.client.post(
            "/api/sources/switch", json={"switches": [], "version": 1}
        )
        self.assertEqual(r.status_code, 422)
        # version 不能为负
        r = self.client.post(
            "/api/sources/switch",
            json={"switches": [{"source_id": "S1", "running": False}], "version": -1},
        )
        self.assertEqual(r.status_code, 422)
        # 缺字段
        r = self.client.post(
            "/api/sources/switch", json={"version": 1}
        )
        self.assertEqual(r.status_code, 422)

    def test_isolation_and_recommend_respect_stopped_sources(self):
        self._import_sources()
        # 双源运行时 P6 关 P4 可隔离（仅 U3 失供）
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertTrue(r.json()["isolatable"])
        # 停运 S2 后，既有计算仍按运行水源 S1：结果不变
        self.client.post(
            "/api/sources/switch",
            json={"switches": [{"source_id": "S2", "running": False}], "version": 1},
        )
        r = self.client.post(
            "/api/isolation/recommend",
            json={"fault_pipe_id": "P6", "version": 2},
        )
        body = r.json()
        self.assertTrue(body["feasible"])
        self.assertEqual(body["recommended_close_pipe_ids"], ["P4"])
        self.assertEqual(body["affected_sink_ids"], ["U3"])
        # 再停运 S1：全网无运行水源，所有用水点方案前已失供；
        # P6 两端本就不可达 -> 空方案可隔离，新增失供为空
        self.client.post(
            "/api/sources/switch",
            json={"switches": [{"source_id": "S1", "running": False}], "version": 2},
        )
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": [], "version": 3},
        )
        body = r.json()
        self.assertTrue(body["isolatable"])
        self.assertEqual(body["affected_sink_ids"], [])

    def test_import_resets_sources_to_running(self):
        self._import_sources()
        self.client.post(
            "/api/sources/switch",
            json={"switches": [{"source_id": "S1", "running": False}], "version": 1},
        )
        self.assertFalse(STORE.nodes["S1"].running)
        # 整网重新导入：水源初始视为运行
        self.client.post("/api/network/import", json=SOURCE_NETWORK)
        self.assertTrue(STORE.nodes["S1"].running)
        self.assertTrue(STORE.nodes["S2"].running)
        net = self.client.get("/api/network").json()
        self.assertTrue(all(n["running"] for n in net["nodes"]))


if __name__ == "__main__":
    unittest.main()
