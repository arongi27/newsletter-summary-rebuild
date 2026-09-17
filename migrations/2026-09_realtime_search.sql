-- 미수집 검색어 실시간 조회 기능에 필요한 스키마 변경.
-- ALTER TABLE ... ADD COLUMN(기본값 없음)과 새 테이블/인덱스 생성은
-- 전부 메타데이터 수준의 빠른 변경이라, 이전 인덱스 마이그레이션과
-- 달리 CONCURRENTLY나 단계별 실행이 필요 없다. Neon SQL Editor에서
-- 한 번에 실행해도 된다.

-- 검색할 때마다 갱신되는 시각. collector.py가 "최근 검색됐는데
-- 마지막 수집이 오래된" 키워드를 재수집 대상으로 고르는 데 쓴다.
ALTER TABLE search_keywords
    ADD COLUMN IF NOT EXISTS last_searched_at TIMESTAMPTZ;

-- 웹 서버의 실시간 네이버 API 호출을 전체 기준 분당 제한하기 위한
-- 호출 기록 테이블. gunicorn 워커가 여러 개여도 DB에 남긴 시각으로
-- 세기 때문에 프로세스 경계와 무관하게 정확하다.
CREATE TABLE IF NOT EXISTS realtime_fetch_log (
    id SERIAL PRIMARY KEY,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_realtime_fetch_log_requested_at
    ON realtime_fetch_log (requested_at);
