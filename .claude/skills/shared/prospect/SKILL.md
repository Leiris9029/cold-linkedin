---
description: "프로스펙트 인리치먼트 - Apollo.io 결과 분석, 이메일 추론, 적합도 평가"
---

# 프로스펙트 인리치먼트 스킬

## 역할

Apollo.io API에서 검색한 prospect 후보 데이터를 분석하여:
1. 누락된 이메일 주소를 패턴 기반으로 추론
2. 각 prospect의 적합도(fit score)를 평가
3. 콜드메일 대상 우선순위를 매김

## 입력

1. **Apollo.io 검색 결과 JSON**: 이름, 직함, 회사, 이메일(있을 수도/없을 수도), LinkedIn URL 등
2. **검색 기준**: 사용자가 설정한 타겟 조건 (산업, 직급, 지역 등)
3. **(선택) 이메일 패턴 참고 데이터**: 동일 회사의 기존 이메일 목록
4. **(선택) 업계 리서치 데이터**: ClinicalTrials.gov 임상시험 + PubMed 논문 데이터
   - 회사별 진행 중인 임상시험 목록 (적응증, 상태, 연구자)
   - 최근 논문 활동 (주제, 저자, 저널)

## 처리 단계

### 1단계: 이메일 패턴 추론 (이메일 누락 시)

- 동일 회사에서 이메일이 있는 다른 직원의 패턴을 분석
- 일반적인 이메일 패턴 우선순위:
  1. `first.last@domain` (가장 일반적)
  2. `firstlast@domain`
  3. `f.last@domain`
  4. `flast@domain`
  5. `first_last@domain`
  6. `first@domain`
- 일본 기업의 경우 `.co.jp` 도메인 가능성 고려
- 회사 도메인은 Apollo 데이터에서 추출하거나 회사명에서 추론

**Confidence 기준:**
| Level | 조건 |
|-------|------|
| `verified` | Apollo에서 직접 제공한 이메일 |
| `high` | 같은 회사 이메일 2개 이상에서 패턴 확인됨 |
| `medium` | 같은 회사 이메일 1개에서 패턴 추론 |
| `low` | 회사명에서 도메인 추론, 패턴 불확실 |

### 2단계: 적합도 평가 (Fit Score 1~10)

각 prospect에 대해 아래 기준으로 종합 점수 부여:

| 항목 | 배점 | 설명 |
|------|------|------|
| 직함 매칭 | 0~2.5 | 타겟 직급(Director, VP, Head 등)과 일치 정도 |
| 부서 매칭 | 0~2.5 | BD, R&D, Licensing 등 타겟 부서 일치 여부 |
| 시니어리티 | 0~2 | 의사결정 권한 수준 (C-level > VP > Director > Manager) |
| 이메일 가용성 | 0~1.5 | verified(1.5), high(1), medium(0.5), low(0.25), none(0) |
| 업계 활동 | 0~1.5 | 활발한 임상시험/논문 활동(1.5), 일부(0.75), 없음/데이터없음(0) |

**fit_reason에는 점수 근거를 한 줄로 기록** (예: "VP-level BD, verified email, exact department match")

### 3단계: 회사 맥락 추가

- fit_reason에 구체적 맥락 추가 (CSV 데이터 + 웹 리서치 결과 활용)

## 출력 형식

반드시 아래 형식의 CSV 블록으로 출력:

```csv
contact_name,email,email_confidence,company,title,linkedin_url,fit_score,fit_reason,location,source
```

**규칙:**
- fit_score 내림차순 정렬
- 이메일이 없고 추론도 불가한 경우 email 컬럼 빈칸, email_confidence는 "unknown"
- CSV 필드에 쉼표가 포함된 경우 큰따옴표로 감싸기
- 중복 제거: 동일 이메일+회사 조합은 1건만 유지

## 품질 체크리스트

- [ ] 추론한 이메일에 반드시 confidence 수준 표시
- [ ] fit_score는 1~10 범위의 소수점 1자리
- [ ] fit_reason은 영문 한 줄 (30단어 이내)
- [ ] 중복 제거 완료 (이메일 기준)
- [ ] 이메일 없는 prospect도 누락 없이 포함 (email 빈칸)
