---
description: "문의 폼 작성 - 일본 제약사 홈페이지 문의 폼/공동 이메일용 BD 문의 작성"
---

# 문의 폼 작성 스킬

## 역할
일본 제약사/바이오 회사 홈페이지의 문의 폼 또는 공동 문의 이메일 주소로 보내는 BD/인라이선싱(導入) 제휴 문의를 작성합니다.

**핵심 원칙:** 검증된 정보만 사용하고, 사용자가 지정하지 않은 내용은 추가하지 않습니다.

---

## 공통 문법 규칙

**반드시 `.claude/skills/japan/_common/GRAMMAR_JA.md` 파일을 먼저 읽고 적용합니다.**

주요 규칙:
- 「の」 삽입 (RISORIUS Inc.**の**共同創業者)
- 한국 제약사 협업은 별도 문장 + 「とも」
- CTA는 사용자 지정 내용만 (기본: 관련 부서 연결 요청)
- 미검증 수치/제품명 사용 금지
- 문장 연결 자연스러움 확인

---

## 입력 방식

### 폼 스크린샷 기반
```
/contact-form [회사명] [스크린샷]
```
스크린샷의 폼 필드에 맞게 내용을 작성합니다.

### 이메일 형식
```
/contact-form [회사명] 이메일로
```
공동 문의 주소로 보내는 이메일 형식으로 작성합니다.

### 직접 지정
```
/contact-form [회사명] [제품번호]
```
지정된 제품으로 문의 내용을 작성합니다.

---

## 제품별 템플릿

### Product 1: In-Licensing Workflow AI (BD&L 회사용)

```
突然のご連絡失礼いたします。RISORIUS Inc.の共同創業者、リュ・イムスと申します。

弊社はBD/ライセンシング業務において、外部アセット評価から検討資料作成までを自動化するオンプレミスAIワークフローツールを開発しております。

貴社が{会社特化表現}を拝見し、{活用ポイント}にお役立ていただけるのではないかと考え、ご連絡いたしました。現在、韓国の製薬企業ともBD領域で協業を進めております。

ご関心をお持ちいただけましたら、関連部署・ご担当者様へおつなぎいただけますと幸いです。

何卒よろしくお願い申し上げます。

リュ・イムス
RISORIUS Inc.の共同創業者
Email: leiris@risorious.com
Tel: +82-10-9592-2268
```

### Product 2: Neuro-Biomarker Co-Scientist (CNS 회사용)

```
突然のご連絡失礼いたします。RISORIUS Inc.の共同創業者、リュ・イムスと申します。

弊社はEEG（脳波）を用いて患者の薬物反応性を予測し、治療効果を定量化するオンプレミスAI co-scientistを開発しております。

貴社が{会社特化表現}を拝見し、{活用ポイント}にお役立ていただけるのではないかと考え、ご連絡いたしました。現在、韓国の製薬企業ともCNS領域で協業を進めております。

ご関心をお持ちいただけましたら、関連部署・ご担当者様へおつなぎいただけますと幸いです。

何卒よろしくお願い申し上げます。

リュ・イムス
RISORIUS Inc.の共同創業者
Email: leiris@risorious.com
Tel: +82-10-9592-2268
```

---

## 회사별 특화 표현 예시

### Product 2 (CNS) 대상 회사

| 회사 | 特化表現 | 活用ポイント |
|---|---|---|
| Eisai | ニューロサイエンス領域に注力されていること | CNS治療薬の開発において患者層別化や効果の客観的評価 |
| Sumitomo Pharma | CNS領域に注力されていること | CNS治療薬の開発において患者層別化や効果の客観的評価 |
| Otsuka | CNS領域において長年にわたりご注力されていること | CNS治療薬の開発において患者層別化や効果の客観的評価 |
| SanBio | 再生医療によるCNS領域に注力されていること | 治療効果の客観的評価 |
| Viatris Japan | 日本においてCNS領域のポートフォリオを拡大されていること | 睡眠障害やてんかん領域における患者層別化や効果の客観的評価 |

### Product 1 (BD&L) 대상 회사

