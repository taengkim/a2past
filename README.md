# Net Tuner

게임용 네트워크 레지스트리 튜닝값을 GUI로 ON/OFF 하는 도구.
표준 라이브러리(tkinter, winreg, ctypes, subprocess)만 사용한다.

## 기능

- 네트워크 어댑터 자동 감지 + 드롭다운 선택
- ON: 현재값 백업 → 튜닝값 적용 → 어댑터 재시작 (이미 ON이면 거부)
- OFF: 백업 기준 원상복구(없던 값은 삭제) → 백업 삭제 → 어댑터 재시작 (백업 없으면 거부)
- 상태 표시: ON / OFF / 불일치, 항목별 현재값 표시
- 기본값 강제 초기화 (백업 손상/분실 시 비상 복구)
- 어댑터 재시작 실패 시 백업 롤백 + 어댑터 재활성
- 재시작 대신 재부팅 안내 옵션
- 로그창 + 로그 파일 저장

## 튜닝 대상

| 값 | 적용값 | 경로 | 효과 |
| --- | --- | --- | --- |
| `TcpAckFrequency` | `1` | 어댑터 인터페이스 | 지연 ACK 비활성 |
| `TCPNoDelay` | `1` | 어댑터 인터페이스 | Nagle 비활성 |
| `NetworkThrottlingIndex` | `0xFFFFFFFF` | Multimedia SystemProfile | 네트워크 스로틀링 해제 |
| `SystemResponsiveness` | `0` | Multimedia SystemProfile | 게임에 더 많은 처리 할당 |
| `AllowGameDVR` | `0` | Policies\GameDVR | Xbox 게임 녹화 비활성 |
| `Win32PrioritySeparation` | `0x26` | PriorityControl | 포그라운드 우선순위 부스트 |

> 전역 값(스로틀링/응답성/GameDVR/우선순위)은 게임 재실행 또는 재부팅 후 반영된다.

자세한 설명은 [요구사항.md](요구사항.md) 참고.

## 실행

```cmd
python net_tuner.py
```

관리자 권한이 없으면 UAC로 자동 재요청한다.

## 빌드

```cmd
pip install pyinstaller
build.bat
```

`dist\NetTuner.exe` 생성. `--onefile` exe는 백신 오탐이 발생할 수 있다.

## 주의

- 어댑터 재시작 시 수 초간 네트워크가 끊긴다.
- 효과는 로컬 처리 지연/지터에 한정되며 물리적 RTT(거리·ISP·서버 위치)는 줄지 않는다.
- 백업은 `%APPDATA%\NetTuner\` 에 어댑터별로 저장된다.
