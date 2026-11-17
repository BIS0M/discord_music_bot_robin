# Robin 🎵

친구들 디스코드 서버에서 쓰는 노래봇. 슬래시 명령어 대신 **전용 채널의 버튼 패널**로 조작한다.

## 기능
- `/setup` (관리자) — 지금 채팅 채널을 로빈 전용 채널로 정하고 패널을 고정
- **부르기** — 누른 사람이 있는 음성 채널로 로빈 입장
- **노래 추가** — 팝업에 유튜브 링크/노래 제목 입력 (재생목록 링크는 최대 50곡)
- **일시정지 / 재생**, **다음 노래**, **중지**(목록 비우기, 로빈은 남음), **퇴장**
- **노래 목록** — 나만 보이는 목록, 노래마다 ✕ 로 빼기 (10곡씩 페이지)
- `/volume 0~100` — 100이 원본 음량, 기본 50
- 재생 없이 30분 지나거나 채널에 혼자 1분 남으면 자동 퇴장
- 유튜브가 서버를 차단하면 패널에 경고를 띄우고 재생을 멈추며, 관리자에게 DM으로 알림 (재생이 다시 되면 자동 해제)

> 버튼은 **로빈과 같은 음성 채널에 있는 사람만** 누를 수 있다. 노래 목록 보기는 누구나 가능.

---

## 1. 디스코드 개발자 포털에서 봇 만들기 (처음 1번)

