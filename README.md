# JAVIS — TWOM Auto Translator (TH ↔ EN ↔ KO)

บอท Discord แบบฟรีที่ใช้ Argos Translate + CTranslate2 แปลข้อความอัตโนมัติเป็น 3 ภาษาในคำตอบเดียว และมี TWOM Dictionary สำหรับศัพท์เฉพาะ

## โครงสร้าง

- `bot.py` — ตัวบอท + HTTP health endpoint
- `dictionary.json` — TWOM Dictionary แก้ศัพท์ได้เอง
- `Dockerfile` — ติดตั้ง Argos models ตอน build
- `render.yaml` — ค่า Render Free
- `requirements.txt` — Python dependencies

## Runtime settings

- CPU only
- CTranslate2/Argos: INT8
- 1 inter thread / 1 intra thread
- batch size 8
- beam size 2

## คำสั่ง

- `!javis status`
- `!javis reload` (ผู้มี Manage Server เท่านั้น)

ข้อความทั่วไปจะถูกแปลอัตโนมัติ ไม่ต้องใช้คำสั่ง


## Log fix: Render build failure

The previous build stopped during dependency installation because `argostranslate==1.11.1` was requested while PyPI provides `1.11.0` as the current release. Use `argostranslate==1.11.0` in `requirements.txt`.

The service keeps `/health` and `/ping` as JSON endpoints. `/` now serves `index.html`. Discord message handling, translation flow, TWOM Dictionary, and memory settings are otherwise preserved.

## Discord mention safety

Outgoing messages use `AllowedMentions.none()` so translated text cannot accidentally trigger user/role/everyone mentions. The bot still needs Message Content Intent enabled in the Discord Developer Portal because it reads normal message content.
