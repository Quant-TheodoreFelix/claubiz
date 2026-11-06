# Windows 지원 작업 방향

- 브랜치: `release/windows-support` (main `c53f545`에서 분기)
- 작성일: 2026-11-05
- 목표 버전: 1.0.0 (첫 릴리스, Windows 지원 포함)
- 원칙: macOS/Linux와 같은 보안 수준을 Windows에서도 지킨다. 보안 검사를 빼서 실행만 되게 만드는 포팅은 하지 않는다.

## 진행 상황 (2026-11-06)

- 0~4단계와 로케일 지원을 구현했고, CI(Ubuntu, macOS, Windows x64, Windows ARM64)에서 테스트, install.sh, install.ps1 설치와 제거를 확인함
- 5단계(실제 Windows Terminal에서 한글 화면, 입력기, 단축키 수동 점검)는 남아 있음
- 10절에서 미리 확인하기로 한 것의 결과
  - `bin`으로 복사한 `claubiz.exe`는 고정 경로 가상 환경에서 그대로 실행됨
  - API 키는 `CRED_PERSIST_LOCAL_MACHINE`으로 저장됨 (CI에서 자격 증명 관리자 값으로 확인)
  - 정션과 심볼릭 링크는 경로 대체 비트로 거부되고, 바이트는 줄바꿈 변환 없이 왕복함
- 계획과 달라진 점
  - 관리자 권한(UAC 상승) 프로세스가 만든 객체는 소유자가 Administrators가 되므로, 새 파일과 폴더를 만들 때 소유자를 현재 사용자로 명시함. 상승된 프로세스에서만 Administrators 소유를 허용함
  - 폴더 삭제 중 링크를 만나면 멈추지 않고 링크 자체만 지움 (POSIX `shutil.rmtree`와 같은 동작, 링크 대상은 건드리지 않음)
  - `install.ps1`에 `-AllowElevated`를 추가함. GitHub의 Windows 실행 환경이 관리자 권한이라 CI 시험용으로만 씀
  - 화면 언어는 `--lang`, `CLAUBIZ_LANG`, 설정의 `ui.language`, 설정이 없을 때만 OS 화면 언어 순으로 정함. 기존 설정은 한국어로 유지됨

## 결정 요약

| 항목 | 결정 |
|---|---|
| 지원 대상 | Windows 11, Windows 10 22H2 (x64, ARM64) |
| 터미널 | Windows Terminal 권장. 예전 콘솔(conhost)은 동작만 확인함 |
| WSL | 지원 대상이 아님. WSL에서는 Linux 설치 스크립트 사용 |
| 구현 방식 | 기본 모듈 `ctypes`로 Windows 시스템 함수를 직접 호출 (pywin32 같은 의존성 추가 없음) |
| 구조 | OS별 코드를 `claubiz/hostos/` 한 곳에 모으고, 나머지 모듈은 `fcntl`, `os.getuid`, `O_NOFOLLOW`를 직접 쓰지 않음 |
| 데이터 폴더 | `%USERPROFILE%\.claubiz` (다른 OS와 같은 `~/.claubiz`) |
| API 키 | Windows 자격 증명 관리자, 로밍 금지(이 PC에만 저장) |
| 백그라운드 실행 | 작업 스케줄러 (`schtasks /Create /XML`) |
| 설치 | `install.ps1`, 기존 `release.py`가 같은 배포 묶음에 함께 생성 |
| 검증 | GitHub Actions Windows 작업 + 실제 Windows에서 TUI 수동 점검 |

## 1. 구조

`src/claubiz/hostos/`

- `__init__.py`: 공개 함수만 노출하고, 실행 중인 OS에 맞는 구현을 고른다.
- `posix.py`: 지금 코드를 옮긴다. 동작은 바꾸지 않는다.
- `windows.py`, `winapi.py`: ctypes 선언과 Windows 구현. Windows에서만 불러온다.

공개 함수

