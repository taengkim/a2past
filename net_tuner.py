"""
게임용 네트워크 레지스트리 튜닝 도구 (Net Tuner)

표준 라이브러리(tkinter, winreg, ctypes, subprocess)만 사용.
관리자 권한으로 HKLM 레지스트리를 ON/OFF 하고 어댑터를 재시작한다.

빌드: pyinstaller --onefile --windowed --uac-admin net_tuner.py
"""

import os
import sys
import json
import queue
import ctypes
import winreg
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime

APP_NAME = "NetTuner"

# ---------------------------------------------------------------------------
# 레지스트리 경로 / 튜닝 대상 정의
# ---------------------------------------------------------------------------
TCPIP_INTERFACES = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
MULTIMEDIA = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"

# 어댑터별(인터페이스 경로) 튜닝값
ADAPTER_VALUES = {
    "TcpAckFrequency": 1,
    "TCPNoDelay": 1,
}
# 시스템 전역 튜닝값
GLOBAL_VALUES = {
    "NetworkThrottlingIndex": 0xFFFFFFFF,
}
# OFF(기본) 시 "값 없음"이 정상인 항목 외에, 알려진 기본값으로 강제 초기화할 때 쓰는 값
DEFAULT_RESET = {
    "NetworkThrottlingIndex": 10,  # Windows 기본값
}

CREATE_NO_WINDOW = 0x08000000


# ---------------------------------------------------------------------------
# 관리자 권한 / UAC
# ---------------------------------------------------------------------------
def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate_and_exit():
    """관리자 권한으로 재실행 요청. 거부 시 정상 종료."""
    params = " ".join(f'"{a}"' for a in sys.argv)
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, None, 1
    )
    # ShellExecuteW 반환값이 32 이하이면 실패(사용자가 UAC 거부 등)
    if rc <= 32:
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                "관리자 권한이 필요합니다. 권한 없이는 실행할 수 없어 종료합니다.",
                APP_NAME,
                0x10,  # MB_ICONERROR
            )
        except Exception:
            pass
    sys.exit(0)


