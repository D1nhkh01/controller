# -*- coding: utf-8 -*-
import argparse
import json
import time
import uuid
import threading
from datetime import datetime, timezone
import zmq

def iso_now():
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def jprint(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))

class Bus:
    def __init__(self, rep, pub, timeout_ms, target):
        self.ctx = zmq.Context.instance()
        self.timeout_ms = timeout_ms
        self.target = target
        self.stop = False

        # REQ for sending commands
        self.req = self.ctx.socket(zmq.REQ)
        self.req.connect(rep)
        self.req_poller = zmq.Poller()
        self.req_poller.register(self.req, zmq.POLLIN)

        # SUB for receiving events
        self.sub = self.ctx.socket(zmq.SUB)
        # Subscribe BEFORE connect to avoid slow-joiner drops
        for topic in (b"op_result", b"soft_state", b"read_response"):
            self.sub.setsockopt(zmq.SUBSCRIBE, topic)
        self.sub.connect(pub)
        time.sleep(0.5)  # small handshake delay for SUB
        self.sub_poller = zmq.Poller()
        self.sub_poller.register(self.sub, zmq.POLLIN)

        print(f"[INIT] REQ → {rep}")
        print(f"[INIT] SUB ← {pub} (topics: op_result, soft_state, read_response)")

    def close(self):
        try:
            self.req.close(0)
        except:
            pass
        try:
            self.sub.close(0)
        except:
            pass

    def send(self, command, payload=None, timeout_ms=None):
        """Send one REQ message and wait for REP within timeout."""
        mid = str(uuid.uuid4())
        msg = {
            "messageId": mid,
            "timestamp": iso_now(),
            "targetDevice": self.target,
            "command": str(command),
            "payload": payload or {}
        }
        out = json.dumps(msg, ensure_ascii=False)
        print(f"\n[REQ →] {out}")
        self.req.send_string(out)

        tout = timeout_ms if timeout_ms is not None else self.timeout_ms
        socks = dict(self.req_poller.poll(tout))
        if socks.get(self.req) != zmq.POLLIN:
            raise TimeoutError(f"Timeout waiting REP for '{command}' ({tout} ms)")

        rep = self.req.recv_string()
        print(f"[REP ←] {rep}")
        try:
            return json.loads(rep)
        except:
            return {"raw": rep}

    def sub_loop(self):
        print("[SUB] listening topics: op_result, soft_state, read_response ...")
        while not self.stop:
            try:
                socks = dict(self.sub_poller.poll(250))
                if socks.get(self.sub) == zmq.POLLIN:
                    topic, payload = self.sub.recv_multipart()
                    t = topic.decode("utf-8", "ignore")
                    try:
                        data = json.loads(payload.decode("utf-8", "ignore"))
                    except:
                        data = payload.decode("latin1", "ignore")
                    print(f"\n[SUB:{t}]")
                    jprint(data)
            except KeyboardInterrupt:
                break
            except Exception:
                # tránh spam khi đóng nhanh
                pass

    def wait_op_result(self, op_id, timeout_ms=None):
        """Optional: wait for specific op_result with given opId."""
        tout = timeout_ms if timeout_ms is not None else self.timeout_ms
        deadline = time.time() + (tout / 1000.0)
        while time.time() < deadline:
            rest = int(max(0, (deadline - time.time()) * 1000))
            socks = dict(self.sub_poller.poll(rest))
            if socks.get(self.sub) == zmq.POLLIN:
                topic, payload = self.sub.recv_multipart()
                if topic != b"op_result":
                    continue
                try:
                    data = json.loads(payload.decode("utf-8", "ignore"))
                except:
                    continue
                if data.get("opId") == op_id:
                    print("\n[SUB:op_result match]")
                    jprint(data)
                    return data
        raise TimeoutError(f"op_result for {op_id} not received within {tout} ms")

def menu():
    print("\n================= MANUAL TESTER =================")
    print("1) BUILTIN: Home")
    print("2) BUILTIN: Reset")
    print("3) SET_JOB")
    print("4) GET_JOB")
    print("5) START_JOB")
    print("6) SET_SEQUENCE")
    print("7) GET_SEQUENCE")
    print("8) START_SEQUENCE")
    print("9) GET_READY_STATUS")
    print("10) GET_POSITION")
    print("11) SET_LOG_LEVEL")
    print("12) RAW JSON (nhập toàn bộ gói)")
    print("13) WAIT op_result theo opId")
    print("0) Thoát")
    return input("Chọn: ").strip()

