# 영국 영어 사용 규칙

## 프로젝트 배경
- 사용자는 한국말을 쓰는 영국사람
- 이 게임은 영국사람들을 타겟으로 함
- 모든 영어 텍스트는 영국 영어(British English)를 사용해야 함

## 영국 영어 적용 범위
- 게임 내 모든 영어 메시지 및 텍스트
- 번역 파일 (data/translations/en.json)
- 사용자 인터페이스 텍스트
- 에러 메시지 및 시스템 메시지

## 영국 영어 특징
### 철자 차이점
- `-ise` 어미 사용 (recognise, realise, organise)
- `-our` 어미 사용 (colour, honour, favour)
- `-re` 어미 사용 (centre, theatre)
- `s` 대신 `c` 사용 (defence, licence)

### 어휘 차이점
- 미국식 "elevator" → 영국식 "lift"
- 미국식 "apartment" → 영국식 "flat"
- 미국식 "garbage" → 영국식 "rubbish"
- 미국식 "candy" → 영국식 "sweets"

## 게임 텍스트 적용 예시
```json
{
  "command_not_found": "Command not recognised.",
  "item_colour": "The item's colour is...",
  "defence_bonus": "Defence bonus applied",
  "centre_location": "You are at the centre of..."
}
```

## 주의사항
- 격식과는 무관하게 자연스러운 영국 영어 사용
- 일상적이고 친근한 톤 유지
- 게임의 판타지 설정에 맞는 어휘 선택
- 영국 영어 철자 및 어휘를 일관되게 적용

## 번역 작업 시 체크리스트
- [ ] `-ize` → `-ise` 변경 확인
- [ ] `-or` → `-our` 변경 확인  
- [ ] `-er` → `-re` 변경 확인
- [ ] 미국식 어휘를 영국식으로 변경
- [ ] 자연스러운 영국 영어 표현 사용