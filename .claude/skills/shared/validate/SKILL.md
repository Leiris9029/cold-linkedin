---
description: "연락처 검증 - LinkedIn 기반 담당자 정보 검증 및 동일 회사 추천"
---

# 연락처 검증 스킬

## 역할
CSV로 입력된 연락처 목록의 정보를 LinkedIn 기반으로 검증합니다.
- 직함 매칭 (CSV ↔ LinkedIn 현재 직함)
- 재직 상태 확인 (해당 회사에 현재 재직 중인지)
- 위치 확인 (일본 내 근무/거주 여부)
- 동일 회사 적합 담당자 추천

---

## 입력 방식

### CSV 파일 기반
```
/validate data/contacts.csv
```

### 단일 검증
```
/validate [이름] [회사] [LinkedIn URL]
```

---

## 필수 입력 컬럼

| 컬럼 | 필수 | 설명 |
|---|---|---|
| contact_name | ✅ | 담당자 이름 |
| company | ✅ | 회사명 |
| title | ✅ | 직함 (검증 대상) |
| linkedin_url | ⭕ | LinkedIn 프로필 URL (있으면 검증 정확도 높아짐) |
| email | ⭕ | 이메일 (검증 결과에 포함) |

---

## 검증 항목

### 1. 직함 매칭 (Title Match)

CSV에 입력된 직함과 LinkedIn 현재 직함을 비교합니다.

**판정 기준:**
| 상태 | 조건 |
|---|---|
| ✅ MATCH | 직함이 동일하거나 동등한 표현 (BD Director = Director of BD) |
| ⚠️ CHANGED | 직함이 변경됨 (예: Manager → Director 승진) |
| ❌ MISMATCH | 완전히 다른 직함/부서 |

**동등 직함 예시:**
- `BD Director` = `Director of Business Development` = `事業開発部長`
- `VP` = `Vice President`
- `部長` = `General Manager` = `Head of`

### 2. 재직 상태 (Employment Status)

해당 회사에 현재 재직 중인지 확인합니다.

**판정 기준:**
| 상태 | 조건 |
|---|---|
| ✅ CURRENT | 현재 해당 회사에 재직 중 |
| ⚠️ RECENT | 최근 퇴사 (6개월 이내) - 연락 가능성 있음 |
| ❌ LEFT | 퇴사함 (다른 회사로 이직) |
| ❓ UNKNOWN | LinkedIn 정보 없음/확인 불가 |

### 3. 위치 확인 (Location)

현재 근무/거주 위치를 확인합니다.

**판정 기준:**
| 상태 | 조건 |
|---|---|
| ✅ JAPAN | 일본 내 (Tokyo, Osaka, Japan 등) |
| ⚠️ APAC | 아시아태평양 지역 (Singapore, Hong Kong 등) |
| ❌ OTHER | 기타 지역 (US, EU 등) |

### 4. 동일 회사 추천 (Same Company Suggestions)

검증 대상이 부적합한 경우 (퇴사, 직함 불일치 등), 같은 회사의 적합한 담당자를 추천합니다.

**추천 기준:**
- BD / Licensing / 事業開発 관련 직함
- CNS / Neuroscience 관련 직함 (Product 2 대상인 경우)
- Director 이상 직급

---

## 검증 방법

### 방법 1: LinkedIn URL 직접 조회 (권장)
CSV에 `linkedin_url`이 있으면 해당 프로필을 직접 조회합니다.

### 방법 2: 이름 + 회사 검색
`linkedin_url`이 없으면 이름과 회사명으로 LinkedIn 검색합니다.

### 방법 3: 수동 확인 요청
자동 검증이 불확실한 경우 사용자에게 수동 확인을 요청합니다.

---

## 출력 형식

### 요약 (Summary)

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 검증 결과 요약
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
총 {N}명 검증 완료

✅ 유효: {n}명 (발송 가능)
⚠️ 확인 필요: {n}명 (수동 확인 권장)
❌ 부적합: {n}명 (발송 제외 권장)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### 개별 결과

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
👤 {contact_name} ({company})
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CSV 직함: {csv_title}
LinkedIn 직함: {linkedin_title}
직함 매칭: {✅|⚠️|❌} {상태}
재직 상태: {✅|⚠️|❌} {상태}
위치: {✅|⚠️|❌} {location}

📌 판정: {VALID | NEEDS_REVIEW | INVALID}
💡 권장 액션: {권장 사항}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### 부적합 시 대체 추천

```
❌ {contact_name} ({company}) - 퇴사함
   현재: {new_company}, {new_title}

   🔄 같은 회사 추천:
   1. {추천1_name} - {추천1_title}
      LinkedIn: {url}
   2. {추천2_name} - {추천2_title}
      LinkedIn: {url}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## 검증 결과 CSV 출력

검증 완료 후 결과를 CSV로 출력합니다.

**출력 파일:** `output/validated_YYYYMMDD.csv`

**추가 컬럼:**
| 컬럼 | 설명 |
|---|---|
| validation_status | VALID / NEEDS_REVIEW / INVALID |
| title_match | MATCH / CHANGED / MISMATCH |
| employment_status | CURRENT / RECENT / LEFT / UNKNOWN |
| location | 확인된 위치 |
| linkedin_title | LinkedIn에서 확인된 직함 |
| notes | 검증 메모 (변경 사항, 추천 등) |
| suggested_replacement | 대체 추천 담당자 (있으면) |

---

## 실행 절차

1. CSV 파일 읽기 및 필수 컬럼 확인
2. 각 연락처에 대해:
   a. LinkedIn 프로필 조회 (URL 또는 검색)
   b. 직함 매칭 검증
   c. 재직 상태 확인
   d. 위치 확인
   e. 부적합 시 동일 회사 추천 검색
3. 검증 결과 요약 출력
4. 개별 결과 상세 출력
5. 결과 CSV 저장

---

## 주의사항

1. **LinkedIn 접근 제한**
   - 과도한 조회 시 제한될 수 있음
   - 배치 검증 시 적절한 간격 유지

2. **정보 정확도**
   - LinkedIn 정보가 최신이 아닐 수 있음
   - 비공개 프로필은 검증 불가
   - 불확실한 경우 NEEDS_REVIEW로 표시

3. **개인정보 보호**
   - 검증 결과는 내부 용도로만 사용
   - 외부 공유 금지

---

## 연동 워크플로우

```
[CSV 업로드] → [/validate] → [검증 결과 확인] → [수정/확정] → [/coldmail]
```

검증 결과가 VALID인 연락처만 자동으로 콜드메일 생성 대상에 포함됩니다.
NEEDS_REVIEW는 사용자 확인 후 포함 여부를 결정합니다.
INVALID는 기본적으로 제외되며, 대체 추천이 있으면 해당 담당자로 교체할 수 있습니다.