def ask_int(prompt, default=None):
    s = input(f"{prompt} [{'' if default is None else default}]: ").strip()
    if s == "" and default is not None:
        return int(default)
    return int(s)

def ask_str(prompt, default=""):
    s = input(f"{prompt} [{default}]: ").strip()
    return s if s != "" else default

def main():
    ap = argparse.ArgumentParser(description="Manual ZMQ tester (REQ+SUB)")
    ap.add_argument("--rep", default="tcp://127.0.0.1:5555", help="REP endpoint of server")
    ap.add_argument("--pub", default="tcp://127.0.0.1:5556", help="PUB endpoint of server")
    ap.add_argument("--timeout", type=int, default=20000, help="REQ and wait_op_result timeout (ms)")
    ap.add_argument("--target", default="localhost", help="targetDevice field")
    args = ap.parse_args()

    bus = Bus(args.rep, args.pub, args.timeout, args.target)

    # Start SUB thread first, give it time to "join"
    sub_thr = threading.Thread(target=bus.sub_loop, daemon=True)
    sub_thr.start()
    time.sleep(0.5)

    print("\nMẹo: Chờ 0.5–1s sau khi mở tester rồi hãy gửi lệnh đầu tiên để tránh miss publish đầu tiên.\n")

    try:
        while True:
            choice = menu()
            if choice == "0":
                break

            try:
                if choice == "1":
                    bus.send("BUILTIN_COMMAND", {"state": "rt_home"})
                elif choice == "2":
                    bus.send("BUILTIN_COMMAND", {"state": "sw_reset"})
                elif choice == "3":
                    idx = ask_int("JobNumber", 1)
                    text = ask_str("CharacterString", "TEST123")
                    # Theo thiết kế server: trả ACK ngay, kết quả sẽ đến topic 'op_result'
                    bus.send("SET_JOB", {"JobNumber": idx, "CharacterString": text, "waitComplete": True})
                elif choice == "4":
                    idx = ask_int("index", 1)
                    bus.send("GET_JOB", {"index": idx})
                elif choice == "5":
                    idx = ask_int("index", 1)
                    bus.send("START_JOB", {"index": idx})
                elif choice == "6":
                    idx = ask_int("index", 1)
                    cmdstr = ask_str("commandString", "H/P1/M3/C2/J15/H")
                    bus.send("SET_SEQUENCE", {"index": idx, "commandString": cmdstr, "waitComplete": True})
                elif choice == "7":
                    idx = ask_int("index", 1)
                    bus.send("GET_SEQUENCE", {"index": idx})
                elif choice == "8":
                    idx = ask_int("index", 1)
                    bus.send("START_SEQUENCE", {"index": idx})
                elif choice == "9":
                    bus.send("GET_READY_STATUS", {})
                elif choice == "10":
                    bus.send("GET_POSITION", {})
                elif choice == "11":
                    level = ask_str("level (off|error|warn|info|debug)", "info")
                    show_prompt = ask_str("showPrompt (true/false)", "false").lower() in ("1","true","yes","y")
                    bus.send("SET_LOG_LEVEL", {"level": level, "showPrompt": show_prompt})
                elif choice == "12":
                    raw = input("Nhập JSON full gói REQ: ").strip()
                    try:
                        obj = json.loads(raw)
                    except Exception as e:
                        print(f"JSON lỗi: {e}")
                        continue
                    if "messageId" not in obj: obj["messageId"] = str(uuid.uuid4())
                    if "timestamp" not in obj: obj["timestamp"] = iso_now()
                    if "targetDevice" not in obj: obj["targetDevice"] = args.target
                    bus.send(obj.get("command",""), obj.get("payload",{}))
                elif choice == "13":
                    op_id = ask_str("Nhập opId muốn chờ")
                    try:
                        bus.wait_op_result(op_id, args.timeout)
                    except Exception as e:
                        print(f"[ERR] {e}")
                else:
                    print("Chọn không hợp lệ.")
            except Exception as e:
                print(f"[ERR] {e}")

            print("\n(Lưu ý: Kết quả hoàn tất/timeout sẽ hiện ở kênh SUB topic 'op_result')")

    except KeyboardInterrupt:
        pass
    finally:
        bus.stop = True
        time.sleep(0.2)
        bus.close()
        print("Bye.")

if __name__ == "__main__":
    main()