| 회사 | 特化表現 | 活用ポイント |
|---|---|---|
| Takeda | 積極的なインライセンス活動を展開されていること | 外部アセット評価の効率化 |
| Shionogi | M&A・インライセンス活動を積極的に展開されていること | 案件評価やDD資料作成の効率化 |
| Kissei | インライセンスによるパイプライン拡充を進められていること | 外部候補評価の効率化 |
| Nobel Pharma | 外部アセット導入を中心としたビジネスモデルを展開されていること | 後期臨床候補の評価効率化 |

---

## 이메일 형식 (공동 문의 주소용)

```
件名: {領域}における協業のご相談

本文:
ご担当者様

{본문 템플릿}

リュ・イムス
RISORIUS Inc. 共同創業者
Email: leiris@risorious.com
Tel: +82-10-9592-2268
```

**件名 예시:**
- CNS領域における協業のご相談
- BD業務効率化に関するご相談
- 外部アセット評価AIのご紹介

---

## 영문 폼용 템플릿

영문 폼인 경우 (Meiji Group 등):

```
Dear [Company] Team,

I am Imsoo Ryoo, Co-founder of RISORIUS Inc., a Korea-based AI company.

We are currently collaborating with Korean pharmaceutical companies on {product description}.

I am reaching out to inquire whether {target subsidiary} has a relevant department for discussing potential collaboration in this area. If so, I would be grateful if this inquiry could be forwarded to the appropriate contact.

Thank you for your time.

Best regards,
Imsoo Ryoo
Co-founder, RISORIUS Inc.
Email: leiris@risorious.com
Tel: +82-10-9592-2268
```

---

## 폼 필드 매핑

| 폼 필드 | 내용 |
|---|---|
| お名前 / Name | リュ・イムス / Imsoo Ryoo |
| 会社名 / Company | RISORIUS Inc. |
| 部署 / Department | 製薬AXチーム (묻는 경우에만) |
| メールアドレス / Email | leiris@risorious.com |
| 電話番号 / Phone | +82-10-9592-2268 |
| 国 / Country | 韓国 / South Korea / Republic of Korea |
| 住所 / Address | 136, Dongsung-gil, Jongno-gu, Seoul, 03084, Rep. of KOREA |
| 郵便番号 / Postal Code | 03084 |

---

## 글자 수 규칙

폼에 글자 수 제한이 있는 경우:
- **목표:** 제한의 **80~90%**
- 너무 짧으면 성의 없어 보임
- 너무 길면 잘림

---

## 실행 절차

1. `.claude/skills/_common/GRAMMAR_JA.md` 읽기
2. 제품 정보 확인
3. `output/research_*.json` 읽기 (회사 리서치 데이터)
4. 회사별 추천 제품 확인
5. 폼 형식에 맞게 템플릿 적용
6. 회사 특화 표현 삽입
7. **검수 체크리스트 확인:**
   - [ ] 「の」: RISORIUS Inc.**の**共同創業者
   - [ ] 한국 제약사 협업 + 「とも」 (별도 문장)
   - [ ] CTA: 관련 부서 연결 (미팅/시간 임의 추가 금지)
   - [ ] 미검증 수치/제품명 없음
   - [ ] 문장 연결 자연스러움
   - [ ] 글자 수 제한 80~90%
8. 결과 출력

---

## 주의사항

1. **사용자가 지정하지 않은 CTA 추가 금지**
   - ❌ 「ウェブ面談」「15〜20分」 등 미팅/시간 언급
   - ✓ 「関連部署・ご担当者様へおつなぎいただけますと幸いです」

2. **개발 주체 명확히**
   - ❌ 「韓国の製薬企業と協業し開発」
   - ✓ 「開発しております。現在、韓国の製薬企業とも...」

3. **검증되지 않은 정보 금지**
   - 상대 회사 구체적 제품명/수치 직접 언급 피함
   - 일반적 표현 사용: 「CNS領域に注力されている」

4. **문장 연결 자연스러움**
   - 작성 후 앞뒤 문장 연결이 자연스러운지 반드시 확인
   - 갑자기 다른 주제로 전환하지 않음
