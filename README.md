# Robin 🎵

친구들 디스코드 서버에서 쓰는 노래봇. 슬래시 명령어 대신 **전용 채널의 버튼 패널**로 조작한다.

## 기능
- `/setup` (관리자) — 지금 채팅 채널을 로빈 전용 채널로 정하고 패널을 고정
- **부르기** — 누른 사람의 음성 채널로 로빈 입장
- **노래 추가** — 팝업에 유튜브 링크/노래 제목 입력 (재생목록은 최대 50곡)
- **일시정지 / 재생**, **다음 노래**, **중지**(목록 비우기, 로빈은 남음), **퇴장**
- **노래 목록** — 나만 보이는 목록, 노래마다 ✕ 로 빼기 (10곡씩 페이지)
- `/volume 0~100` — 100이 원본 음량, 기본 50
- 재생 없이 30분 지나거나 채널에 혼자 1분 남으면 자동 퇴장
- 유튜브가 서버를 차단하면 패널에 경고를 띄우고 재생을 멈추며, 관리자에게 DM으로 알림 (재생이 다시 되면 자동 해제)

## 준비물
- Python 3.10+
- ffmpeg, [Deno](https://deno.com) (yt-dlp가 유튜브 해석에 사용)

## 설치
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip ...
cp .env.example .env                            # DISCORD_TOKEN 채우기
.venv/bin/python bot.py
```

### Discord 개발자 포털 설정
- Bot → 토큰 발급, Public Bot 끄기
- OAuth2 → URL 생성기: 스코프 `bot`, `applications.commands` / 권한 `채널 보기`, `메시지 보내기`, `링크 임베드`, `연결`, `말하기`

## `.env` 설정값
| 이름 | 기본값 | 설명 |
|---|---|---|
| `DISCORD_TOKEN` | (필수) | 봇 토큰 |
| `GUILD_IDS` | 비움 | 쉼표로 구분한 서버 ID. 넣으면 해당 서버에 명령어 즉시 등록 (개발용) |
| `DEFAULT_VOLUME` | 50 | 기본 볼륨 0~100 |
| `BITRATE` | 128 | 전송 음질 kbps. 트래픽을 아끼려면 64 |
| `IDLE_TIMEOUT` | 1800 | 재생이 없을 때 자동 퇴장까지 초 |
| `ALONE_TIMEOUT` | 60 | 채널에 혼자 남았을 때 자동 퇴장까지 초 |
| `YT_COOKIES` | cookies.txt | 유튜브 차단 시 쓸 쿠키 파일 |
| `FFMPEG_PATH` | ffmpeg | ffmpeg 경로 |
| `ADMIN_USER_ID` | 비움 | 유튜브 차단 알림 DM 받을 사용자 ID. 비우면 봇 소유자 |

## 참고
- `davey`는 0.1.7로 고정. 0.2.x는 discord.py 2.7.1과 맞지 않아 음성 암호화가 준비되지 않고 소리가 안 난다.
- `.env`, `cookies.txt`, `robin_data.json`(서버별 전용 채널 정보)은 git에 올리지 않는다.