| 함수 | 역할 |
|---|---|
| `private_dir(path)` | 폴더를 만들고 소유자와 권한을 확인하고 바로잡음 |
| `open_fd(path, mode)` | 링크를 따라가지 않고, 바이너리 모드로, 새 파일은 소유자 전용으로 염 |
| `is_link_like(st)` | 심볼릭 링크, 정션, 그 밖의 경로 대체형 재분석 지점 판별 |
| `owned_by_me(path)` | 현재 사용자 소유인지 확인 |
| `lock(fd, blocking)`, `unlock(fd)` | 프로세스 간 배타 잠금 |
| `replace(src, dst)` | 원자적 교체, Windows에서는 잠깐 잠긴 파일에 대해 짧게 재시도 |
| `remove_tree(path)` | 링크를 따라가지 않는 폴더 삭제 |
| `open_with_default_app(path)` | 승인된 결과 파일을 기본 앱으로 열기 |
| `check_portable_name(name)` | 모든 OS에서 안전한 파일 이름인지 검사 |

바꿀 호출 위치: `paths.py`, `audit.py`, `digest.py`, `knowledge.py`, `secrets.py`, `workspace.py`, `actions.py`, `agent.py`, `budget.py`, `playbooks.py`, `tui/modals.py`

## 2. 보안 기능 대응표

| 현재 (POSIX) | Windows 구현 |
|---|---|
| `mkdir(0o700)` + `chmod` 복구 | SDDL `D:PAI(A;OICI;FA;;;<사용자 SID>)`로 상속을 끊은 소유자 전용 권한을 데이터 폴더에 설정. 하위 파일과 폴더는 이 권한을 물려받음. 매 실행 시 다른 계정 허용 항목이 있으면 다시 설정 (SYSTEM과 Administrators는 막을 수 없으므로 검사에서 허용) |
| 새 파일 `0o600` | 데이터 폴더 안은 상속으로 해결. 데이터 폴더 밖(보고서 복사, 결과 파일)은 `CreateFileW`에 소유자 전용 보안 설명자를 넣어 만드는 순간부터 막음 |
| `st_uid == getuid()` | `GetSecurityInfo`로 얻은 소유자 SID와 현재 토큰 사용자 SID 비교 |
| `O_NOFOLLOW` | `CreateFileW` + `FILE_FLAG_OPEN_REPARSE_POINT`로 열고 재분석 지점이면 거부. 그 뒤 `msvcrt.open_osfhandle`로 일반 fd로 바꿈 |
| `S_ISLNK`, `is_symlink()` | 정션은 `S_ISLNK`에 잡히지 않으므로 `st_reparse_tag`의 경로 대체 비트로 판별. OneDrive 클라우드 자리 표시자처럼 경로를 바꾸지 않는 재분석 지점은 허용 (OneDrive 문서 폴더에서 자료 가져오기 가능) |
| `fcntl.flock` | `LockFileEx`로 파일 끝 너머 고정 위치 1바이트를 잠금. 감사 로그를 잠근 채로도 `audit verify`가 읽을 수 있음. `msvcrt.locking`은 10초 뒤 포기하므로 쓰지 않음 |
| `os.open` 기본 모드 | Windows에서는 항상 `O_BINARY`를 붙임. 텍스트 모드면 줄바꿈 변환과 `0x1A` 조기 종료로 감사 로그 해시 체인과 바이너리 파일이 깨짐 |
| `shutil.rmtree` | Windows에서는 `avoids_symlink_attacks`가 False라 지금 작업 영역 삭제가 거부됨. `scandir` 기반으로 직접 지우고, 경로 대체형 재분석 지점을 만나면 지우지 않고 멈춤. 읽기 전용 속성은 풀고 지움 |
| 잠금 파일 삭제 (`digest-<id>.lock`) | Windows는 열린 파일을 지우면 `PermissionError`. 비차단 잠금을 먼저 잡아 보고, 잡히면 닫고 지우고, 못 잡으면 남겨 둠 (빈 잠금 파일은 무해) |
| `secure_unlink` | 같은 방식으로 덮어쓴 뒤 삭제. SSD와 NTFS에서 완전 소거가 보장되지 않는 점은 macOS와 같음 |