# ---------------------------------------------------------------------------
# 레지스트리 헬퍼
# ---------------------------------------------------------------------------
def read_dword(subkey, name):
    """존재하면 정수값, 없으면 None."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey, 0, winreg.KEY_QUERY_VALUE) as k:
            val, typ = winreg.QueryValueEx(k, name)
            return int(val)
    except FileNotFoundError:
        return None


def write_dword(subkey, name, value):
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, value & 0xFFFFFFFF)


def delete_value(subkey, name):
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# 백업 관리 (%APPDATA%\NetTuner\backup_{guid}.json)
# ---------------------------------------------------------------------------
def backup_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def backup_path(guid):
    safe = guid.strip("{}").replace("-", "")
    return os.path.join(backup_dir(), f"backup_{safe}.json")


def save_backup(adapter, snapshot):
    data = {
        "adapter": adapter,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "values": snapshot,
    }
    with open(backup_path(adapter["guid"]), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_backup(guid):
    p = backup_path(guid)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return "corrupt"


def delete_backup(guid):
    p = backup_path(guid)
    if os.path.exists(p):
        os.remove(p)


# ---------------------------------------------------------------------------
# 어댑터 열거 / 재시작 (PowerShell 경유, 외부 라이브러리 없음)
# ---------------------------------------------------------------------------
def _run_ps(command):
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
    )


def list_adapters():
    """[{name, desc, status, guid, mac}, ...]"""
    cmd = (
        "@(Get-NetAdapter | Select-Object "
        "Name,InterfaceDescription,Status,InterfaceGuid,MacAddress) "
        "| ConvertTo-Json -Depth 3"
    )
    res = _run_ps(cmd)
    out = res.stdout.strip()
    if not out:
        return []
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, dict):
        parsed = [parsed]
    adapters = []
    for a in parsed:
        guid = (a.get("InterfaceGuid") or "").strip()
        if not guid:
            continue
        adapters.append({
            "name": a.get("Name") or "",
            "desc": a.get("InterfaceDescription") or "",
            "status": a.get("Status") or "",
            "guid": guid,
            "mac": a.get("MacAddress") or "",
        })
    return adapters


def restart_adapter(name):
    safe = name.replace("'", "''")
    res = _run_ps(f"Restart-NetAdapter -Name '{safe}' -Confirm:$false")
    return res.returncode == 0, (res.stderr or "").strip()


def enable_adapter(name):
    safe = name.replace("'", "''")
    res = _run_ps(f"Enable-NetAdapter -Name '{safe}' -Confirm:$false")
    return res.returncode == 0


# ---------------------------------------------------------------------------
# 상태 판정
# ---------------------------------------------------------------------------
def adapter_iface_path(guid):
    return rf"{TCPIP_INTERFACES}\{guid}"


def read_current(guid):
    """현재 레지스트리 값 스냅샷 (present/data)."""
    snap = {}
    iface = adapter_iface_path(guid)
    for name in ADAPTER_VALUES:
        v = read_dword(iface, name)
        snap[name] = {"present": v is not None, "data": v}
    for name in GLOBAL_VALUES:
        v = read_dword(MULTIMEDIA, name)
        snap[name] = {"present": v is not None, "data": v}
    return snap


def compute_state(guid):
    """'ON' | 'OFF' | 'MIXED' 와 항목별 적용여부 dict."""
    snap = read_current(guid)
    targets = {**ADAPTER_VALUES, **GLOBAL_VALUES}
    tuned = {}
    for name, target in targets.items():
        cur = snap[name]
        tuned[name] = cur["present"] and cur["data"] == target
    count = sum(tuned.values())
    if count == len(targets):
        state = "ON"
    elif count == 0:
        state = "OFF"
    else:
        state = "MIXED"
    return state, tuned, snap


# ---------------------------------------------------------------------------
# ON / OFF 핵심 로직
# ---------------------------------------------------------------------------
def apply_on(adapter, log):
    """현재값 백업 → 튜닝값 적용. 실패 시 예외."""
    guid = adapter["guid"]
    iface = adapter_iface_path(guid)

    snapshot = read_current(guid)
    save_backup(adapter, snapshot)
    log(f"백업 저장: {backup_path(guid)}")

    applied = []  # 롤백용 (subkey, name)
    try:
        for name, target in ADAPTER_VALUES.items():
            write_dword(iface, name, target)
            applied.append((iface, name))
            log(f"적용: {name} = {target}")
        for name, target in GLOBAL_VALUES.items():
            write_dword(MULTIMEDIA, name, target)
            applied.append((MULTIMEDIA, name))
            log(f"적용: {name} = 0x{target:08X}")
    except Exception as e:
        log(f"[오류] 적용 실패: {e} — 백업으로 롤백합니다.")
        restore_from_snapshot(snapshot, guid, log)
        delete_backup(guid)
        raise


def apply_off(adapter, log):
    """백업 기준 원상복구 → 백업 삭제. 백업 없거나 손상 시 예외."""
    guid = adapter["guid"]
    backup = load_backup(guid)
    if backup is None:
        raise RuntimeError("백업이 없어 OFF를 수행할 수 없습니다.")
    if backup == "corrupt":
        raise RuntimeError(
            "백업 파일이 손상되었습니다. '기본값으로 강제 초기화'를 사용하세요."
        )
    restore_from_snapshot(backup["values"], guid, log)
    delete_backup(guid)
    log("백업 삭제 완료.")


def restore_from_snapshot(snapshot, guid, log):
    """스냅샷대로 복구: 있던 값은 원래값, 없던 값은 삭제."""
    iface = adapter_iface_path(guid)
    for name in ADAPTER_VALUES:
        _restore_one(iface, name, snapshot.get(name), log)
    for name in GLOBAL_VALUES:
        _restore_one(MULTIMEDIA, name, snapshot.get(name), log)


def _restore_one(subkey, name, entry, log):
    if not entry or not entry.get("present"):
        delete_value(subkey, name)
        log(f"복구: {name} 삭제 (원래 없음)")
    else:
        write_dword(subkey, name, int(entry["data"]))
        log(f"복구: {name} = {entry['data']}")


def force_reset(adapter, log):
    """백업 없이도 알려진 기본값으로 강제 초기화."""
    guid = adapter["guid"]
    iface = adapter_iface_path(guid)
    for name in ADAPTER_VALUES:
        delete_value(iface, name)
        log(f"초기화: {name} 삭제")
    for name, default in DEFAULT_RESET.items():
        write_dword(MULTIMEDIA, name, default)
        log(f"초기화: {name} = {default}")
    delete_backup(guid)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} — 네트워크 레지스트리 튜닝")
        self.geometry("640x560")
        self.resizable(False, True)

        self.adapters = []
        self.log_queue = queue.Queue()
        self.busy = False

        self._build_ui()
        self._poll_log()
        self.refresh_adapters()

    # --- UI 구성 ---
    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="네트워크 어댑터:").pack(side="left")
        self.cb = ttk.Combobox(top, state="readonly", width=50)
        self.cb.pack(side="left", padx=6)
        self.cb.bind("<<ComboboxSelected>>", lambda e: self.update_status())
        ttk.Button(top, text="새로고침", command=self.refresh_adapters).pack(side="left")

        self.reboot_mode = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self, text="어댑터 재시작 대신 재부팅 안내 (네트워크 끊김 회피)",
            variable=self.reboot_mode,
        ).pack(anchor="w", **pad)

        status = ttk.LabelFrame(self, text="현재 상태")
        status.pack(fill="x", **pad)
        self.status_var = tk.StringVar(value="-")
        ttk.Label(status, textvariable=self.status_var, font=("", 11, "bold")).pack(
            anchor="w", padx=8, pady=4
        )
        self.detail_var = tk.StringVar(value="")
        ttk.Label(status, textvariable=self.detail_var, justify="left",
                  font=("Consolas", 9)).pack(anchor="w", padx=8, pady=(0, 6))

        btns = ttk.Frame(self)
        btns.pack(fill="x", **pad)
        self.on_btn = ttk.Button(btns, text="ON (튜닝 적용)", command=self.do_on)
        self.on_btn.pack(side="left", expand=True, fill="x", padx=4)
        self.off_btn = ttk.Button(btns, text="OFF (원상복구)", command=self.do_off)
        self.off_btn.pack(side="left", expand=True, fill="x", padx=4)
        self.reset_btn = ttk.Button(btns, text="기본값 강제 초기화", command=self.do_reset)
        self.reset_btn.pack(side="left", expand=True, fill="x", padx=4)

        logf = ttk.LabelFrame(self, text="로그")
        logf.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(logf, height=12, state="disabled", wrap="word",
                                font=("Consolas", 9))
        self.log_text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        sb.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=sb.set)

    # --- 로그 ---
    def log(self, msg):
        self.log_queue.put(msg)

    def _poll_log(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                ts = datetime.now().strftime("%H:%M:%S")
                line = f"[{ts}] {msg}\n"
                self.log_text.config(state="normal")
                self.log_text.insert("end", line)
                self.log_text.see("end")
                self.log_text.config(state="disabled")
                self._write_logfile(line)
        except queue.Empty:
            pass
        self.after(150, self._poll_log)

    def _write_logfile(self, line):
        try:
            with open(os.path.join(backup_dir(), "nettuner.log"), "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    # --- 어댑터 ---
    def refresh_adapters(self):
        self.log("어댑터 목록 조회 중...")

        def worker():
            adapters = list_adapters()
            self.after(0, lambda: self._set_adapters(adapters))

        threading.Thread(target=worker, daemon=True).start()

    def _set_adapters(self, adapters):
        self.adapters = adapters
        labels = [f"{a['name']} | {a['desc']} | {a['status']}" for a in adapters]
        self.cb["values"] = labels
        if labels:
            self.cb.current(0)
            self.log(f"어댑터 {len(labels)}개 감지.")
            self.update_status()
        else:
            self.log("[경고] 어댑터를 찾지 못했습니다.")
            self.status_var.set("어댑터 없음")

    def current_adapter(self):
        idx = self.cb.current()
        if idx < 0 or idx >= len(self.adapters):
            return None
        return self.adapters[idx]

    def update_status(self):
        a = self.current_adapter()
        if not a:
            return
        state, tuned, snap = compute_state(a["guid"])
        label = {"ON": "ON (튜닝 적용됨)", "OFF": "OFF (기본 상태)",
                 "MIXED": "불일치 (일부만 적용됨)"}[state]
        self.status_var.set(f"상태: {label}")

        lines = []
        for name in list(ADAPTER_VALUES) + list(GLOBAL_VALUES):
            cur = snap[name]
            mark = "O" if tuned[name] else "X"
            data = "없음" if not cur["present"] else (
                f"0x{cur['data']:08X}" if name == "NetworkThrottlingIndex" else cur["data"]
            )
            lines.append(f"  [{mark}] {name} = {data}")
        backup = load_backup(a["guid"])
        bstat = "있음" if isinstance(backup, dict) else ("손상" if backup == "corrupt" else "없음")
        lines.append(f"  백업: {bstat}")
        self.detail_var.set("\n".join(lines))

        self._update_buttons(state, backup)

    def _update_buttons(self, state, backup):
        if self.busy:
            for b in (self.on_btn, self.off_btn, self.reset_btn):
                b.state(["disabled"])
            return
        # ON: 이미 ON이면 비활성
        self.on_btn.state(["!disabled"] if state != "ON" else ["disabled"])
        # OFF: 백업 있어야 활성
        self.off_btn.state(["!disabled"] if isinstance(backup, dict) else ["disabled"])
        self.reset_btn.state(["!disabled"])

    # --- 동작 핸들러 ---
    def _run_task(self, fn, restart, success_msg):
        a = self.current_adapter()
        if not a:
            return
        self.busy = True
        self._update_buttons(None, None)

        def worker():
            try:
                fn(a, self.log)
                if restart:
                    if self.reboot_mode.get():
                        self.log("재부팅 후 적용됩니다. 수동으로 재부팅하세요.")
                    else:
                        self.log(f"어댑터 재시작: {a['name']}")
                        ok, err = restart_adapter(a["name"])
                        if not ok:
                            self.log(f"[오류] 어댑터 재시작 실패: {err}")
                            self.log("어댑터 재활성 시도...")
                            enable_adapter(a["name"])
                self.log(success_msg)
            except Exception as e:
                self.log(f"[실패] {e}")
                self.after(0, lambda: messagebox.showerror(APP_NAME, str(e)))
            finally:
                self.busy = False
                self.after(0, self.update_status)

        threading.Thread(target=worker, daemon=True).start()

    def do_on(self):
        a = self.current_adapter()
        if not a:
            return
        state, _, _ = compute_state(a["guid"])
        if state == "ON":
            messagebox.showinfo(APP_NAME, "이미 ON 상태입니다. (중복 적용 차단)")
            return
        self._run_task(apply_on, restart=True, success_msg="ON 완료.")

    def do_off(self):
        a = self.current_adapter()
        if not a:
            return
        if not isinstance(load_backup(a["guid"]), dict):
            messagebox.showwarning(APP_NAME, "백업이 없거나 손상되어 OFF를 할 수 없습니다.")
            return
        self._run_task(apply_off, restart=True, success_msg="OFF 완료.")

    def do_reset(self):
        a = self.current_adapter()
        if not a:
            return
        if not messagebox.askyesno(
            APP_NAME,
            "백업과 무관하게 알려진 기본값으로 강제 초기화합니다.\n계속할까요?",
        ):
            return
        self._run_task(force_reset, restart=True, success_msg="강제 초기화 완료.")


def main():
    if not is_admin():
        elevate_and_exit()
        return
    App().mainloop()


if __name__ == "__main__":
    main()