[discord.com/developers/applications](https://discord.com/developers/applications)

1. **새 애플리케이션** → 이름 `Robin`
2. 왼쪽 **봇**
   - **토큰 재설정** → 토큰 복사해서 `.env`의 `DISCORD_TOKEN`에 넣기 (다시 볼 수 없고, 절대 공유 금지)
   - **공개 봇** 끄기 → 나만 서버에 초대할 수 있게
     - 꺼지지 않으면: 왼쪽 **설치** 탭 → 설치 링크를 **없음**으로 바꾸고 저장 후 다시 끄기
   - **특수 권한 게이트웨이 인텐트**는 전부 꺼둬도 된다 (필요 없음)

## 2. 디스코드 서버에 로빈 초대하기 (서버마다)

1. 개발자 포털 → 왼쪽 **OAuth2** → **OAuth2 URL 생성기**
2. **스코프**: `bot`, `applications.commands`
3. **봇 권한**
   - 일반 권한: `채널 보기`
   - 채팅 권한: `메시지 보내기`, `링크 임베드`
   - 음성 권한: `연결`, `말하기`
4. **연동 유형**: 길드 설치
5. 맨 아래 **생성된 URL**을 브라우저에 붙여넣기 → 서버 선택 → **승인**
   - 그 서버의 **관리자 / 서버 관리 권한**이 있어야 목록에 뜬다. 없으면 서버 주인에게 링크를 보내 초대 부탁
6. 같은 링크로 서버마다 반복

## 3. 디스코드 서버 설정 (서버마다)

### 전용 채널 만들기
1. 로빈 전용 채팅 채널을 하나 만든다 (예: `🎵robin`)
2. 그 채널에서 관리자가 **`/setup`** 입력 → 패널이 고정된다
   - `/setup`은 **서버 관리 권한**이 있는 사람에게만 보인다
   - 로빈에게 필요한 권한이 빠져 있으면 `/setup` 결과 메시지에 표시된다
3. 패널이 채팅에 묻히거나 지워졌으면 `/setup`을 다시 치면 맨 아래에 새로 올라온다

### 채널 권한 확인
- **전용 채널**: 로빈에게 `채널 보기`, `메시지 보내기`, `링크 임베드`
- **음성 채널**: 로빈에게 `채널 보기`, `연결`, `말하기`
- 채널별로 권한을 막아둔 서버라면 채널 설정 → 권한에서 로빈을 추가하고 위 권한을 허용

### (추천) 전용 채널은 로빈만 글을 쓰게
패널이 항상 맨 아래에 보이게 하는 설정. 버튼과 노래 추가 팝업은 메시지 보내기 권한 없이도 동작한다.
1. 채널 설정 → 권한 → `@everyone`의 **메시지 보내기 ✕(거부)**
2. 같은 화면에서 **로빈을 추가**하고 **메시지 보내기 ✓(허용)**
   - 이걸 안 하면 `@everyone` 거부가 로빈에게도 적용돼서 패널을 못 올린다
3. 관리자는 그대로 `/setup`을 쓸 수 있다

### 관리자 DM 받기 (유튜브 차단 알림)
- 알림은 **개발자 포털에서 봇을 만든 계정**(봇 소유자)에게 간다. 다른 사람이 받게 하려면 `.env`에 `ADMIN_USER_ID`
- 받는 사람은 로빈과 **같은 서버에 있어야** 하고, 그 서버에서 **서버 이름 우클릭 → 개인정보 보호 설정 → "서버 멤버가 보내는 다이렉트 메시지 허용"**이 켜져 있어야 한다

---

## 4. 설치와 실행

### 준비물
- Python 3.10+
- ffmpeg, [Deno](https://deno.com) (yt-dlp가 유튜브 해석에 사용)

### PC (Windows, conda)
```powershell
conda create -n robin python=3.12 -y
conda activate robin
winget install Gyan.FFmpeg
winget install DenoLand.Deno        # 설치 후 터미널 다시 열기
pip install -r requirements.txt
copy .env.example .env              # DISCORD_TOKEN 채우기
python bot.py
```

### 리눅스 서버 (Ubuntu, 24시간 실행)
```bash
# 램이 1GB면 스왑 2GB 추천
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

sudo apt update && sudo apt install -y ffmpeg python3-venv python3-pip unzip git
curl -fsSL https://deno.land/install.sh | sh -s -- -y
sudo cp ~/.deno/bin/deno /usr/local/bin/

git clone https://github.com/BIS0M/discord_music_bot_robin.git ~/robin
cd ~/robin
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
nano .env                            # DISCORD_TOKEN=..., BITRATE=64
.venv/bin/python bot.py              # 테스트 후 Ctrl+C
```

자동 실행 등록 (systemd):
```bash
sudo tee /etc/systemd/system/robin.service > /dev/null <<EOF
[Unit]
Description=Robin Discord music bot
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$HOME/robin
ExecStart=$HOME/robin/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now robin
```

| 하고 싶은 것 | 명령어 |
|---|---|
| 상태 보기 | `systemctl status robin` |
| 로그 보기 | `journalctl -u robin -f` |
| 코드 업데이트 | `cd ~/robin && git pull && sudo systemctl restart robin` |
| 끄기 / 켜기 | `sudo systemctl stop robin` / `sudo systemctl start robin` |

> ⚠️ 같은 토큰으로 PC와 서버에서 동시에 돌리지 말 것. 서로 충돌한다.
> 서버로 옮기면 전용 채널에서 `/setup`을 한 번 다시 해야 한다 (`robin_data.json`은 git에 안 올라감).

---

## `.env` 설정값
| 이름 | 기본값 | 설명 |
|---|---|---|
| `DISCORD_TOKEN` | (필수) | 봇 토큰 |
| `GUILD_IDS` | 비움 | 쉼표로 구분한 서버 ID. 넣으면 해당 서버에만 명령어 즉시 등록 (개발용). 평소엔 비워두기 |
| `DEFAULT_VOLUME` | 50 | 기본 볼륨 0~100 |
| `BITRATE` | 128 | 전송 음질 kbps. 트래픽을 아끼려면 64 |
| `IDLE_TIMEOUT` | 1800 | 재생이 없을 때 자동 퇴장까지 초 |
| `ALONE_TIMEOUT` | 60 | 채널에 혼자 남았을 때 자동 퇴장까지 초 |
| `YT_COOKIES` | cookies.txt | 유튜브 차단 시 쓸 쿠키 파일 (파일이 있을 때만 사용) |
| `FFMPEG_PATH` | ffmpeg | ffmpeg 경로 |
| `ADMIN_USER_ID` | 비움 | 유튜브 차단 알림 DM 받을 사용자 ID. 비우면 봇 소유자 |

## 문제 해결
| 증상 | 원인 / 해결 |
|---|---|
| 로빈이 들어오는데 소리가 안 남 | `davey`가 0.2.x면 0.1.7로 낮추기: `pip install "davey==0.1.7"` |
| 슬래시 명령어가 안 보임 | 디스코드 Ctrl+R. 전역 등록은 반영에 몇 분 걸릴 수 있음 |
| 같은 명령어가 두 번 보임 | 봇을 재시작하면 예전 서버 전용 명령어가 자동 정리됨 |
| 패널이 노란색 "유튜브가 막았어요" | 서버의 `~/robin`에 `cookies.txt`를 넣고 `sudo systemctl restart robin` |
| 노래가 끝났는데 초록 테두리가 남음 | 디스코드 앱 표시 문제. Ctrl+R |

## 참고
- `davey`는 0.1.7로 고정. 0.2.x는 discord.py 2.7.1과 맞지 않아 음성 암호화가 준비되지 않고 소리가 안 난다.
- `.env`, `cookies.txt`, `robin_data.json`(서버별 전용 채널 정보)은 git에 올리지 않는다.