## 3. 데이터 폴더와 경로 규칙

- 기본 위치는 `%USERPROFILE%\.claubiz`. 문서와 CLI 안내가 OS와 상관없이 같고, AppData\Roaming과 달리 도메인 프로필로 로밍되지 않음.
- `CLAUBIZ_HOME`으로 지정할 때 거부하는 경우
  - UNC 경로(`\\server\share`), 장치 경로(`\\?\`, `\\.\`), 네트워크 드라이브 (`GetDriveTypeW`)
  - ACL을 지원하지 않는 볼륨 (FAT32, exFAT USB. `GetVolumeInformationW`의 `FILE_PERSISTENT_ACLS`)
  - OneDrive 동기화 폴더 안 (`%OneDrive%`, `%OneDriveCommercial%`). 업무 자료가 클라우드로 올라가므로 Air-Gapped 원칙 위반
- 파일 이름 검사 `check_portable_name`을 모든 OS에 똑같이 적용해서 작업 영역을 다른 OS로 옮겨도 깨지지 않게 함
  - 예약 장치 이름 거부: `CON`, `PRN`, `AUX`, `NUL`, `COM1`~`COM9`, `LPT1`~`LPT9` (확장자가 붙어도, 위첨자 숫자여도 거부)
  - `<>:"|?*`와 제어 문자 거부. `:`는 NTFS 대체 데이터 스트림이 되므로 반드시 막음
  - 끝의 점과 공백 거부 (Windows가 지워서 다른 파일과 이름이 겹침)
  - 대상: `FILE_RE`로 받는 작업 영역 자료 이름, `confine`, `export_file_name`, `_stem`
- 관리자 권한(UAC 상승)으로 실행한 설치는 거부함. 만들어진 파일 소유자가 Administrators 그룹이 되어 이후 일반 실행에서 소유자 검사가 실패함.

## 4. API 키 저장

- `WinVaultKeyring`은 이미 허용 목록에 있음. 다만 기본값이 `CRED_PERSIST_ENTERPRISE`라 도메인 환경에서 로밍 프로필을 따라 다른 PC로 복사됨. 저장 전에 `persist`를 `CRED_PERSIST_LOCAL_MACHINE`으로 바꿈.
- 위협 모델 차이를 문서에 밝힘. macOS 키체인은 앱별 접근 확인이 있지만 Windows 자격 증명 관리자는 같은 사용자로 도는 모든 프로세스가 묻지 않고 읽을 수 있음 (Linux Secret Service와 같은 수준). DPAPI로 따로 암호화해도 같은 사용자 프로세스는 풀 수 있어 이득이 없으므로 추가하지 않음.

## 5. 백그라운드 실행과 파일 열기

- 작업 스케줄러
  - `%SystemRoot%\System32\schtasks.exe`를 절대 경로로 실행하고, 명령줄 인용 문제를 피하려고 `/TR` 대신 XML로 등록함
  - 작업 이름은 `\claubiz\digest`. 로그인한 사용자 세션에서만 실행해서(`InteractiveToken`) 비밀번호를 저장하지 않음. 이미 돌고 있으면 새로 시작하지 않음(`IgnoreNew`)
  - 실행 파일은 콘솔 창이 뜨지 않게 `venv\Scripts\pythonw.exe -m claubiz --home <데이터 폴더> digest run-due`
  - 프로그램 제거 시 작업이 등록돼 있으면 거부하는 장치는 LaunchAgent와 똑같이 둠
  - `_require_macos` 안내 문구를 OS별로 고침
- 파일 열기: 해시 확인과 확장자 허용 목록(`eml`, `ics`, `csv`, `md`)은 그대로 두고 `os.startfile(path, "open")`으로 엶. `.md`에 연결된 앱이 없으면 알아들을 수 있는 오류를 보여 줌.

## 6. TUI

- 화면 라이브러리(Textual)는 Windows에서도 동작하므로 구조 변경은 없음
- 고칠 것
  - `tui/modals.py`의 경로 입력과 자동 완성이 `/`로만 나누므로 `\`와 드라이브 문자(`C:`)를 처리함
  - "~/ 또는 / 로 시작" 같은 안내 문구를 OS에 맞게 바꿈
- 실제 Windows Terminal에서 수동 확인할 것
  - 한글 넓은 문자 보정(`tui/compositor.py`)
  - 한글 입력기 조합 위치
  - Ctrl 조합 단축키 전달 여부
  - 백스페이스로 자료 제거

## 7. 설치 스크립트

- `scripts/install.ps1.in`을 만들고 `release.py`가 같은 placeholder로 렌더링해서 `install.sh` 옆에 둠. `SHA256SUMS`와 Cloudflare `_headers`에도 추가함.
- 한 줄 설치: `irm https://<배포 주소>/install.ps1 | iex`
- 먼저 확인하고 실행하는 방법: 파일을 받아 `Get-FileHash`로 공개된 SHA-256과 비교한 뒤 `& ([scriptblock]::Create((Get-Content -Raw -Encoding UTF8 .\install.ps1)))`
  - `-File`로 실행하지 않음. 파일이 BOM 없는 UTF-8이라 Windows PowerShell 5.1은 시스템 코드 페이지로 읽어서 한국어 문구가 깨짐
- Windows PowerShell 5.1(기본 탑재)과 PowerShell 7을 지원하고, 5.1에서는 TLS 1.2를 강제함. HTTPS가 아닌 주소로 리디렉션되면 멈춤.
- uv 0.13.1의 `x86_64-pc-windows-msvc`, `aarch64-pc-windows-msvc` 해시를 기존 6개 타겟 옆에 고정함
- 의존성: `uv.lock`을 확인해 보니 컴파일된 패키지(`jiter`, `pydantic-core`)에 `win_amd64`, `win_arm64` wheel이 있고, `cffi`와 `cryptography`는 Linux에서만 쓰임. 그래서 `--only-binary :all:` 해시 검증 설치가 두 Windows 아키텍처에서 그대로 됨.
- 설치 위치: 프로그램은 `%LOCALAPPDATA%\Programs\claubiz`, 명령은 `...\claubiz\bin\claubiz.exe`
- 가상 환경 교체 방식
  - macOS/Linux의 `--relocatable` 방식 대신, 같은 경로에 설치하고 실패하면 되돌림 (기존 `venv` -> `venv.old`로 옮긴 뒤 새로 설치하고, 동작 확인에 실패하면 `venv.old`로 복구)
  - 이유: Windows 실행 파일(uv trampoline)은 Python 경로를 파일 안에 담고 있어서, 고정 경로에 설치해야 `bin`으로 복사한 `claubiz.exe`가 계속 동작함
  - claubiz나 예약 작업이 실행 중이면 폴더를 옮길 수 없으므로 종료하라고 안내하고 멈춤
- PATH
  - `venv\Scripts`는 PATH에 넣지 않음. `python.exe`와 의존성 명령이 함께 노출되기 때문
  - `bin`에 `claubiz.exe`만 둠
  - `.cmd` 래퍼는 쓰지 않음. `cmd.exe`가 인자를 다시 해석해서 `R&D` 같은 목표 문구가 명령으로 실행될 수 있고(BatBadBut 계열, CVE-2024-24576), Ctrl+C마다 "일괄 작업을 끝내시겠습니까" 질문이 뜸
  - `HKCU\Environment`의 Path에만 추가함. `REG_EXPAND_SZ` 형식과 `%USERPROFILE%` 같은 펼치지 않은 항목을 유지하고, 추가한 뒤 `WM_SETTINGCHANGE`로 알림
  - `setx`는 1024자에서 잘리므로 쓰지 않음
  - macOS/Linux 설치 스크립트는 셸 설정을 건드리지 않지만, Windows는 기본 PATH에 사용자 bin 폴더가 없어서 기본으로 추가함. `-NoModifyPath`로 끌 수 있고, 제거할 때 되돌림
- 관리자 권한(UAC 상승) 세션에서는 설치를 거부함 (install.sh가 root를 거부하는 것과 같음)
- `.claubiz-install` 표식 파일로 다른 폴더를 덮어쓰거나 지우지 못하게 막는 장치는 그대로 둠
- 코드 서명(Authenticode)은 인증서 비용 문제로 이번 범위에서 뺌. 무결성은 다른 곳에 공개한 SHA-256과 스크립트에 고정한 해시로 확인함.

## 8. 테스트와 검증

- `symlink_to`, `chmod`, `getuid`, `fcntl`에 기대는 테스트 파일은 6개 (`test_security`, `test_features`, `test_automation`, `test_resume_export`, `test_tui_jobs`, `test_release`)
  - POSIX 전용 단언에는 `skipif(sys.platform == "win32")`를 붙임
  - Windows 전용 테스트를 추가함: ACL이 소유자 전용인지 다시 읽어 확인, 정션과 심볼릭 링크 거부, 두 프로세스 간 잠금 경쟁, `\r\n`과 `0x1A`가 섞인 바이트가 그대로 왕복하는지, 예약 이름 거부, 키 저장 persist 값
- GitHub Actions `.github/workflows/ci.yml`
  - `macos-latest`, `ubuntu-latest`, `windows-latest`, `windows-11-arm`
  - 액션은 커밋 SHA로 고정하고 권한은 `contents: read`, 비밀 값은 쓰지 않음
  - Windows 작업에서 배포 묶음을 만들고 `install.ps1 -Source <로컬 경로>`로 설치, `claubiz --version`, 제거까지 확인함
  - push해야 실행되므로 push 시점은 사용자가 정함
- 이 Mac에서는 POSIX 쪽 회귀(기존 93개 테스트, ruff, shellcheck)만 확인할 수 있음. Windows 쪽은 CI 결과나 실제 기기로만 확인할 수 있음.

## 9. 단계

| 단계 | 내용 | 끝났다고 보는 기준 |
|---|---|---|
| 0 | CI 추가 (Windows 작업 포함) | Windows 작업이 지금 상태에서 `fcntl` import 오류로 실패하는 것을 확인 |
| 1 | `hostos` 계층과 POSIX 구현 이동 | macOS/Linux 테스트 그대로 통과, 동작 변화 없음 |
| 2 | Windows 보안 구현 (2·3·4절) | Windows CI에서 전체 테스트와 Windows 전용 테스트 통과 |
| 3 | 작업 스케줄러, 파일 열기, TUI 경로 입력 (5·6절) | 등록, 상태, 해제와 제거 거부 장치 테스트 통과 |
| 4 | `install.ps1`과 `release.py` 확장 (7절) | CI에서 설치, 실행, 재설치, 제거 통과 |
| 5 | 실제 Windows Terminal에서 수동 점검 (6절 목록) | 점검 목록 통과 후 main에 병합, 1.0.0 배포 |

커밋은 단계별 논리 단위로 나누고 push는 하지 않음 (요청 시에만).

## 10. 먼저 확인할 것 (2단계 시작 전, Windows CI에서)

1. Python 3.12의 `os.open`이 Windows에서 기본 텍스트 모드인지. 결과와 상관없이 `O_BINARY`는 붙이지만 테스트 기대값을 정하는 데 필요함.
2. uv가 만든 `venv\Scripts\claubiz.exe`를 `bin`으로 복사해도 실행되는지 (가상 환경을 고정 경로에 둔 경우). 안 되면 7절의 PATH 방식을 다시 정함.
3. 도메인에 가입하지 않은 PC에서 `CRED_PERSIST_LOCAL_MACHINE`으로 저장하고 읽기가 되는지
4. 경로 대체 비트 판별로 정션과 심볼릭 링크는 거부되고 OneDrive 자리 표시자는 허용되는지

## 범위 밖

- Linux 백그라운드 실행(systemd user timer). 지금처럼 cron 안내 유지
- 설치 스크립트 코드 서명, winget·Scoop 패키지 배포
- Windows 10 22H2보다 오래된 버전, 32비트(x86)
