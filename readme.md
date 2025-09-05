### `README.md`

````markdown
# VM2030 ZeroMQ Tester

Client Python đơn giản để kiểm thử `server.py` (VM2030 + PLC + Board Relay) qua **ZeroMQ**.

## Yêu cầu

- Python 3.9+
- `pip install pyzmq`

## Chạy server

1) Cấu hình cổng trong `device_config.json` hoặc để `dry_run=true` (mặc định) để test không cần phần cứng.  
2) Chạy server:

```bash
python server.py
````

* REP (RPC) mặc định: `tcp://*:5555`
* PUB (realtime): `tcp://*:5556`
* Topic xuất bản:

  * `read_response`: raw PLC registers (BOARD\_RELAY)
  * `soft_state`: summary Ready/Home/Reset (0/1)
  * `op_result`: kết quả các lệnh tới VM2030 (ok/timeout)

> Bạn có thể đổi endpoints trong `device_config.json` (`zeromq.rep_bind`, `zeromq.pub_bind`).

## Chạy tester

```bash
python tester.py --connect tcp://127.0.0.1:5555 --sub tcp://127.0.0.1:5556 <command> [args...]
```

Ví dụ:

```bash
# subscribe mặc định (read_response, soft_state, op_result) + gửi lệnh kiểm tra READY
python tester.py ready

# đọc vị trí X/Y từ relay (tuỳ config app.position)
python tester.py pos

# get job số 15
python tester.py job-get 15

# set job số 15 (CharacterString="J 15", các thông số cơ bản)
python tester.py job-set 15 "J 15" --size 2.0 --dir 0 --speed 2400 --sx 0 --sy 0 --px 0 --py 0 --font FontB

# start job 15
python tester.py job-start 15

# builtin home/reset
python tester.py builtin home
python tester.py builtin reset

# set sequence index=1 (chuỗi lệnh của VM2030 do bạn định nghĩa)
python tester.py seq-set 1 "%A%B%C"    # ví dụ
python tester.py seq-start 1

# điều chỉnh log level ở server
python tester.py log debug --show-prompt=true
```

Tắt SUB nếu không cần:

```bash
python tester.py --no-sub ready
```

Đổi topic SUB:

```bash
python tester.py --topics soft_state op_result job-get 15
```

Timeout cho REQ/REP (ms, mặc định 15000):

```bash
python tester.py --timeout 20000 job-start 15
```

## Shell tương tác

```bash
python tester.py shell
```

Trong shell:

```
ready
pos
job.get 15
job.set 15 "J 15" size=2 dir=0 speed=2400 sx=0 sy=0 px=0 py=0 font=FontB
job.start 15
seq.set 1 "%A%B%C"
seq.start 1
builtin home
log debug showPrompt=true
quit
```

## Ghi chú

* `server.py` **dry\_run=true** sẽ in payload và tự mô phỏng **complete (0x1F)** sau `dry_run_complete_ms`.
* Với phần cứng thật: đặt `dry_run=false` và cấu hình `devices.SOFTWARE_COMMAND.com_port`.
* Mọi lệnh REQ phải **chờ** RECV (pattern REQ/REP). `tester.py` đảm bảo tuần tự: gửi → nhận.
* Realtime (SUB) in ra sự kiện theo các topic, hữu ích để theo dõi thay đổi **input**/edge và **kết quả lệnh**.

```

---

Nếu cần mình thêm preset “macro” các lệnh trong tester (ví dụ `demo` chạy cả chuỗi ready → set → get → start) thì đã có luôn: `python tester.py demo --num 15 --text "J 15" ...`.
```
